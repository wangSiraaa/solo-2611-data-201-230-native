"""plan:只读路由计划。

对输入 JSONL 逐行模拟 PostgreSQL 的分区路由语义(纯内存计算,数据库侧只有
目录 SELECT;连接由 cli 显式置为 READ ONLY),输出:
  - 每行经过的分区、每层比较依据、最终叶
  - 无匹配或类型不符行的拒绝清单(绝不路由到随意挑选的叶)
计划绑定:输入文件 sha256 + 全部涉及分区的 OID/键/边界指纹。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .catalog import INT_RANGES, format_bound, load_partition_tree
from .errors import UsageError

PLAN_FORMAT = 1

# 拒绝原因
R_INVALID_JSON = "invalid_json"
R_NOT_OBJECT = "not_an_object"
R_UNKNOWN_COLUMN = "unknown_column"
R_TYPE_MISMATCH = "type_mismatch"
R_NO_MATCH = "no_matching_partition"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_input(path: str):
    """返回 (sha256_hex, [(lineno, obj_or_none, error_or_none), ...])。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise UsageError(f"无法读取输入文件 {path!r}: {e}")
    digest = sha256_bytes(raw)
    rows = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((lineno, json.loads(line), None))
        except json.JSONDecodeError as e:
            rows.append((lineno, None, f"JSON 解析失败: {e}"))
    return digest, rows


# ---------------------------------------------------------------- 行校验

def validate_row(tree, row):
    """JSON 类型与列类型核对。返回问题列表(空 = 通过)。"""
    problems = []
    cols = {c.name: c for c in tree.columns}
    for k in row:
        if k not in cols:
            problems.append(f"未知列 {k!r}(父表无此列)")
    for c in tree.columns:
        if c.name not in row:
            continue  # 缺失列按 SQL NULL 处理
        v = row[c.name]
        if v is None:
            continue
        if c.kind == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                problems.append(f"列 {c.name}({c.typname})需要 JSON 整数,实际 {type(v).__name__}: {v!r}")
            else:
                lo, hi = INT_RANGES[c.typname]
                if not (lo <= v <= hi):
                    problems.append(f"列 {c.name} 值 {v} 超出 {c.typname} 范围 [{lo},{hi}]")
        elif c.kind == "text":
            if not isinstance(v, str):
                problems.append(f"列 {c.name}(text)需要 JSON 字符串,实际 {type(v).__name__}: {v!r}")
    return problems


# ---------------------------------------------------------------- 比较原语

def _cmp_scalar(a, b, kind):
    """int 按数值;text 按 C collation。

    C collation 逐字节比较 UTF-8 编码;UTF-8 的字节序与码点序一致,
    因此 Python str 的大小比较与 C collation 等价(文本不允许内嵌 NUL)。
    """
    return (a > b) - (a < b)


def cmp_row_to_bound(vals, datums, key):
    """行键元组与 RANGE 边界的词典序比较。调用前保证 vals 无 NULL。

    MINVALUE/MAXVALUE 是无限边界,不等于任何普通整数:
    任何行值都 > MINVALUE、< MAXVALUE。
    返回 >0 表示行在边界之上,<0 表示行在边界之下,0 表示逐列相等。
    """
    for v, d, col in zip(vals, datums, key):
        if d.kind == "min":
            return 1
        if d.kind == "max":
            return -1
        c = _cmp_scalar(v, d.value, col.kind)
        if c:
            return c
    return 0


def explain_cmp(vals, datums, key):
    """返回 (cmp, 说明文字):首个分出胜负的列,用于逐层比较依据。"""
    for v, d, col in zip(vals, datums, key):
        if d.kind == "min":
            return 1, f"列 {col.name}: 下界 MINVALUE 为负无穷,任何值都更大"
        if d.kind == "max":
            return -1, f"列 {col.name}: 上界 MAXVALUE 为正无穷,任何值都更小"
        c = _cmp_scalar(v, d.value, col.kind)
        if c:
            how = ">" if c > 0 else "<"
            return c, f"列 {col.name}: {v!r} {how} 边界值 {d.value!r}"
    return 0, "各列与边界逐列相等"


def _fmt_json(v):
    """JSON 值显示为 SQL 字面量风格:None → NULL。"""
    if v is None:
        return "NULL"
    return repr(v)


