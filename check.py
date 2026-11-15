"""独立核对与不变量测试。

oracle 与执行器刻意走两条完全不同的路径：
- 执行器：每批每来源一条参数化 IN 探测，Python 侧三值逻辑求值；
- oracle：对每条外行用独立 SQLite 相关子查询逐行核对 EXISTS / IN / NOT IN。
  SQLite 的相关子查询自身实现三值逻辑（IN/EXISTS 是正确 oracle）。
- 标量子查询例外：SQLite 对多行结果静默取首行，不符合题定错误语义，
  因此标量部分不用其取值结果做 oracle，而是独立执行「候选计数」核查，
  多行即要求执行器抛出带具体外行 / 内行身份的 CardinalityError。
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from batchjoin.engine import CardinalityError, execute_plan, quote_ident  # noqa: E402
from batchjoin.plan import PlanError, load_plan, load_schema  # noqa: E402
from batchjoin import predicates  # noqa: E402

SCHEMA_PATH = ROOT / "fixtures" / "schema.json"
PLAN_PATH = ROOT / "fixtures" / "plan.json"
SCALAR_PLAN_PATH = ROOT / "fixtures" / "scalar-plan.json"
SEED_PATH = ROOT / "fixtures" / "seed.sql"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def fresh_db(tmp: Path, tag: str) -> Path:
    path = tmp / f"{tag}.db"
    conn = sqlite3.connect(path)
    conn.executescript(SEED_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return path


def run_engine(db_path: Path, plan_path: Path, out_path: Path):
    schema = load_schema(SCHEMA_PATH)
    plan = load_plan(plan_path, schema)
    stats = execute_plan(db_path, plan, schema, out_path)
    rows = json.loads(out_path.read_text(encoding="utf-8"))
    return schema, plan, stats, rows


# --------------------------------------------------------------------------- #
# 独立 oracle：逐行相关子查询
# --------------------------------------------------------------------------- #
def oracle_row(conn: sqlite3.Connection, schema, plan, outer_row: dict) -> dict:
    record = {col: outer_row[col] for col in schema.columns(plan.outer_table)}
    outer = quote_ident(plan.outer_table)
    # 相关条件直接引用外表别名 O 的列，只有定位当前外行的 row_id 走绑定参数。
    row_param = [outer_row[plan.outer_order]]
    for expr in plan.expressions:
        t = quote_ident(expr.inner_table)
        on = (
            f"{t}.{quote_ident(expr.correlate.inner)} = "
            f"O.{quote_ident(expr.correlate.outer)}"
        )
        # O 是外表别名，逐行以 row_id 定位当前外行；外值全部绑定参数。
        locate = f"FROM {outer} O WHERE O.{quote_ident(plan.outer_order)} = ?"
        if expr.op == "exists":
            sql = f"SELECT EXISTS(SELECT 1 FROM {t} WHERE {on}) {locate}"
            value = conn.execute(sql, row_param).fetchone()[0] == 1
        elif expr.op in ("in", "not_in"):
            iv = quote_ident(expr.inner_value)
            ov = quote_ident(expr.outer_value)
            marker = "NOT IN" if expr.op == "not_in" else "IN"
            # 独立相关子查询逐行求值；SQLite 在其引擎内部实现 SQL 三值逻辑。
            sql = (
                f"SELECT O.{ov} {marker} "
                f"(SELECT {t}.{iv} FROM {t} WHERE {on}) {locate}"
            )
            value = conn.execute(sql, row_param).fetchone()[0]
        elif expr.op == "scalar":
            # 不使用 SQLite 标量取值（多行会静默取首行）；只做独立候选计数核查。
            pk_rows = conn.execute(f"PRAGMA table_info({t})").fetchall()
            pk_col = [r[1] for r in pk_rows if r[5] > 0][0]
            # 计数放在 select 列表的标量子查询里，外层 FROM 外表 O 定位当前行。
            count_sql = (
                f"SELECT (SELECT COUNT(*) FROM {t} WHERE {on}) {locate}"
            )
            count = conn.execute(count_sql, row_param).fetchone()[0]
            value = {"_scalar_count": count, "_pk": pk_col}
        else:  # pragma: no cover - plan 校验已拦截
            raise AssertionError(expr.op)
        record[expr.name] = value
    return record


def oracle_all(db_path: Path, plan_path: Path):
    schema = load_schema(SCHEMA_PATH)
    plan = load_plan(plan_path, schema)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = list(schema.columns(plan.outer_table).keys())
    outer = conn.execute(
        f"SELECT * FROM {quote_ident(plan.outer_table)} "
        f"ORDER BY {quote_ident(plan.outer_order)}"
    ).fetchall()
    expected = [
        oracle_row(conn, schema, plan, {c: row[c] for c in cols}) for row in outer
    ]
    conn.close()
    return schema, plan, expected


# --------------------------------------------------------------------------- #
# 1. 主 plan：逐行 oracle 核对 EXISTS / IN / NOT IN
# --------------------------------------------------------------------------- #
def test_oracle_alignment(tmp: Path) -> dict:
    print("\n[1] 独立相关子查询 oracle 逐行核对 EXISTS / IN / NOT IN")
    db = fresh_db(tmp, "main")
    out = tmp / "main-results.json"
    schema, plan, stats, rows = run_engine(db, PLAN_PATH, out)
    _, _, expected = oracle_all(db, PLAN_PATH)

    check("行数与 row_id 顺序一致", [r["row_id"] for r in rows] == [1, 2, 3, 4, 5, 6, 7])
    check("保留外表全部原列", all(list(r)[:3] == ["row_id", "customer_id", "probe"] for r in rows))
    check("表达式按声明顺序追加", all(list(r)[3:] == ["has_payments", "in_codes", "not_in_codes"] for r in rows))

    mismatches = []
    for got, exp in zip(rows, expected):
        for expr in plan.expressions:
            if got[expr.name] != exp[expr.name]:
                mismatches.append((got["row_id"], expr.name, got[expr.name], exp[expr.name]))
    check("三列全部与 SQLite 逐行相关子查询一致", not mismatches, str(mismatches))

    # 人工锚点，直接钉死题述语义的关键 NULL 场景。
    by_id = {r["row_id"]: r for r in rows}
    anchors = {
        # row 1: key=10, probe=7；payments(10): code 7 命中 -> IN TRUE / NOT IN FALSE
        1: {"has_payments": True, "in_codes": True, "not_in_codes": False},
        # row 2: key=10, probe=8；codes(7,NULL) 无等值但有 NULL 比较 -> UNKNOWN/null
        2: {"has_payments": True, "in_codes": None, "not_in_codes": None},
        # row 3: key=20, probe=8；codes(9) 全非空不命中 -> FALSE / TRUE
        3: {"has_payments": True, "in_codes": False, "not_in_codes": True},
        # row 4: key=30, probe=NULL；codes(NULL) 候选非空、外值 NULL -> UNKNOWN/null
        4: {"has_payments": True, "in_codes": None, "not_in_codes": None},
        # row 5: key=40 无内行 -> EXISTS FALSE；空集合 IN FALSE / NOT IN TRUE（含外值 NULL）
        5: {"has_payments": False, "in_codes": False, "not_in_codes": True},
        # row 6: 外键 NULL，不匹配 NULL 内键 -> 全部按空候选处理
        6: {"has_payments": False, "in_codes": False, "not_in_codes": True},
        # row 7: key=50，payments 无该客户 -> 空候选 FALSE / TRUE
        7: {"has_payments": False, "in_codes": False, "not_in_codes": True},
    }
    anchor_bad = [
        (rid, k, by_id[rid][k], v)
        for rid, want in anchors.items()
        for k, v in want.items()
        if by_id[rid][k] != v
    ]
    check("NULL/空集三值锚点全部正确", not anchor_bad, str(anchor_bad))
    return {"stats": stats, "rows": rows, "db": db}


# --------------------------------------------------------------------------- #
# 2. batch_size 不变性 + 查询数随批/来源增长
# --------------------------------------------------------------------------- #
def test_batch_invariance(tmp: Path):
    print("\n[2] batch_size 调整：结果相同，查询数随批数×来源数变化而非外行数")
    base_db = fresh_db(tmp, "sweep")
    baseline = None
    observed = []
    for size in (1, 2, 3, 7, 1000):
        schema = load_schema(SCHEMA_PATH)
        plan = dataclasses.replace(load_plan(PLAN_PATH, schema), batch_size=size)
        out = tmp / f"sweep-{size}.json"
        stats = execute_plan(base_db, plan, schema, out)
        rows = json.loads(out.read_text(encoding="utf-8"))
        if baseline is None:
            baseline = rows
        check(f"batch_size={size} 结果与基线完全一致", rows == baseline)
        observed.append((size, stats.batches, stats.counters["inner_batch_queries"]))

    # 7 行；只有「整批所有外键都是 NULL」时才跳过探测（本数据仅 row 6 为 NULL 外键）。
    # size=1: 7 批，row6 整批 NULL 跳过 -> 6 次；
    # size=2: 批 [5,6] 仍含非 NULL 键 40（无匹配但照探测），4 批全发 -> 4 次；
    # size=3: 批 [4,5,6] 含键 30/40 -> 3 次；size=7/1000: 单批 -> 1 次。
    check(
        "内表批量查询数 = 含非NULL键的批数（单来源）",
        observed == [(1, 7, 6), (2, 4, 4), (3, 3, 3), (7, 1, 1), (1000, 1, 1)],
        str(observed),
    )
    check(
        "外行恒为 7 但查询数随批数变化（非 N+1）",
        len({q for _, _, q in observed}) > 1 and observed[-1][2] == 1,
        str(observed),
    )


# --------------------------------------------------------------------------- #
# 3. 不相关内表行压力：不扩大本批返回内行数
# --------------------------------------------------------------------------- #
def test_irrelevant_keys(tmp: Path, main_artifact: dict):
    print("\n[3] 加入大量不相关内表键：本批返回内行数与结果均不变")
    base_db = fresh_db(tmp, "stress-base")
    stress_db = tmp / "stress.db"
    shutil.copy(base_db, stress_db)
    conn = sqlite3.connect(stress_db)
    # 500 个本批永远不会探测的客户键，每个 3 行，共 1500 行噪音。
    conn.executemany(
        "INSERT INTO payments(payment_id, customer_id, code, amount) VALUES (?, ?, ?, ?)",
        [(1000 + i, 1000 + i, (i % 5), 1) for i in range(500)],
    )
    conn.commit()
    conn.close()

    out = tmp / "stress-results.json"
    _, _, stats, rows = run_engine(stress_db, PLAN_PATH, out)
    base_stats = main_artifact["stats"]
    base_rows = main_artifact["rows"]

    check("噪音数据后结果 JSON 与基线完全相同", rows == base_rows)
    check(
        "返回内行总数不随不相关行增长",
        stats.counters["inner_batch_queries"]
        == base_stats.counters["inner_batch_queries"],
    )
    noisy = sum(s.inner_rows_returned for s in stats.sources)
    clean = sum(s.inner_rows_returned for s in base_stats.sources)
    check("inner_rows_returned 保持基线值", noisy == clean, f"{noisy} vs {clean}")

    # 逐批返回行数与基线逐批完全相同（噪音键从未被探测）。
    def per_batch_log(stats_obj):
        return {
            (s.inner_table, log["batch_index"]): log["rows_returned"]
            for s in stats_obj.sources
            for log in s.batch_log
        }

    check(
        "逐批 rows_returned 与基线逐批相同",
        per_batch_log(stats) == per_batch_log(base_stats),
        f"{per_batch_log(stats)} vs {per_batch_log(base_stats)}",
    )
    check(
        "每批 distinct 键集合也与基线一致",
        [(s.inner_table, log["batch_index"], log["distinct_keys"])
         for s in stats.sources for log in s.batch_log]
        == [(s.inner_table, log["batch_index"], log["distinct_keys"])
            for s in base_stats.sources for log in s.batch_log],
    )


# --------------------------------------------------------------------------- #
# 4. 标量 plan：多行错误语义、身份信息、不发布部分结果
# --------------------------------------------------------------------------- #
def test_scalar_error(tmp: Path):
    print("\n[4] 标量子查询多行：具体身份报错，且不发布部分结果")
    db = fresh_db(tmp, "scalar")
    out = tmp / "scalar-results.json"

    schema = load_schema(SCHEMA_PATH)
    plan = load_plan(SCALAR_PLAN_PATH, schema)
    raised = None
    try:
        execute_plan(db, plan, schema, out)
    except CardinalityError as exc:
        raised = exc
    check("多行标量抛出 CardinalityError", raised is not None)
    if raised:
        check("错误指向具体外行 row_id=7", raised.outer_row_id == 7, str(raised))
        check("错误列出全部命中内行身份(flags 3,4)", raised.inner_row_ids == [3, 4],
              str(raised.inner_row_ids))
        check("候选计数为 2 而非任选首行", raised.candidate_count == 2)
    check("错误路径不创建结果文件（不发布部分结果）", not out.exists())

    # 独立候选计数 oracle 逐行核查：与执行器的零行/单行/多行判定完全一致。
    _, _, expected = oracle_all(db, SCALAR_PLAN_PATH)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    outer = conn.execute(
        "SELECT * FROM outer_rows ORDER BY row_id"
    ).fetchall()
    oracle_counts = [(r["row_id"], r["flag"]["_scalar_count"]) for r in expected]
    check(
        # row1 与 row2 是重复外键 10，各自独立命中同一行（外行不折叠）；
        # row3 命中单行但内值为 NULL；row7 两行触发错误语义。
        "oracle 计数锚点: row1/2/3=1, row4/5/6=0, row7=2",
        oracle_counts == [(1, 1), (2, 1), (3, 1), (4, 0), (5, 0), (6, 0), (7, 2)],
        str(oracle_counts),
    )

    # 零行 -> NULL、单行 -> 该值（含 NULL）：删掉冲突行后应成功并与锚点一致。
    fixed_db = tmp / "scalar-fixed.db"
    shutil.copy(db, fixed_db)
    fix_conn = sqlite3.connect(fixed_db)
    fix_conn.execute("DELETE FROM flags WHERE flag_id = 4")  # row7 只剩 flag_id=3 'b'
    fix_conn.commit()
    fix_conn.close()
    fixed_out = tmp / "scalar-fixed-results.json"
    _, _, _, fixed_rows = run_engine(fixed_db, SCALAR_PLAN_PATH, fixed_out)
    flag_by_id = {r["row_id"]: r["flag"] for r in fixed_rows}
    check(
        # row1/2 重复外键各自得 'ok'（不折叠）；row3 单行内值 NULL -> NULL；
        # row4/5 零行 -> NULL；row6 外键 NULL -> NULL；row7 修复后单行 'a'。
        "零行NULL/单行取值（重复外键不折叠、内列NULL row3）正确",
        {k: flag_by_id[k] for k in (1, 2, 3, 4, 5, 6, 7)}
        == {1: "ok", 2: "ok", 3: None, 4: None, 5: None, 6: None, 7: "a"},
        str(flag_by_id),
    )


# --------------------------------------------------------------------------- #
# 5. 三值逻辑纯函数：空候选 / 单 NULL / 多候选显式区分
# --------------------------------------------------------------------------- #
def test_predicate_units():
    print("\n[5] 三值逻辑单元：空候选、单个 NULL、多个候选分支")
    T, F, U = True, False, None
    cases_in = [
        # (外值, 候选 list, 期望 IN)
        (7, [], F),                 # 空集合一律 FALSE
        (None, [], F),              # NULL IN 空集 也是 FALSE
        (7, [None], U),             # 非空仅 NULL -> UNKNOWN
        (None, [None], U),          # 外 NULL + NULL 候选 -> UNKNOWN
        (None, [1, 2], U),          # 外 NULL 永远 UNKNOWN（候选非空）
        (7, [7], T),                # 等值命中
        (7, [None, 7], T),          # 先遇 NULL 但等值仍 TRUE
        (8, [7, None], U),          # 无等值但涉及 NULL -> UNKNOWN
        (8, [7, 9], F),             # 全非空不命中 -> FALSE
        (8, [8, 8], T),             # 重复值不折叠也能命中
    ]
    bad = [
        (v, c, predicates.in_result(v, c), want)
        for v, c, want in cases_in
        if predicates.in_result(v, c) != want
    ]
    check("in_result 10 个分支全部正确", not bad, str(bad))

    not_in_bad = [
        (v, c, predicates.not_in_result(v, c), want)
        for v, c, want in cases_in
        if predicates.not_in_result(v, c) != (None if want is None else not want)
    ]
    check("not_in_result 按三值取反全部正确", not not_in_bad, str(not_in_bad))

    check("exists 忽略内列 NULL", predicates.exists_result([(1, None)]) is True)
    check("exists 空候选 FALSE", predicates.exists_result([]) is False)
    check("scalar 零行 NULL", predicates.scalar_result([]) is None)
    check("scalar 单行返回该值", predicates.scalar_result([None]) is None)
    check("scalar 单行普通值", predicates.scalar_result(["x"]) == "x")
    try:
        predicates.scalar_result([1, 2])
        scalar_bad = True
    except predicates._ScalarCardinality:
        scalar_bad = False
    check("scalar 多行抛错而非取首行", not scalar_bad)


# --------------------------------------------------------------------------- #
# 6. 白名单 / 结构校验
# --------------------------------------------------------------------------- #
def test_whitelist(tmp: Path):
    print("\n[6] schema 白名单与 plan 结构校验")
    schema = load_schema(SCHEMA_PATH)
    good = load_plan(PLAN_PATH, schema)

    def reject(mutate, label):
        data = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        mutate(data)
        p = tmp / f"bad-{label}.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        try:
            load_plan(p, schema)
            check(label, False, "应当拒绝")
        except PlanError:
            check(f"{label} 被拒绝", True)

    reject(lambda d: d.__setitem__("outer_table", "payments; DROP TABLE flags"), "未知表名")
    reject(lambda d: d["expressions"][0].__setitem__("inner_table", "ghost"), "未知内表")
    reject(lambda d: d["expressions"][0]["correlate"].__setitem__("inner", "row_id;--"), "未知列名")
    reject(lambda d: d["expressions"][0].__setitem__("op", "union_select"), "非法 op")
    reject(lambda d: d.__setitem__("batch_size", 0), "batch_size=0")
    reject(lambda d: d.__setitem__("sneaky", 1), "未知顶层键")
    reject(lambda d: d["expressions"][0].__setitem__("as", "probe"), "表达式名撞原列")
    check("合法 plan 正常加载", good.outer_table == "outer_rows")
    check("内表来源数 <= 2", len(good.inner_tables) <= 2)


# --------------------------------------------------------------------------- #
# 7. CLI：退出码与错误不发布端到端
# --------------------------------------------------------------------------- #
def test_cli(tmp: Path):
    print("\n[7] run.py CLI 端到端退出码 / 原子发布")
    import subprocess

    db = fresh_db(tmp, "cli")
    ok_out, ok_stats = tmp / "cli-ok.json", tmp / "cli-ok.stats.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), str(db), str(PLAN_PATH),
         str(SCHEMA_PATH), str(ok_out), str(ok_stats)],
        capture_output=True, text=True,
    )
    check("成功退出码 0 且产物存在", proc.returncode == 0 and ok_out.exists() and ok_stats.exists(),
          proc.stderr)

    bad_out, bad_stats = tmp / "cli-bad.json", tmp / "cli-bad.stats.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), str(db), str(SCALAR_PLAN_PATH),
         str(SCHEMA_PATH), str(bad_out), str(bad_stats)],
        capture_output=True, text=True,
    )
    check("标量多行退出码 3", proc.returncode == 3, proc.stderr)
    check("失败时无结果文件、无统计文件", not bad_out.exists() and not bad_stats.exists())
    check("stderr 含具体外行/内行身份", "row_id=7" in proc.stderr and "3, 4" in proc.stderr,
          proc.stderr)

    ghost = tmp / "ghost-plan.json"
    ghost.write_text(json.dumps({"outer_table": "nope"}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), str(db), str(ghost),
         str(SCHEMA_PATH), str(tmp / "x.json"), str(tmp / "y.json")],
        capture_output=True, text=True,
    )
    check("plan 非法退出码 2", proc.returncode == 2, proc.stderr)


def test_empty_tables(tmp: Path):
    print("\n[8] 空外表 / 空内表边界")
    schema = load_schema(SCHEMA_PATH)
    plan = load_plan(PLAN_PATH, schema)

    empty_outer = tmp / "empty-outer.db"
    c = sqlite3.connect(empty_outer)
    c.executescript(SEED_PATH.read_text(encoding="utf-8"))
    c.execute("DELETE FROM outer_rows")
    c.commit(); c.close()
    out = tmp / "empty-outer.json"
    stats = execute_plan(empty_outer, plan, schema, out)
    rows = json.loads(out.read_text(encoding="utf-8"))
    check("空外表 -> 空结果、0 批、0 内表查询", rows == [] and stats.batches == 0
          and stats.counters["inner_batch_queries"] == 0)

    empty_inner = tmp / "empty-inner.db"
    c = sqlite3.connect(empty_inner)
    c.executescript(SEED_PATH.read_text(encoding="utf-8"))
    c.execute("DELETE FROM payments")
    c.commit(); c.close()
    out = tmp / "empty-inner.json"
    _, _, stats2, rows2 = run_engine(empty_inner, PLAN_PATH, out)
    # 内表全空：每条外行 EXISTS FALSE、IN FALSE（空集合）、NOT IN TRUE。
    bad = [r for r in rows2 if not (
        r["has_payments"] is False and r["in_codes"] is False and r["not_in_codes"] is True
    )]
    check("空内表 -> EXISTS FALSE / IN FALSE / NOT IN TRUE 全行成立", not bad)
    # 仍按批探测（键非 NULL），只是取回 0 行；不相关全表行不存在时计数为 0。
    check("空内表 inner_rows_returned=0",
          sum(s.inner_rows_returned for s in stats2.sources) == 0)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        artifact = test_oracle_alignment(tmp)
        test_batch_invariance(tmp)
        test_irrelevant_keys(tmp, artifact)
        test_scalar_error(tmp)
        test_predicate_units()
        test_whitelist(tmp)
        test_cli(tmp)
        test_empty_tables(tmp)

    print(f"\n==== {len(PASSED)} passed, {len(FAILED)} failed ====")
    if FAILED:
        for item in FAILED:
            print("FAILED:", item)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # pragma: no cover
        traceback.print_exc()
        raise SystemExit(2)
