"""load:复核计划新鲜度后,按计划分组批量 COPY 到真实叶表,单事务提交。

- 只加载计划已接受的行(行内容取自计划文件,输入文件仅用于重算摘要核对)。
- load 前在同一事务内只读重查目录,重算指纹;目录或输入已变则拒绝旧计划。
- 任何 COPY 错误(如 CHECK 约束)使整批回滚;随后另起仅用于诊断的事务,
  对失败批次逐行重放(全部回滚,不落任何数据),以报告错误叶与输入行。
  诊断重放是 load 错误报告的一部分,与 plan 的只读路由无关。
"""

from __future__ import annotations

import json

import psycopg
from psycopg import sql

from .catalog import load_partition_tree
from .errors import LoadFailed, StalePlan, UsageError
from .planner import read_input


def load_plan_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except OSError as e:
        raise UsageError(f"无法读取计划文件 {path!r}: {e}")
    except json.JSONDecodeError as e:
        raise UsageError(f"计划文件 {path!r} 不是合法 JSON: {e}")
    if plan.get("plan_format") != 1 or plan.get("tool") != "routecopy":
        raise UsageError(f"计划文件 {path!r} 格式不受支持")
    return plan


def diff_catalog(plan: dict, tree) -> list:
    """比较计划中的目录快照与当前目录,返回人类可读的差异列表。"""
    diffs = []
    old_nodes = {n["oid"]: n for n in plan["partitions"]}
    new_nodes = {n["oid"]: n for n in tree.canonical_nodes}
    for oid in sorted(old_nodes.keys() - new_nodes.keys()):
        diffs.append(f"分区消失: oid={oid} {old_nodes[oid]['name']}")
    for oid in sorted(new_nodes.keys() - old_nodes.keys()):
        diffs.append(f"新增分区: oid={oid} {new_nodes[oid]['name']}")
    for oid in sorted(old_nodes.keys() & new_nodes.keys()):
        o, n = old_nodes[oid], new_nodes[oid]
        if o != n:
            changes = []
            for field in ("name", "level", "parent_oid", "relkind", "strategy", "key", "bound"):
                if o.get(field) != n.get(field):
                    changes.append(f"{field}: {json.dumps(o.get(field), ensure_ascii=False)} -> "
                                   f"{json.dumps(n.get(field), ensure_ascii=False)}")
            diffs.append(f"分区变更: oid={oid} {o['name']}: " + "; ".join(changes))
    if plan["columns"] != tree.canonical_columns:
        diffs.append("父表列定义已变化")
    return diffs


def recheck(cur, plan: dict, input_path: str) -> dict:
    """load 前的只读复核:输入摘要 + 目录指纹。返回复核信息;失败抛 StalePlan。"""
    cfg = plan["config"]
    tree = load_partition_tree(cur, plan["parent"], cfg)
    digest, rows = read_input(input_path)
    reasons = []
    if digest != plan["input"]["sha256"]:
        reasons.append(
            f"输入摘要已变: 计划 {plan['input']['sha256']} != 当前 {digest}({input_path})"
        )
    catalog_diffs = diff_catalog(plan, tree)
    if tree.fingerprint != plan["catalog_fingerprint"]:
        reasons.append(
            f"目录指纹已变: 计划 {plan['catalog_fingerprint']} != 当前 {tree.fingerprint}"
        )
        reasons.extend(catalog_diffs)
    if reasons:
        raise StalePlan("计划已过期,拒绝执行旧计划", payload={
            "reasons": reasons,
            "catalog_diffs": catalog_diffs,
            "expected_fingerprint": plan["catalog_fingerprint"],
            "actual_fingerprint": tree.fingerprint,
            "expected_input_sha256": plan["input"]["sha256"],
            "actual_input_sha256": digest,
        })
    return {
        "input_sha256": digest,
        "input_rows": len(rows),
        "catalog_fingerprint": tree.fingerprint,
        "recheck": "ok",
    }


def _copy_stmt(leaf_qname: str, columns: list):
    schema, name = leaf_qname.split(".", 1)
    return sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(schema, name),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )


def _row_tuple(plan: dict, row: dict) -> tuple:
    return tuple(row.get(c["name"]) for c in plan["columns"])


def _diag_of(exc: psycopg.Error) -> dict:
    d = getattr(exc, "diag", None)
    out = {"sqlstate": exc.sqlstate}
    for attr in ("message_primary", "constraint_name", "table_name", "column_name", "context"):
        val = getattr(d, attr, None) if d else None
        if val:
            out[attr] = val
    return out