def _fmt_vals(key, vals):
    return "(" + ", ".join(f"{c.name}={_fmt_json(v)}" for c, v in zip(key, vals)) + ")"


# ---------------------------------------------------------------- 路由

def route_row(tree, row):
    """返回 (status, leaf_node|None, path, reason, detail)。

    DEFAULT 是同层其他分区覆盖的补集:先评估全部非 DEFAULT 分区,
    都不匹配才落到 DEFAULT;同层无 DEFAULT 则为无目的地。
    """
    path = []
    node = tree.root
    while not node.is_leaf:
        key_cols = [k.name for k in node.key]
        key_vals = {k.name: row.get(k.name) for k in node.key}
        entry = {
            "level": node.level + 1,
            "node": node.qname,
            "node_oid": node.oid,
            "strategy": node.strategy,
            "key_columns": [
                {"name": k.name, "type": k.typname, **({"collation": "C"} if k.kind == "text" else {})}
                for k in node.key
            ],
            "key_values": key_vals,
        }
        non_default = [c for c in node.children if c.bound.kind != "default"]
        default_child = next((c for c in node.children if c.bound.kind == "default"), None)

        if node.strategy == "list":
            col = node.key[0]
            v = key_vals[col.name]
            hit = None
            for child in non_default:
                if v in child.bound.list_values:
                    hit = child
                    break
            cmp_note = "text 按 C collation 逐字节判等" if col.kind == "text" else "整数按数值判等"
            if hit is not None:
                if v is None:
                    entry["decision"] = "null_list"
                    entry["basis"] = (
                        f"LIST 键 {col.name}=NULL:命中 {hit.name} 的 FOR VALUES IN (NULL);"
                        f"LIST 中的 NULL 与 DEFAULT 不同,仅匹配显式 NULL 列表项"
                    )
                else:
                    entry["decision"] = "matched"
                    entry["basis"] = (
                        f"LIST 键 {col.name}={_fmt_json(v)}:命中 {hit.name} 的 "
                        f"{format_bound(hit.bound)}({cmp_note})"
                    )
                entry["next"] = hit.qname
                path.append(entry)
                node = hit
                continue
            if default_child is not None:
                entry["decision"] = "default"
                seen = "、".join(format_bound(c.bound) for c in non_default) or "(无非 DEFAULT 分区)"
                entry["basis"] = (
                    f"LIST 键 {col.name}={_fmt_json(v)}:未命中任何非 DEFAULT 列表({seen};{cmp_note});"
                    f"DEFAULT 分区 {default_child.name} 是同层补集 → 落入 DEFAULT"
                )
                entry["next"] = default_child.qname
                path.append(entry)
                node = default_child
                continue
            entry["decision"] = "no_match"
            seen = "、".join(format_bound(c.bound) for c in non_default) or "(无非 DEFAULT 分区)"
            entry["basis"] = (
                f"LIST 键 {col.name}={_fmt_json(v)}:未命中任何非 DEFAULT 列表({seen};{cmp_note}),"
                f"且同层无 DEFAULT 分区 → 无目的地"
            )
            entry["next"] = None
            path.append(entry)
            return ("rejected", None, path, R_NO_MATCH,
                    f"第 {entry['level']} 层 {node.qname}: 无匹配分区且无 DEFAULT")

        # RANGE
        vals = tuple(key_vals[k.name] for k in node.key)
        if any(v is None for v in vals):
            # PostgreSQL 真实规则:RANGE 键含 NULL 的行只能进入 DEFAULT 分区
            if default_child is not None:
                entry["decision"] = "default"
                entry["basis"] = (
                    f"RANGE 键 {_fmt_vals(node.key, vals)} 含 NULL:按 PostgreSQL 规则,"
                    f"含 NULL 的 RANGE 键不匹配任何 RANGE 边界,只能进入 DEFAULT 分区 "
                    f"{default_child.name}"
                )
                entry["next"] = default_child.qname
                path.append(entry)
                node = default_child
                continue
            entry["decision"] = "no_match"
            entry["basis"] = (
                f"RANGE 键 {_fmt_vals(node.key, vals)} 含 NULL:按 PostgreSQL 规则只能进入 "
                f"DEFAULT 分区,但同层无 DEFAULT → 无目的地"
            )
            entry["next"] = None
            path.append(entry)
            return ("rejected", None, path, R_NO_MATCH,
                    f"第 {entry['level']} 层 {node.qname}: RANGE 键含 NULL 且无 DEFAULT")

        candidates = []
        hit = None
        for child in non_default:
            lo_cmp, lo_note = explain_cmp(vals, child.bound.lower, node.key)
            hi_cmp, hi_note = explain_cmp(vals, child.bound.upper, node.key)
            ok = lo_cmp >= 0 and hi_cmp < 0
            candidates.append({
                "partition": child.qname,
                "bound": format_bound(child.bound),
                "lower_ok": lo_cmp >= 0, "lower_note": lo_note,
                "upper_ok": hi_cmp < 0, "upper_note": hi_note,
                "matched": ok,
            })
            if ok and hit is None:
                hit = child
        entry["candidates"] = candidates
        if hit is not None:
            entry["decision"] = "matched"
            entry["basis"] = (
                f"RANGE 键 {_fmt_vals(node.key, vals)}:按词典序与 {hit.name} 边界 "
                f"{format_bound(hit.bound)} 比较,≥ 下界且 < 上界(左闭右开)→ 命中"
            )
            entry["next"] = hit.qname
            path.append(entry)
            node = hit
            continue
        if default_child is not None:
            entry["decision"] = "default"
            entry["basis"] = (
                f"RANGE 键 {_fmt_vals(node.key, vals)}:逐边界词典序比较后不被任何非 DEFAULT "
                f"分区覆盖(见 candidates),DEFAULT 分区 {default_child.name} 是同层补集 → 落入 DEFAULT"
            )
            entry["next"] = default_child.qname
            path.append(entry)
            node = default_child
            continue
        entry["decision"] = "no_match"
        entry["basis"] = (
            f"RANGE 键 {_fmt_vals(node.key, vals)}:逐边界词典序比较后不被任何非 DEFAULT "
            f"分区覆盖(见 candidates),且同层无 DEFAULT → 无目的地"
        )
        entry["next"] = None
        path.append(entry)
        return ("rejected", None, path, R_NO_MATCH,
                f"第 {entry['level']} 层 {node.qname}: 无 RANGE 分区覆盖且无 DEFAULT")

    return ("accepted", node, path, None, None)


