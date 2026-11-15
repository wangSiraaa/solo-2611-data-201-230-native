"""根据 fixtures/seed.sql 建立真实小数据库，并做执行前静态校验。

步骤：
1. 在给定新路径上执行 seed.sql 建表插数（SQL 仅来自受信任的 fixture 文件，
   不从 plan 解析或拼接任意 SQL）；
2. 用 PRAGMA table_info 交叉核对 fixtures/schema.json 白名单与真实表结构；
3. 用执行器同一套校验器校验 fixtures 下的 typed JSON plan，
   表名 / 列名越界在建库阶段即失败；
4. 为每张内表的关联列建索引（仅辅助参数化 IN 探测，不改变语义）。

用法: python prepare.py DB_PATH [SCHEMA_PATH ...PLAN_PATHS]
默认参数复现 fixtures 布局。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_SCHEMA = ROOT / "fixtures" / "schema.json"
DEFAULT_PLANS = [
    ROOT / "fixtures" / "plan.json",
    ROOT / "fixtures" / "scalar-plan.json",
    ROOT / "fixtures" / "two-source-plan.json",
]
SEED = ROOT / "fixtures" / "seed.sql"

sys.path.insert(0, str(ROOT))
from batchjoin.plan import PlanError, load_plan, load_schema  # noqa: E402

_LOGICAL_TYPES = {
    "INTEGER": "integer",
    "TEXT": "text",
}


def _check_schema_matches_db(conn: sqlite3.Connection, schema_path: Path) -> None:
    schema = load_schema(schema_path)
    for table, declared in schema.tables.items():
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            raise PlanError(f"schema 声明了表 {table!r}，但数据库中不存在")
        actual = {row[1]: (row[2].upper(), bool(row[3])) for row in info}
        if set(actual) != set(declared):
            raise PlanError(
                f"表 {table!r} 列不一致: schema={sorted(declared)} db={sorted(actual)}"
            )
        for column, logical in declared.items():
            base, _nullable = actual[column]
            expected_base = logical.removesuffix("?")
            if _LOGICAL_TYPES.get(base) != expected_base:
                raise PlanError(
                    f"表 {table!r} 列 {column!r} 类型不符: "
                    f"schema={logical} db={base}"
                )
    return schema


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]) if len(argv) > 1 else ROOT / "output" / "report.db"
    schema_path = Path(argv[2]) if len(argv) > 2 else DEFAULT_SCHEMA
    plan_paths = [Path(p) for p in argv[3:]] if len(argv) > 3 else DEFAULT_PLANS

    if db_path.exists():
        raise SystemExit(f"拒绝覆盖已存在的数据库: {db_path}（请选择新路径）")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SEED.read_text(encoding="utf-8"))
        schema = _check_schema_matches_db(conn, schema_path)

        # plan 提前校验：任何越权名称 / 非法结构在这里失败，不进入执行阶段。
        for plan_path in plan_paths:
            load_plan(plan_path, schema)
            print(f"plan 校验通过: {plan_path}")

        # 关联列索引：让参数化 IN 探测走索引而非全表扫描；fixture 很小，
        # 主要作用是把「受限范围探测」的访问路径固定下来。
        indexed: set[tuple[str, str]] = set()
        for plan_path in plan_paths:
            plan = load_plan(plan_path, schema)
            for expr in plan.expressions:
                key = (expr.inner_table, expr.correlate.inner)
                if key in indexed:
                    continue
                table, column = key
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS '
                    f'"idx_{table}_{column}" ON "{table}" ("{column}")'
                )
                indexed.add(key)
        conn.commit()
    finally:
        conn.close()

    print(f"数据库已建立: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