def run_load(conn, plan: dict, input_path: str) -> dict:
    """执行 load。成功返回提交报告;数据库错误抛 LoadFailed(已回滚并附诊断)。"""
    columns = [c["name"] for c in plan["columns"]]
    batch_rows = int(plan["config"]["batch_rows"])

    # 按计划顺序分组到叶
    groups = []  # [(leaf, leaf_oid, [accepted_entry, ...])]
    by_leaf = {}
    for a in plan["accepted"]:
        if a["leaf"] not in by_leaf:
            by_leaf[a["leaf"]] = (len(groups), a["leaf_oid"])
            groups.append((a["leaf"], a["leaf_oid"], []))
        groups[by_leaf[a["leaf"]][0]][2].append(a)

    recheck_info = {}
    leaves_report = []
    total = 0
    fail_ctx = {}  # COPY 失败现场

    try:
        with conn.transaction():  # 单事务:只读复核 + 全部 COPY,异常即整批回滚(连接保持打开)
            with conn.cursor() as cur:
                recheck_info = recheck(cur, plan, input_path)
                for leaf, leaf_oid, entries in groups:
                    stmt = _copy_stmt(leaf, columns)
                    batches = 0
                    for i in range(0, len(entries), batch_rows):
                        chunk = entries[i:i + batch_rows]
                        fail_ctx = {"leaf": leaf, "leaf_oid": leaf_oid,
                                    "batch_index": batches, "chunk": chunk}
                        with cur.copy(stmt) as copy:
                            for e in chunk:
                                copy.write_row(_row_tuple(plan, e["row"]))
                        batches += 1
                        total += len(chunk)
                    leaves_report.append({
                        "leaf": leaf, "leaf_oid": leaf_oid,
                        "batches": batches, "rows": len(entries),
                    })
                    fail_ctx = {}
    except StalePlan:
        raise
    except psycopg.Error as e:
        # 事务已被 with conn 回滚:整批 0 行落库
        diagnostics = _diagnose(conn, plan, columns, fail_ctx)
        raise LoadFailed(
            f"COPY 到 {fail_ctx.get('leaf')} 失败,本次 load 已整批回滚: {e}",
            payload={
                "error": _diag_of(e),
                "failed_leaf": fail_ctx.get("leaf"),
                "failed_leaf_oid": fail_ctx.get("leaf_oid"),
                "failed_batch_index": fail_ctx.get("batch_index"),
                "diagnostics": diagnostics,
            },
        ) from e

    return {
        "status": "committed",
        "parent": plan["parent"],
        "plan_fingerprint": plan["catalog_fingerprint"],
        "recheck": recheck_info,
        "leaves": leaves_report,
        "total_rows": total,
        "transaction": "单事务:只读复核 + 全部 COPY,一次提交",
    }


def _diagnose(conn, plan: dict, columns: list, fail_ctx: dict) -> dict:
    """失败批次逐行重放以定位问题行。独立事务,全部回滚,不落任何数据。"""
    chunk = fail_ctx.get("chunk") or []
    leaf = fail_ctx.get("leaf")
    if not chunk or not leaf:
        return {"note": "无失败批次上下文", "offending_rows": [], "rolled_back": True}
    stmt = _copy_stmt(leaf, columns)
    offending = []
    with conn.cursor() as cur:
        for e in chunk:
            cur.execute("SAVEPOINT routecopy_diag")
            try:
                with cur.copy(stmt) as copy:
                    copy.write_row(_row_tuple(plan, e["row"]))
                cur.execute("RELEASE SAVEPOINT routecopy_diag")
            except psycopg.Error as e2:
                cur.execute("ROLLBACK TO SAVEPOINT routecopy_diag")
                offending.append({
                    "line": e["line"], "row": e["row"], **_diag_of(e2),
                })
    conn.rollback()  # 诊断事务整体回滚:重放成功的行也不落库
    return {
        "note": "诊断在独立事务内逐行重放失败批次并全部回滚,数据库净变化为 0",
        "leaf": leaf,
        "batch_size": len(chunk),
        "offending_rows": offending,
        "rolled_back": True,
    }


def render_load_md(report: dict) -> str:
    L = ["# load 报告", ""]
    L.append(f"- 父表: `{report.get('parent')}`")
    L.append(f"- 状态: **{report['status']}**")
    if report["status"] == "committed":
        L.append(f"- 目录指纹复核: `{report['recheck']['catalog_fingerprint']}`(与计划一致)")
        L.append(f"- 输入摘要复核: `{report['recheck']['input_sha256']}`(与计划一致)")
        L.append(f"- 事务: {report['transaction']}")
        L.append(f"- 总行数: {report['total_rows']}")
        L.append("")
        L.append("| 叶分区 | OID | COPY 批次数 | 行数 |")
        L.append("|---|---|---|---|")
        for leaf in report["leaves"]:
            L.append(f"| `{leaf['leaf']}` | {leaf['leaf_oid']} | {leaf['batches']} | {leaf['rows']} |")
    elif report["status"] == "stale_rejected":
        L.append("- 旧计划被拒绝,未写入任何行。原因:")
        for r in report.get("reasons", []):
            L.append(f"  - {r}")
    elif report["status"] == "rolled_back":
        L.append(f"- 失败叶: `{report.get('failed_leaf')}`(oid={report.get('failed_leaf_oid')}),"
                 f"批次 #{report.get('failed_batch_index')}")
        err = report.get("error", {})
        L.append(f"- 数据库错误: [{err.get('sqlstate')}] {err.get('message_primary')}")
        if err.get("constraint_name"):
            L.append(f"- 约束: `{err['constraint_name']}`(表 {err.get('table_name')})")
        L.append("- 本次 load 已整批回滚,0 行落库;未把约束失败伪装成路由无匹配。")
        diag = report.get("diagnostics", {})
        L.append(f"- 诊断: {diag.get('note')}")
        for o in diag.get("offending_rows", []):
            L.append(f"  - 输入行 {o['line']}: `{json.dumps(o['row'], ensure_ascii=False)}`"
                     f" → [{o.get('sqlstate')}] {o.get('message_primary')}")
    L.append("")
    return "\n".join(L) + "\n"