# ---------------------------------------------------------------- 计划构建

def build_plan(cur, parent: str, input_path: str, cfg: dict) -> dict:
    tree = load_partition_tree(cur, parent, cfg)
    digest, raw_rows = read_input(input_path)

    accepted, rejected = [], []
    for lineno, obj, err in raw_rows:
        if err is not None:
            rejected.append({"line": lineno, "row": None, "reason": R_INVALID_JSON, "detail": err, "path": []})
            continue
        if not isinstance(obj, dict):
            rejected.append({"line": lineno, "row": obj, "reason": R_NOT_OBJECT,
                             "detail": "输入行不是 JSON 对象", "path": []})
            continue
        problems = validate_row(tree, obj)
        if problems:
            rejected.append({"line": lineno, "row": obj, "reason": R_TYPE_MISMATCH,
                             "detail": "; ".join(problems), "path": []})
            continue
        status, leaf, path, reason, detail = route_row(tree, obj)
        if status == "accepted":
            accepted.append({
                "line": lineno, "row": obj,
                "leaf": leaf.qname, "leaf_oid": leaf.oid,
                "path": path,
            })
        else:
            rejected.append({"line": lineno, "row": obj, "reason": reason,
                             "detail": detail, "path": path})

    return {
        "plan_format": PLAN_FORMAT,
        "tool": "routecopy",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "parent": tree.root.qname,
        "parent_oid": tree.root.oid,
        "config": {
            "batch_rows": cfg["batch_rows"],
            "supported_types": list(cfg["supported_types"]),
            "max_partition_depth": cfg["max_partition_depth"],
        },
        "input": {"path": input_path, "sha256": digest, "row_count": len(raw_rows)},
        "catalog_fingerprint": tree.fingerprint,
        "columns": tree.canonical_columns,
        "partitions": tree.canonical_nodes,
        "accepted": accepted,
        "rejected": rejected,
        "summary": {
            "total": len(raw_rows),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "leaves": sorted({a["leaf"] for a in accepted}),
        },
    }


