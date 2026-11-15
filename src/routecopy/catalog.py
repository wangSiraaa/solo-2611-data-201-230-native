"""只读目录内省:真实分区层次、键列、边界,以及目录指纹。

本模块对数据库只执行 SELECT(plan 阶段连接还会显式置为 READ ONLY)。
不支持的对象一律抛 CatalogUnsupported:
  - HASH 分区、表达式分区键
  - 超过 max_partition_depth 的层次
  - 键列类型不在 config.supported_types 内(整数族 int2/int4/int8、text)
  - text 键未显式使用 C collation
边界文本来自 pg_get_expr(relpartbound, oid),只解析其固定文法
(FOR VALUES IN / FROM .. TO .. / DEFAULT),不是通用 SQL 解析器。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .errors import CatalogUnsupported, UsageError

# config.supported_types 里的逻辑名 -> pg_type.typname
CONFIG_TYPE_MAP = {"smallint": "int2", "integer": "int4", "bigint": "int8", "text": "text"}
INT_RANGES = {
    "int2": (-(2**15), 2**15 - 1),
    "int4": (-(2**31), 2**31 - 1),
    "int8": (-(2**63), 2**63 - 1),
}
STRATEGY_MAP = {"l": "list", "r": "range"}


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class KeyColumn:
    attnum: int
    name: str
    typname: str            # int2 / int4 / int8 / text
    kind: str               # 'int' | 'text'
    collation: str | None   # text 键为 'C';int 键为 None


@dataclass(frozen=True)
class RangeDatum:
    kind: str               # 'min' | 'max' | 'value'
    value: object = None    # kind == 'value' 时为 int 或 str


@dataclass(frozen=True)
class Bound:
    kind: str                       # 'default' | 'list' | 'range'
    list_values: tuple | None = None   # list: 元素为 int | str | None(NULL)
    lower: tuple | None = None         # range: tuple[RangeDatum, ...]
    upper: tuple | None = None


@dataclass
class Node:
    oid: int
    schema: str
    name: str
    relkind: str
    level: int
    parent_oid: int | None
    bound_text: str | None
    bound: Bound | None = None
    strategy: str | None = None      # 分区节点: 'list' | 'range';叶为 None
    key: tuple = ()                  # tuple[KeyColumn, ...]
    default_child_oid: int | None = None
    children: list = field(default_factory=list)

    @property
    def qname(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def is_leaf(self) -> bool:
        return self.strategy is None


@dataclass(frozen=True)
class TableColumn:
    attnum: int
    name: str
    typname: str
    kind: str               # 'int' | 'text'
    notnull: bool


@dataclass
class PartitionTree:
    root: Node
    nodes: dict             # oid -> Node(含根)
    columns: list           # 根表列 list[TableColumn],按 attnum
    fingerprint: str        # sha256 hex
    canonical_nodes: list   # 写入计划的规范化节点条目(按 oid 排序)
    canonical_columns: list

    def leaf_by_oid(self, oid):
        node = self.nodes.get(oid)
        return node if node and node.is_leaf else None


# ---------------------------------------------------------------- 边界解析

_TOKEN = re.compile(
    r"(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+)|(?P<kw>[A-Za-z_][A-Za-z_0-9]*)|(?P<punct>[(),])"
)


class BoundParseError(CatalogUnsupported):
    pass


def _tokenize(text: str):
    toks = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        m = _TOKEN.match(text, i)
        if not m:
            raise BoundParseError(f"无法解析边界表达式片段: {text[i:]!r}(完整边界 {text!r})")
        i = m.end()
        kind = m.lastgroup
        if kind == "str":
            toks.append(("str", m.group("str")[1:-1].replace("''", "'")))
        elif kind == "num":
            toks.append(("num", int(m.group("num"))))
        elif kind == "kw":
            toks.append(("kw", m.group("kw").upper()))
        else:
            toks.append((m.group("punct"), m.group("punct")))
    toks.append(("EOF", None))
    return toks


def _coerce_literal(raw, kind: str, where: str):
    """把边界里的字面量按键列类型规整。PG16 对负整数边界会反解析为带引号形式('-5000')。"""
    if kind == "int":
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and re.fullmatch(r"-?\d+", raw):
            return int(raw)
        raise BoundParseError(f"{where}: 整型键的边界值 {raw!r} 不是整数")
    if kind == "text":
        if isinstance(raw, str):
            return raw
        raise BoundParseError(f"{where}: 文本键的边界值 {raw!r} 不是字符串字面量")
    raise BoundParseError(f"{where}: 未知键类型 {kind!r}")


def parse_bound(text: str, key: tuple) -> Bound:
    """解析 pg_get_expr(relpartbound) 的固定输出文法。key 为该层分区键列。"""
    toks = _tokenize(text)
    pos = 0

    def peek():
        return toks[pos][0]

    def take():
        nonlocal pos
        tok = toks[pos]
        pos += 1
        return tok

    def expect(tok):
        got = take()
        if got[0] != tok:
            raise BoundParseError(f"边界 {text!r}: 期望 {tok!r},实际 {got!r}")
        return got

    def expect_kw(word):
        got = take()
        if got != ("kw", word):
            raise BoundParseError(f"边界 {text!r}: 期望关键字 {word},实际 {got!r}")

    if peek() == "kw" and toks[0] == ("kw", "DEFAULT"):
        take()
        expect("EOF")
        return Bound(kind="default")

    expect_kw("FOR")
    expect_kw("VALUES")

    if peek() == "kw" and toks[pos][1] == "IN":
        take()
        expect("(")
        values = []
        while True:
            tok = take()
            if tok == ("kw", "NULL"):
                values.append(None)
            elif tok[0] == "str":
                values.append(tok[1])
            elif tok[0] == "num":
                values.append(tok[1])
            else:
                raise BoundParseError(f"LIST 边界 {text!r}: 非法列表项 {tok!r}")
            sep = take()
            if sep[0] == ",":
                continue
            if sep[0] == ")":
                break
            raise BoundParseError(f"LIST 边界 {text!r}: 期望 , 或 ),实际 {sep!r}")
        expect("EOF")
        if len(key) != 1:
            raise BoundParseError(f"LIST 边界 {text!r}: 键列数应为 1,实际 {len(key)}")
        kind = key[0].kind
        coerced = tuple(
            None if v is None else _coerce_literal(v, kind, f"LIST 边界 {text!r}")
            for v in values
        )
        return Bound(kind="list", list_values=coerced)

    if peek() == "kw" and toks[pos][1] == "FROM":
        take()

        def parse_datums(side):
            expect("(")
            datums = []
            while True:
                tok = take()
                if tok == ("kw", "MINVALUE"):
                    datums.append(RangeDatum("min"))
                elif tok == ("kw", "MAXVALUE"):
                    datums.append(RangeDatum("max"))
                elif tok == ("kw", "NULL"):
                    raise BoundParseError(f"RANGE 边界 {text!r}: {side} 不允许 NULL")
                elif tok[0] == "str":
                    datums.append(RangeDatum("value", tok[1]))
                elif tok[0] == "num":
                    datums.append(RangeDatum("value", tok[1]))
                else:
                    raise BoundParseError(f"RANGE 边界 {text!r}: {side} 非法项 {tok!r}")
                sep = take()
                if sep[0] == ",":
                    continue
                if sep[0] == ")":
                    break
                raise BoundParseError(f"RANGE 边界 {text!r}: 期望 , 或 ),实际 {sep!r}")
            return datums

        lower = parse_datums("FROM")
        expect_kw("TO")
        upper = parse_datums("TO")
        expect("EOF")
        if len(lower) != len(key) or len(upper) != len(key):
            raise BoundParseError(
                f"RANGE 边界 {text!r}: 维度 {len(lower)}/{len(upper)} 与键列数 {len(key)} 不符"
            )

        def coerce_side(datums, side):
            out = []
            for d, col in zip(datums, key):
                if d.kind == "value":
                    out.append(RangeDatum("value", _coerce_literal(d.value, col.kind, f"RANGE 边界 {text!r} {side}")))
                else:
                    out.append(d)
            return tuple(out)

        return Bound(kind="range", lower=coerce_side(lower, "FROM"), upper=coerce_side(upper, "TO"))

    raise BoundParseError(f"无法识别的边界表达式: {text!r}")


# ---------------------------------------------------------------- 规范化与指纹

def canonical_bound(b: Bound | None):
    if b is None:
        return None
    if b.kind == "default":
        return {"kind": "default"}
    if b.kind == "list":
        return {"kind": "list", "values": list(b.list_values)}
    return {
        "kind": "range",
        "lower": [_canonical_datum(d) for d in b.lower],
        "upper": [_canonical_datum(d) for d in b.upper],
    }


def _canonical_datum(d: RangeDatum):
    if d.kind == "value":
        return {"t": "value", "v": d.value}
    return {"t": d.kind}


def format_bound(b: Bound | None) -> str:
    """人类可读边界,用于报告。"""
    if b is None:
        return "-"
    if b.kind == "default":
        return "DEFAULT"
    if b.kind == "list":
        return "IN (" + ", ".join(_fmt_value(v) for v in b.list_values) + ")"
    return (
        "FROM (" + ", ".join(_fmt_datum(d) for d in b.lower) + ")"
        " TO (" + ", ".join(_fmt_datum(d) for d in b.upper) + ")"
    )


def _fmt_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _fmt_datum(d: RangeDatum):
    if d.kind == "min":
        return "MINVALUE"
    if d.kind == "max":
        return "MAXVALUE"
    return _fmt_value(d.value)


# ---------------------------------------------------------------- 目录读取

def _kind_of(typname: str, supported: set):
    if typname in INT_RANGES and typname in supported:
        return "int"
    if typname == "text" and "text" in supported:
        return "text"
    return None


def load_partition_tree(cur, parent: str, cfg: dict) -> PartitionTree:
    """只读查询目录,构建分区树并校验受支持性。所有表名均来自目录,不硬编码。"""
    supported = {CONFIG_TYPE_MAP[t] for t in cfg["supported_types"]}
    max_depth = int(cfg["max_partition_depth"])

    row = cur.execute(
        "SELECT c.oid, n.nspname, c.relname, c.relkind "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s)",
        (parent,),
    ).fetchone()
    if row is None:
        raise UsageError(f"父表 {parent!r} 不存在")
    root_oid, root_schema, root_name, root_kind = row
    if root_kind != "p":
        raise CatalogUnsupported(f"{parent!r} 不是分区父表(relkind={root_kind!r})")

    tree_rows = cur.execute(
        "SELECT t.relid::oid, t.parentrelid::oid, t.level, t.isleaf, "
        "       n.nspname, c.relname, c.relkind, c.relispartition, "
        "       pg_get_expr(c.relpartbound, c.oid) "
        "FROM pg_partition_tree(to_regclass(%s)) t "
        "JOIN pg_class c ON c.oid = t.relid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "ORDER BY t.level, t.relid",
        (parent,),
    ).fetchall()

    nodes = {}
    for relid, parent_oid, level, _isleaf, nsp, relname, relkind, _ispart, bound_text in tree_rows:
        nodes[relid] = Node(
            oid=relid, schema=nsp, name=relname, relkind=relkind,
            level=level, parent_oid=parent_oid, bound_text=bound_text,
        )
    for n in nodes.values():
        if n.parent_oid in nodes:
            nodes[n.parent_oid].children.append(n)
    for n in nodes.values():
        n.children.sort(key=lambda c: c.oid)  # 确定性顺序;DEFAULT 语义由路由逻辑保证,不依赖顺序

    deepest = max(n.level for n in nodes.values())
    if deepest > max_depth:
        raise CatalogUnsupported(
            f"分区层次深度 {deepest} 超过 max_partition_depth={max_depth}"
        )

    part_oids = [n.oid for n in nodes.values() if n.relkind == "p"]
    part_info = {}
    if part_oids:
        for oid, strat, natts, attrs_text, has_expr, defid in cur.execute(
            "SELECT p.partrelid, p.partstrat::text, p.partnatts, p.partattrs::text, "
            "       (p.partexprs IS NOT NULL), p.partdefid "
            "FROM pg_partitioned_table p WHERE p.partrelid = ANY(%s)",
            (part_oids,),
        ).fetchall():
            part_info[oid] = (strat, natts, attrs_text, has_expr, defid)

    col_rows = cur.execute(
        "SELECT a.attrelid, a.attnum, a.attname, t.typname, co.collname, a.attnotnull "
        "FROM pg_attribute a "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "LEFT JOIN pg_collation co ON co.oid = a.attcollation "
        "WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attrelid, a.attnum",
        ([n.oid for n in nodes.values()],),
    ).fetchall()
    cols_by_rel = {}
    for attrelid, attnum, attname, typname, collname, attnotnull in col_rows:
        cols_by_rel.setdefault(attrelid, {})[attnum] = (attname, typname, collname, attnotnull)

    for n in nodes.values():
        if n.relkind != "p":
            continue
        strat, natts, attrs_text, has_expr, defid = part_info[n.oid]
        if strat == "h":
            raise CatalogUnsupported(f"{n.qname}: 不支持 HASH 分区")
        if strat not in STRATEGY_MAP:
            raise CatalogUnsupported(f"{n.qname}: 未知分区策略 {strat!r}")
        if has_expr:
            raise CatalogUnsupported(f"{n.qname}: 不支持表达式分区键")
        attnums = [int(x) for x in attrs_text.split()] if attrs_text else []
        if len(attnums) != natts or not attnums:
            raise CatalogUnsupported(f"{n.qname}: 分区键列数异常({attrs_text!r})")
        if strat == "l" and natts != 1:
            raise CatalogUnsupported(f"{n.qname}: LIST 分区键必须为单列")
        key = []
        for attnum in attnums:
            if attnum not in cols_by_rel.get(n.oid, {}):
                raise CatalogUnsupported(f"{n.qname}: 分区键列 attnum={attnum} 不存在")
            attname, typname, collname, _nn = cols_by_rel[n.oid][attnum]
            kind = _kind_of(typname, supported)
            if kind is None:
                raise CatalogUnsupported(
                    f"{n.qname}: 分区键列 {attname} 类型 {typname} 不在支持范围"
                )
            if kind == "text":
                if collname != "C":
                    raise CatalogUnsupported(
                        f"{n.qname}: 文本分区键 {attname} 的 collation 为 "
                        f"{collname or '默认'},仅支持显式 C collation"
                    )
                key.append(KeyColumn(attnum, attname, typname, kind, "C"))
            else:
                key.append(KeyColumn(attnum, attname, typname, kind, None))
        n.strategy = STRATEGY_MAP[strat]
        n.key = tuple(key)
        n.default_child_oid = defid or None

    for n in nodes.values():
        if n.parent_oid is None:
            continue  # 根无边界
        if n.bound_text is None:
            raise CatalogUnsupported(f"{n.qname}: 分区缺少 relpartbound")
        parent_node = nodes[n.parent_oid]
        n.bound = parse_bound(n.bound_text, parent_node.key)
        if n.bound.kind == "default" and parent_node.default_child_oid != n.oid:
            raise CatalogUnsupported(f"{n.qname}: DEFAULT 边界与 partdefid 目录信息不一致")

    root = nodes[root_oid]
    columns = []
    for attnum, (attname, typname, _coll, attnotnull) in sorted(cols_by_rel[root_oid].items()):
        kind = _kind_of(typname, supported)
        if kind is None:
            raise CatalogUnsupported(
                f"父表列 {attname} 类型 {typname} 不在支持范围,无法保证原样搬运"
            )
        columns.append(TableColumn(attnum, attname, typname, kind, attnotnull))

    canonical_nodes = []
    for oid in sorted(nodes):
        n = nodes[oid]
        canonical_nodes.append({
            "oid": n.oid,
            "name": n.qname,
            "level": n.level,
            "parent_oid": n.parent_oid,
            "relkind": n.relkind,
            "strategy": n.strategy,
            "key": [
                {"attnum": k.attnum, "name": k.name, "type": k.typname,
                 "kind": k.kind, "collation": k.collation}
                for k in n.key
            ],
            "bound": canonical_bound(n.bound),
            "bound_text": n.bound_text,
        })
    canonical_columns = [
        {"attnum": c.attnum, "name": c.name, "type": c.typname, "kind": c.kind, "notnull": c.notnull}
        for c in columns
    ]
    payload = {"columns": canonical_columns, "nodes": canonical_nodes}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fingerprint = hashlib.sha256(blob).hexdigest()

    return PartitionTree(
        root=root, nodes=nodes, columns=columns,
        fingerprint=fingerprint,
        canonical_nodes=canonical_nodes,
        canonical_columns=canonical_columns,
    )
