"""verify:独立核对。

1. readback:只读 SELECT tableoid, * FROM 父表,与计划接受集逐字段比对
   (验证 COPY 落库的行与字段完整)。
2. oracle:对每条计划接受的行,向父表 INSERT ... RETURNING tableoid,
   以数据库真实路由为 oracle 核对计划叶;整个验证事务最后回滚,不落数据。
3. 对“无目的地”拒绝行,尝试 INSERT 父表,必须报 no partition of relation,
   证明数据库同样无法路由;类型不符拒绝行属客户端严格性,不参与该核对。
"""

from __future__ import annotations

import json
from collections import Counter

import psycopg
from psycopg import sql

from .planner import R_NO_MATCH


def _qname_parts(qname: str):
    return tuple(qname.split(".", 1))


def _row_key(columns, values):
    return tuple(values)


def run_verify(conn, plan: dict, expect: str) -> dict:
    columns = [c["name"] for c in plan["columns"]]
    parent = plan["parent"]
    pschema, pname = _qname_parts(parent)
    report = {"parent": parent, "expect": expect, "checks": {}, "ok": True}

    # 验证事务:readback 为只读;oracle 的 INSERT 在 finally 中整体回滚,不落数据。
    try:
        with conn.cursor() as cur:
            # ---- 1. readback -------------------------------------------------
            if expect in ("loaded", "empty"):
                sel = sql.SQL("SELECT t.tableoid::regclass::text, t.* FROM {} t").format(
                    sql.Identifier(pschema, pname)
                )
                cur.execute(sel)
                db_rows = Counter()
                colnames = [d.name for d in cur.description]
                for rec in cur.fetchall():
                    leaf = rec[0]
                    vals = tuple(rec[1:])
                    db_rows[(leaf, vals)] += 1
                expected = Counter()
                for a in plan["accepted"]:
                    vals = tuple(a["row"].get(c) for c in columns)
                    expected[(a["leaf"], vals)] += 1
                if expect == "empty":
                    expected = Counter()
                missing = expected - db_rows
                unexpected = db_rows - expected
                ok = not missing and not unexpected
                report["checks"]["readback"] = {
                    "ok": ok,
                    "db_rows": sum(db_rows.values()),
                    "expected_rows": sum(expected.values()),
                    "missing": [
                        {"leaf": k[0], "values": dict(zip(colnames[1:], k[1])), "count": v}
                        for k, v in sorted(missing.items(), key=str)
                    ],
                    "unexpected": [
                        {"leaf": k[0], "values": dict(zip(colnames[1:], k[1])), "count": v}
                        for k, v in sorted(unexpected.items(), key=str)
                    ],
                }
                report["ok"] &= ok

            # ---- 2. oracle:父表真实路由核对每条接受行 -------------------------
            col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
            ret_idents = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            ins = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING tableoid::regclass::text, {}").format(
                sql.Identifier(pschema, pname), col_idents, placeholders, ret_idents
            )
            oracle = []
            for a in plan["accepted"]:
                vals = tuple(a["row"].get(c) for c in columns)
                cur.execute("SAVEPOINT routecopy_verify")
                try:
                    got = cur.execute(ins, vals).fetchone()
                    cur.execute("RELEASE SAVEPOINT routecopy_verify")
                    actual_leaf, actual_vals = got[0], tuple(got[1:])
                    fields_ok = actual_vals == vals
                    leaf_ok = actual_leaf == a["leaf"]
                    oracle.append({
                        "line": a["line"], "planned_leaf": a["leaf"],
                        "actual_leaf": actual_leaf, "leaf_ok": leaf_ok,
                        "fields_ok": fields_ok, "ok": leaf_ok and fields_ok,
                    })
                except psycopg.Error as e:
                    cur.execute("ROLLBACK TO SAVEPOINT routecopy_verify")
                    msg = e.diag.message_primary if e.diag else str(e)
                    tbl = e.diag.table_name if e.diag else None
                    # 完整性约束(23 类)且非“无分区”错误:说明行已被路由,
                    # 触发约束的关系名即为真实叶,可与计划叶比对。
                    if (e.sqlstate or "").startswith("23") and tbl and "no partition of relation" not in (msg or ""):
                        leaf_ok = tbl == a["leaf"].split(".", 1)[1]
                        oracle.append({
                            "line": a["line"], "planned_leaf": a["leaf"],
                            "actual_leaf": tbl, "leaf_ok": leaf_ok, "fields_ok": None,
                            "ok": leaf_ok,
                            "note": f"行已被路由到 {tbl}(与计划{'一致' if leaf_ok else '不一致'}),"
                                    f"随后触发约束: [{e.sqlstate}] {msg}",
                        })
                    else:
                        oracle.append({
                            "line": a["line"], "planned_leaf": a["leaf"],
                            "actual_leaf": None, "leaf_ok": False, "fields_ok": False,
                            "ok": False, "error": f"[{e.sqlstate}] {msg}",
                        })
            ok = all(o["ok"] for o in oracle)
            report["checks"]["oracle_accepted"] = {"ok": ok, "rows": oracle}
            report["ok"] &= ok

            # ---- 3. 拒绝行:数据库同样不能路由 --------------------------------
            rej = []
            for r in plan["rejected"]:
                if r["reason"] != R_NO_MATCH:
                    rej.append({"line": r["line"], "reason": r["reason"],
                                "check": "skipped",
                                "note": "类型/格式问题属客户端严格性,DB 可能强转,不参与路由核对"})
                    continue
                vals = tuple(r["row"].get(c) for c in columns)
                cur.execute("SAVEPOINT routecopy_verify")
                try:
                    cur.execute(ins, vals)
                    cur.execute("RELEASE SAVEPOINT routecopy_verify")
                    rej.append({"line": r["line"], "reason": r["reason"],
                                "check": "failed",
                                "note": "数据库竟然路由了我们拒绝的行!"})
                except psycopg.Error as e:
                    cur.execute("ROLLBACK TO SAVEPOINT routecopy_verify")
                    msg = e.diag.message_primary if e.diag else str(e)
                    is_no_partition = "no partition of relation" in (msg or "")
                    rej.append({"line": r["line"], "reason": r["reason"],
                                "check": "ok" if is_no_partition else "failed",
                                "db_error": f"[{e.sqlstate}] {msg}",
                                "note": "数据库确认无法路由" if is_no_partition else "数据库报了非预期错误"})
            ok = all(x["check"] in ("ok", "skipped") for x in rej)
            report["checks"]["oracle_rejected"] = {"ok": ok, "rows": rej}
            report["ok"] &= ok
    finally:
        conn.rollback()  # oracle 的 INSERT 全部回滚:验证不落任何数据
    report["transaction_note"] = "oracle 的 INSERT 均在验证事务内完成并整体回滚,数据库净变化为 0"
    report["status"] = "ok" if report["ok"] else "failed"
    return report