# ---------------------------------------------------------------- 报告渲染

def render_plan_md(plan: dict) -> str:
    L = []
    L.append(f"# 只读路由计划报告 — {plan['parent']}")
    L.append("")
    L.append(f"- 生成时间(UTC): {plan['created_utc']}")
    L.append(f"- 输入: `{plan['input']['path']}`(sha256 `{plan['input']['sha256']}`,{plan['input']['row_count']} 行)")
    L.append(f"- 父表 OID: {plan['parent_oid']},目录指纹: `{plan['catalog_fingerprint']}`")
    L.append(f"- 计划只读来源:pg_partition_tree / pg_partitioned_table / pg_attribute / pg_get_expr 的 SELECT")
    L.append("")
    L.append("## 分区树(目录快照,含 OID 与边界)")
    L.append("")
    by_parent = {}
    nodes = {n["oid"]: n for n in plan["partitions"]}
    for n in plan["partitions"]:
        by_parent.setdefault(n["parent_oid"], []).append(n)

    def fmt_key(n):
        if not n["strategy"]:
            return ""
        cols = ", ".join(
            f"{k['name']} {k['type']}" + (' COLLATE "C"' if k.get("collation") else "")
            for k in n["key"]
        )
        return f" — {n['strategy'].upper()}({cols})"

    def walk(oid, depth):
        n = nodes[oid]
        bound = ""
        if n["bound"]:
            b = n["bound"]
            if b["kind"] == "default":
                bound = " = DEFAULT"
            elif b["kind"] == "list":
                bound = " = IN (" + ", ".join(
                    "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else str(v))
                    for v in b["values"]
                ) + ")"
            else:
                def fmt_side(side):
                    return "(" + ", ".join(
                        "MINVALUE" if d["t"] == "min" else "MAXVALUE" if d["t"] == "max" else
                        (f"'{d['v']}'" if isinstance(d["v"], str) else str(d["v"]))
                        for d in side
                    ) + ")"
                bound = f" = FROM {fmt_side(b['lower'])} TO {fmt_side(b['upper'])}"
        L.append("  " * depth + f"- `{n['name']}`(oid={n['oid']}){fmt_key(n)}{bound}")
        for c in sorted(by_parent.get(oid, []), key=lambda x: x["oid"]):
            walk(c["oid"], depth + 1)

    root = [n for n in plan["partitions"] if n["parent_oid"] is None][0]
    walk(root["oid"], 0)
    L.append("")
    L.append(f"## 逐行路由(接受 {plan['summary']['accepted']} / 共 {plan['summary']['total']})")
    L.append("")
    def render_path(path):
        for p in path:
            L.append(f"- 第 {p['level']} 层 `{p['node']}`:{p['basis']}")
            for cand in p.get("candidates", []):
                mark = "✓" if cand["matched"] else "✗"
                L.append(
                    f"  - 候选 `{cand['partition']}` {cand['bound']}:下界 {'✓' if cand['lower_ok'] else '✗'}"
                    f"({cand['lower_note']}),上界 {'✓' if cand['upper_ok'] else '✗'}({cand['upper_note']}){mark}"
                )

    for a in plan["accepted"]:
        L.append(f"### 行 {a['line']} → `{a['leaf']}`(oid={a['leaf_oid']})")
        L.append("")
        L.append("```json")
        L.append(json.dumps(a["row"], ensure_ascii=False, sort_keys=True))
        L.append("```")
        render_path(a["path"])
        L.append("")
    L.append(f"## 拒绝清单({plan['summary']['rejected']} 行)")
    L.append("")
    if not plan["rejected"]:
        L.append("(无)")
    for r in plan["rejected"]:
        L.append(f"### 行 {r['line']} — {r['reason']}")
        L.append("")
        L.append("```json")
        L.append(json.dumps(r["row"], ensure_ascii=False, sort_keys=True))
        L.append("```")
        L.append(f"- 原因: {r['detail']}")
        render_path(r.get("path", []))
        L.append("")
    return "\n".join(L) + "\n"