def render_verify_md(report: dict) -> str:
    L = [f"# 独立验证报告 — {report['parent']}", ""]
    L.append(f"- 状态: **{report['status']}**")
    L.append(f"- 说明: {report['transaction_note']}")
    L.append("")
    rb = report["checks"].get("readback")
    if rb is not None:
        L.append(f"## 落库核对(readback,expect={report['expect']})")
        L.append("")
        L.append(f"- 结果: {'✓ 一致' if rb['ok'] else '✗ 不一致'};库中 {rb['db_rows']} 行,期望 {rb['expected_rows']} 行")
        for item in rb["missing"]:
            L.append(f"- 缺失: `{item['leaf']}` {json.dumps(item['values'], ensure_ascii=False)} ×{item['count']}")
        for item in rb["unexpected"]:
            L.append(f"- 多出: `{item['leaf']}` {json.dumps(item['values'], ensure_ascii=False)} ×{item['count']}")
        L.append("")
    oc = report["checks"].get("oracle_accepted")
    if oc is not None:
        L.append("## oracle:父表真实路由 vs 计划叶(接受行)")
        L.append("")
        L.append("| 行 | 计划叶 | oracle tableoid | 字段一致 | 结果 |")
        L.append("|---|---|---|---|---|")
        for o in oc["rows"]:
            L.append(f"| {o['line']} | `{o['planned_leaf']}` | `{o.get('actual_leaf')}` "
                     f"| {'✓' if o['fields_ok'] else ('—' if o['fields_ok'] is None else '✗')} | {'✓' if o['ok'] else '✗'} |")
            if o.get("note"):
                L.append(f"| | ↳ {o['note']} | | | |")
        L.append("")
    orj = report["checks"].get("oracle_rejected")
    if orj is not None:
        L.append("## 拒绝行的数据库路由核对")
        L.append("")
        for x in orj["rows"]:
            if x["check"] == "skipped":
                L.append(f"- 行 {x['line']}({x['reason']}):跳过 — {x['note']}")
            elif x["check"] == "ok":
                L.append(f"- 行 {x['line']}({x['reason']}):✓ {x['note']} — `{x.get('db_error')}`")
            else:
                L.append(f"- 行 {x['line']}({x['reason']}):✗ {x['note']} {x.get('db_error','')}")
        L.append("")
    return "\n".join(L) + "\n"
