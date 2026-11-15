#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tablefreeze — 冻结网页表格采集器。

读取本地 HTML 与 scope.json，对 scope 指定的 table id 建立逻辑网格，
输出可入仓长表（long_table.csv）、表头关系（headers.json）与问题记录（problems.json）。
不运行 JavaScript，不爬取其他页面；嵌套 table 属于另一张表，其文字与格子不进入父表。

实现的受限规范（对应 fixtures/scope.json 的 rules，不追求完整 HTML 表格算法）：

grid
    每个单元格放在本行首个未占列；colspan 为正整数，rowspan 为正整数或 0；
    正跨度不得与已占格子相交（占位重叠），也不得跨出行组（越界行跨度）；
    rowspan=0 表示延伸到本行组末尾。违反即拒绝该表，不靠覆盖已占格子凑出整齐结果。
implicit column headers
    thead 内 scope=col/colgroup 且逻辑列区间与目标格相交的 th，按行再列排序。
implicit row headers
    同一 tbody 内 scope=row 且行区间覆盖目标行的 th；scope=rowgroup 作用于
    其所在 tbody 的每一行。
explicit headers
    单元格带 headers 属性时：递归展开每个被引 th 自己的 headers，祖先在前、
    自身在后，按首次出现稳定去重；显式列表取代 scope 推断。被引 th 的 scope 为
    row/rowgroup 归入行路径，其余归入列路径。ID 缺失、指向 td、指向另一张表、
    引用成环均拒绝该表并定位到源 cell。
nested tables
    提取单元格文本与网格时完全忽略嵌套 table。
text
    单元格后代文本（排除嵌套 table），仅去除首尾空白，内部空白保留；
    相同显示文本但不同 ID 的表头不合并。
types
    data-type=text|int|decimal；int 输出规范有符号十进制；decimal 必须恰好两位
    小数；无 data-type 按 text。类型转换失败保留原文与原因，不改变网格与表头关联。

位置表示：行组 ID（行组元素的 id，缺省为 tag#序号）、组内行号、列号，均从 0 开始。
跨格 td 的每个逻辑位置都引用同一原始 cell ID，以 origin/shared 标记来源关系；
缺失格与内容为空的 td 分别记录。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
SECTION_TAGS = ("thead", "tbody", "tfoot")
CELL_TAGS = ("td", "th")
# 提取单元格文本时整棵跳过的子树：嵌套表属于另一张表
SKIP_TEXT_TAGS = ("table", "script", "style")

INT_RE = re.compile(r"[+-]?\d+")
DECIMAL_RE = re.compile(r"[+-]?\d+\.\d{2}")  # 金额仅处理给定两位小数形式
SPAN_RE = re.compile(r"\d+")

DATA_TYPES = ("text", "int", "decimal")

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_USAGE = 1


# ---------------------------------------------------------------------------
# 极简 DOM：html.parser 构建，隐式闭合 tr/td/th/行组，保留嵌套 table 结构
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []  # Node 或 str，按文档顺序
        self.parent = None


class DomBuilder(HTMLParser):
    """宽松 HTML 解析。表格相关的开始标签会隐式闭合未关闭的行/格/行组。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def _auto_close(self, tag):
        if tag == "tr":
            closable = ("td", "th", "tr")
        elif tag in CELL_TAGS:
            closable = ("td", "th")
        elif tag in SECTION_TAGS:
            closable = ("td", "th", "tr") + SECTION_TAGS
        else:
            return
        while len(self.stack) > 1 and self.stack[-1].tag in closable:
            self.stack.pop()

    def _append(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._auto_close(tag)
        node = self._append(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._auto_close(tag.lower())
        self._append(tag.lower(), attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
            # 行/格/行组的结束标签不跨越 table 边界
            if tag in CELL_TAGS + ("tr",) + SECTION_TAGS and self.stack[i].tag == "table":
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def iter_nodes(node):
    """文档序遍历全部元素节点。"""
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from iter_nodes(child)


def iter_table_own(table):
    """文档序遍历目标表自身的元素节点，不进入嵌套 table。"""
    for child in table.children:
        if isinstance(child, Node):
            if child.tag == "table":
                continue  # 嵌套表属于另一张表
            yield child
            yield from iter_table_own(child)


def nearest_ancestor(node, tags, stop=None):
    parent = node.parent
    while parent is not None and parent is not stop:
        if parent.tag in tags:
            return parent
        parent = parent.parent
    return None


def cell_text(node):
    """单元格后代文本（排除嵌套 table 等子树），仅去首尾空白。"""
    parts = []

    def walk(n):
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in SKIP_TEXT_TAGS:
                continue
            else:
                walk(child)

    walk(node)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class RowGroup:
    tag: str
    index: int          # 同类行组中的序号（0 起）
    order: int          # 表内行组顺序（0 起）
    implicit: bool = False
    node: Node | None = None
    rows: list = field(default_factory=list)  # tr Node
    width: int = 0

    @property
    def group_id(self):
        """行组 ID：元素 id 优先，缺省 tag#序号。"""
        if self.node is not None:
            gid = (self.node.attrs.get("id") or "").strip()
            if gid:
                return gid
        return f"{self.tag}#{self.index}"


@dataclass
class Cell:
    cell_id: str
    tag: str            # "td" | "th"
    node: Node
    group: RowGroup
    row: int            # 组内行号（0 起）
    col: int = -1       # 列号（0 起），放置时确定
    rowspan: int = 1    # 生效值（rowspan=0 已解析为到行组末尾）
    colspan: int = 1
    text: str = ""
    has_explicit_id: bool = False
    headers_attr: tuple = ()   # 非空 headers 属性的 token
    scope: str = ""
    data_type: str = "text"


@dataclass
class Problem:
    severity: str       # fatal | warning | info
    code: str
    message: str
    cell_id: str | None = None
    row_group: str | None = None
    row_index: int | None = None
    col_index: int | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "cell_id": self.cell_id,
            "row_group": self.row_group,
            "row_index": self.row_index,
            "col_index": self.col_index,
            "details": self.details,
        }


def cell_problem(severity, code, message, cell, details=None):
    return Problem(
        severity, code, message,
        cell_id=cell.cell_id,
        row_group=cell.group.group_id,
        row_index=cell.row, col_index=cell.col,
        details=details or {},
    )


@dataclass
class TableResult:
    table_id: str
    status: str                       # "ok" | "rejected"
    problems: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    slots: dict = field(default_factory=dict)   # (group.order, r, c) -> (Cell, role)
    graph: dict = field(default_factory=dict)   # cell_id -> 合法 headers 目标
    long_rows: list = field(default_factory=list)
    headers_doc: dict = field(default_factory=dict)

    @property
    def fatal_count(self):
        return sum(1 for p in self.problems if p.severity == "fatal")


# ---------------------------------------------------------------------------
# 行组收集与逻辑网格
# ---------------------------------------------------------------------------

def collect_row_groups(table):
    """按文档序收集 thead/tbody/tfoot；直接挂在 table 下的 tr 归入隐式 tbody。"""
    groups = []
    section_group = {}   # id(section node) -> RowGroup
    counts = {tag: 0 for tag in SECTION_TAGS}
    implicit = None
    for node in iter_table_own(table):
        if node.tag in SECTION_TAGS:
            group = RowGroup(tag=node.tag, index=counts[node.tag],
                             order=len(groups), node=node)
            counts[node.tag] += 1
            groups.append(group)
            section_group[id(node)] = group
            implicit = None
        elif node.tag == "tr":
            section = nearest_ancestor(node, SECTION_TAGS, stop=table)
            if section is not None and id(section) in section_group:
                section_group[id(section)].rows.append(node)
            else:
                if implicit is None:
                    implicit = RowGroup(tag="tbody", index=counts["tbody"],
                                        order=len(groups), implicit=True)
                    counts["tbody"] += 1
                    groups.append(implicit)
                implicit.rows.append(node)
    return groups


def parse_span(cell, attr, problems, allow_zero):
    """colspan 为正整数；rowspan 为正整数或 0。非法值记 fatal 并按 1 继续诊断。"""
    raw = cell.node.attrs.get(attr)
    if raw is None:
        return 1
    value = raw.strip()
    expect = "正整数或 0" if allow_zero else "正整数"
    if not SPAN_RE.fullmatch(value) or (value == "0" and not allow_zero):
        problems.append(cell_problem(
            "fatal", f"invalid-{attr}",
            f"{attr} 须为{expect}，得到 {raw!r}", cell, {"raw": raw}))
        return 1
    return int(value)


def build_grids(groups, problems):
    """在每个行组内放置单元格，返回 (cells, slots)。违规记 fatal 且不覆盖已占格子。"""
    cells = []
    slots = {}
    auto_n = 0
    id_map = {}
    for group in groups:
        nrows = len(group.rows)
        occupied = {}  # (r, c) -> Cell
        for r, tr in enumerate(group.rows):
            c = 0
            for node in tr.children:
                if not (isinstance(node, Node) and node.tag in CELL_TAGS):
                    continue
                raw_id = (node.attrs.get("id") or "").strip()
                if raw_id:
                    cell_id, explicit = raw_id, True
                else:
                    auto_n += 1
                    cell_id, explicit = f"auto-{auto_n}", False
                cell = Cell(
                    cell_id=cell_id, tag=node.tag, node=node, group=group, row=r,
                    text=cell_text(node),
                    has_explicit_id=explicit,
                    headers_attr=tuple((node.attrs.get("headers") or "").split()),
                    scope=(node.attrs.get("scope") or "").strip().lower(),
                    data_type=(node.attrs.get("data-type") or "text").strip().lower(),
                )
                if explicit and cell_id in id_map:
                    problems.append(cell_problem(
                        "fatal", "duplicate-cell-id",
                        f"表内单元格 ID 重复：{cell_id!r}", cell,
                        {"first_seen_at": id_map[cell_id]}))
                elif explicit:
                    id_map[cell_id] = cell_id

                while (r, c) in occupied:
                    c += 1
                cell.col = c
                cell.colspan = parse_span(cell, "colspan", problems, allow_zero=False)
                rowspan = parse_span(cell, "rowspan", problems, allow_zero=True)
                if rowspan == 0:
                    cell.rowspan = nrows - r          # 仅延伸到本行组结束
                else:
                    cell.rowspan = rowspan
                    if r + rowspan > nrows:
                        problems.append(cell_problem(
                            "fatal", "rowspan-out-of-bounds",
                            f"rowspan={rowspan} 跨出行组（剩余 {nrows - r} 行）",
                            cell, {"rowspan": rowspan, "rows_available": nrows - r}))
                        cell.rowspan = nrows - r      # 仅为继续诊断截断，表将被拒绝

                conflict = None
                for rr in range(r, r + cell.rowspan):
                    for cc in range(c, c + cell.colspan):
                        if (rr, cc) in occupied:
                            conflict = (rr, cc)
                            break
                    if conflict:
                        break
                if conflict:
                    other = occupied[conflict]
                    problems.append(cell_problem(
                        "fatal", "cell-overlap",
                        f"占位重叠：与 {other.cell_id} 在 r{conflict[0]}c{conflict[1]} 冲突",
                        cell, {"conflicts_with": other.cell_id,
                               "at_row": conflict[0], "at_col": conflict[1]}))
                    continue  # 不覆盖已占格子
                for rr in range(r, r + cell.rowspan):
                    for cc in range(c, c + cell.colspan):
                        occupied[(rr, cc)] = cell
                cells.append(cell)

        width = max((cc + 1 for _, cc in occupied), default=0)
        group.width = width
        for (rr, cc), cell in occupied.items():
            role = "origin" if (cell.row, cell.col) == (rr, cc) else "shared"
            slots[(group.order, rr, cc)] = (cell, role)
        for rr in range(nrows):
            for cc in range(width):
                if (rr, cc) not in occupied:
                    problems.append(Problem(
                        "warning", "missing-slot",
                        f"行组 {group.group_id} r{rr}c{cc} 无单元格覆盖（缺失格）",
                        row_group=group.group_id, row_index=rr, col_index=cc))
    return cells, slots


# ---------------------------------------------------------------------------
# 表头：headers 属性校验、环检测、递归展开；scope 隐式关联
# ---------------------------------------------------------------------------

def validate_headers(cells, doc_ids, table_id, problems):
    """校验所有 headers 属性，返回 cell_id -> 合法 th ID 列表的有向图。"""
    id_map = {}
    for cell in cells:
        id_map.setdefault(cell.cell_id, cell)
    graph = {}
    for cell in cells:
        if not cell.headers_attr:
            continue
        valid = []
        for token in cell.headers_attr:
            if token in id_map:
                target = id_map[token]
                if target.tag != "th":
                    problems.append(cell_problem(
                        "fatal", "headers-target-not-th",
                        f"headers 引用的 {token!r} 是 <{target.tag}>，不是 th",
                        cell, {"token": token}))
                else:
                    valid.append(token)
            elif token in doc_ids:
                node, owner_table = doc_ids[token]
                if node.tag in CELL_TAGS:
                    problems.append(cell_problem(
                        "fatal", "headers-cross-table",
                        f"headers 引用的 {token!r} 属于另一张表 {owner_table!r}",
                        cell, {"token": token, "found_in_table": owner_table}))
                else:
                    problems.append(cell_problem(
                        "fatal", "headers-target-not-th",
                        f"headers 引用的 {token!r} 是 <{node.tag}>，不是 th",
                        cell, {"token": token}))
            else:
                problems.append(cell_problem(
                    "fatal", "headers-id-missing",
                    f"headers 引用的 ID {token!r} 在文档中不存在",
                    cell, {"token": token}))
        graph[cell.cell_id] = valid

    # 环检测（只有 th 能作为被引用方，环只可能出现在 th 之间）
    color, stack, cycles = {}, [], set()

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in graph.get(u, ()):
            state = color.get(v, 0)
            if state == 0:
                dfs(v)
            elif state == 1:
                cycle = stack[stack.index(v):]
                k = cycle.index(min(cycle))
                cycles.add(tuple(cycle[k:] + cycle[:k]))
        stack.pop()
        color[u] = 2

    for cell in cells:
        if cell.headers_attr and color.get(cell.cell_id, 0) == 0:
            dfs(cell.cell_id)
    for cycle in sorted(cycles):
        first = id_map[cycle[0]]
        problems.append(cell_problem(
            "fatal", "headers-cycle",
            "表头引用环：" + " → ".join(cycle + (cycle[0],)),
            first, {"cycle": list(cycle) + [cycle[0]]}))
    return graph


def expand_headers(cell_id, graph):
    """递归展开 headers：祖先在前、自身在后，按首次出现稳定去重。"""
    out, seen = [], set()

    def visit(cid):
        for token in graph.get(cid, ()):
            if token not in seen:
                seen.add(token)
                visit(token)        # 祖先在前
                out.append(token)   # 自身在后

    visit(cell_id)
    return out


def implicit_column_headers(thead_cells, col):
    """thead 内 scope=col/colgroup 且列区间相交的 th，按行再列排序。"""
    return [
        th.cell_id for th in thead_cells
        if th.scope in ("col", "colgroup") and th.col <= col < th.col + th.colspan
    ]


def implicit_row_headers(group_cells, row):
    """同一 tbody 内 scope=row 覆盖目标行的 th；scope=rowgroup 作用于本 tbody 每行。"""
    out = []
    for th in group_cells:
        if th.scope == "row" and th.row <= row < th.row + th.rowspan:
            out.append(th.cell_id)
        elif th.scope == "rowgroup":
            out.append(th.cell_id)
    return out


# ---------------------------------------------------------------------------
# 类型转换：data-type=text|int|decimal；失败保留原文与原因
# ---------------------------------------------------------------------------

def convert_value(text, data_type):
    """返回 (value, error_reason)。text 原样；int 规范有符号十进制；decimal 两位小数。"""
    if data_type == "text":
        return text, None
    if data_type == "int":
        if not INT_RE.fullmatch(text):
            return None, "int 须为规范有符号十进制整数"
        return str(int(text)), None
    if data_type == "decimal":
        if not DECIMAL_RE.fullmatch(text):
            return None, "decimal 须为恰好两位小数形式"
        try:
            return str(Decimal(text)), None
        except InvalidOperation:
            return None, "decimal 数值无法解析"
    return None, f"未知 data-type {data_type!r}"


# ---------------------------------------------------------------------------
# 抽取主流程
# ---------------------------------------------------------------------------

def extract_tables(html_text, table_ids):
    """对 html_text 中每个指定 table id 执行抽取，返回 TableResult 列表。"""
    builder = DomBuilder()
    builder.feed(html_text)
    builder.close()

    doc_ids = {}
    for node in iter_nodes(builder.root):
        node_id = (node.attrs.get("id") or "").strip()
        if node_id and node_id not in doc_ids:
            owner = nearest_ancestor(node, ("table",))
            owner_id = (owner.attrs.get("id") or "").strip() if owner is not None else None
            doc_ids[node_id] = (node, owner_id or None)

    tables_by_id = {}
    for node in iter_nodes(builder.root):
        if node.tag == "table":
            table_id = (node.attrs.get("id") or "").strip()
            if table_id and table_id not in tables_by_id:
                tables_by_id[table_id] = node

    return [_extract_one(t, tables_by_id.get(t), doc_ids) for t in table_ids]


def _extract_one(table_id, table, doc_ids):
    problems = []
    if table is None:
        problems.append(Problem("fatal", "table-not-found",
                                f"找不到 id={table_id!r} 的表格"))
        return TableResult(table_id=table_id, status="rejected", problems=problems)

    groups = collect_row_groups(table)
    cells, slots = build_grids(groups, problems)
    graph = validate_headers(cells, doc_ids, table_id, problems)

    for cell in cells:
        if cell.text == "":
            problems.append(cell_problem("info", "empty-cell",
                                         "内容为空的单元格", cell, {"tag": cell.tag}))
        if cell.data_type not in DATA_TYPES:
            problems.append(cell_problem(
                "warning", "unknown-data-type",
                f"未知 data-type {cell.data_type!r}，按 text 处理", cell,
                {"data-type": cell.data_type}))

    result = TableResult(table_id=table_id, status="ok", problems=problems,
                         groups=groups, cells=cells, slots=slots, graph=graph)
    if any(p.severity == "fatal" for p in problems):
        result.status = "rejected"
        return result

    result.long_rows, result.headers_doc = _build_outputs(result)
    result.problems = problems  # _build_outputs 可能追加类型转换 warning
    return result


def _header_paths(result, cell, group, row, col, th_by_id):
    """返回 (source, header_ids, row_ids, col_ids)。显式 headers 取代 scope 推断。"""
    if cell is not None and cell.headers_attr:
        ids = expand_headers(cell.cell_id, result.graph)
        row_ids, col_ids = [], []
        for hid in ids:
            th = th_by_id[hid]
            (row_ids if th.scope in ("row", "rowgroup") else col_ids).append(hid)
        return "headers", ids, row_ids, col_ids
    thead_cells = [c for c in result.cells if c.tag == "th" and c.group.tag == "thead"]
    col_ids = implicit_column_headers(thead_cells, col)
    row_ids = []
    if group.tag == "tbody":
        group_ths = [c for c in result.cells
                     if c.tag == "th" and c.group is group]
        row_ids = implicit_row_headers(group_ths, row)
    return "scope", row_ids + col_ids, row_ids, col_ids


def _build_outputs(result):
    th_by_id = {c.cell_id: c for c in result.cells if c.tag == "th"}
    long_rows = []
    relations = []
    for group in result.groups:
        for r in range(len(group.rows)):
            for c in range(group.width):
                slot = result.slots.get((group.order, r, c))
                cell, role = slot if slot else (None, "missing")
                if cell is not None and cell.tag != "td":
                    continue  # 表头格不进长表，见 headers.json
                source, hids, rids, cids = _header_paths(result, cell, group, r, c, th_by_id)
                row = {
                    "table_id": result.table_id,
                    "row_group": group.group_id,
                    "row_index": r,
                    "col_index": c,
                    "cell_id": cell.cell_id if cell else "",
                    "slot_role": role,
                    "is_empty": "",
                    "raw_text": "",
                    "data_type": "",
                    "value": "",
                    "value_status": "skipped",
                    "error_reason": "",
                    "header_source": source,
                    "header_ids": " ".join(hids),
                    "row_header_ids": " ".join(rids),
                    "column_header_ids": " ".join(cids),
                }
                if cell is not None and role == "origin":
                    row["is_empty"] = "true" if cell.text == "" else "false"
                    row["raw_text"] = cell.text
                    row["data_type"] = cell.data_type
                    if cell.text == "":
                        row["value_status"] = "empty"
                    elif cell.data_type not in DATA_TYPES:
                        row["value"] = cell.text      # 未知类型按 text，已记 warning
                        row["value_status"] = "ok"
                    else:
                        value, reason = convert_value(cell.text, cell.data_type)
                        if reason is None:
                            row["value"] = value
                            row["value_status"] = "ok"
                        else:
                            row["value_status"] = "error"
                            row["error_reason"] = reason
                            result.problems.append(cell_problem(
                                "warning", "type-conversion",
                                f"类型转换失败：{reason}", cell,
                                {"raw_text": cell.text, "data_type": cell.data_type,
                                 "reason": reason}))
                    relations.append({
                        "cell_id": cell.cell_id,
                        "row_group": group.group_id,
                        "row_index": r,
                        "col_index": c,
                        "source": source,
                        "header_ids": hids,
                        "row_header_ids": rids,
                        "column_header_ids": cids,
                    })
                long_rows.append(row)

    headers_doc = {
        "table_id": result.table_id,
        "headers": [
            {
                "id": th.cell_id,
                "text": th.text,
                "scope": th.scope,
                "row_group": th.group.group_id,
                "row_index": th.row,
                "col_index": th.col,
                "rowspan": th.rowspan,
                "colspan": th.colspan,
                "explicit_header_ids": expand_headers(th.cell_id, result.graph)
                if th.headers_attr else [],
            }
            for th in result.cells if th.tag == "th"
        ],
        "relations": relations,
    }
    return long_rows, headers_doc


# ---------------------------------------------------------------------------
# 输出：long_table.csv / headers.json / problems.json
# ---------------------------------------------------------------------------

LONG_TABLE_FIELDS = [
    "table_id", "row_group", "row_index", "col_index",
    "cell_id", "slot_role", "is_empty", "raw_text", "data_type",
    "value", "value_status", "error_reason",
    "header_source", "header_ids", "row_header_ids", "column_header_ids",
]


def problems_doc(result):
    return {
        "table_id": result.table_id,
        "status": result.status,
        "fatal_count": result.fatal_count,
        "problem_count": len(result.problems),
        "problems": [p.to_dict() for p in result.problems],
    }


def write_outputs(result, out_dir):
    """problems.json 总是写；仅 ok 时发布 long_table.csv 与 headers.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "problems.json").write_text(
        json.dumps(problems_doc(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    long_csv = out / "long_table.csv"
    headers_json = out / "headers.json"
    if result.status == "ok":
        with long_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LONG_TABLE_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(result.long_rows)
        headers_json.write_text(
            json.dumps(result.headers_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    else:
        # 拒绝时不发布成功表，并清除可能存在的陈旧产物
        for stale in (long_csv, headers_json):
            if stale.exists():
                stale.unlink()


# ---------------------------------------------------------------------------
# inspect：打印逻辑网格、单元格、表头关联与问题，便于人工追查
# ---------------------------------------------------------------------------

def format_inspection(result):
    lines = [f"table_id: {result.table_id}", f"status: {result.status}"]
    if result.groups:
        lines.append("row groups:")
        for g in result.groups:
            note = " (implicit)" if g.implicit else ""
            lines.append(f"  [{g.order}] {g.group_id} <{g.tag}>{note} "
                         f"rows={len(g.rows)} cols={g.width}")
        th_by_id = {c.cell_id: c for c in result.cells if c.tag == "th"}
        for g in result.groups:
            lines.append(f"grid [{g.order}] {g.group_id}:")
            for r in range(len(g.rows)):
                tokens = []
                for c in range(g.width):
                    slot = result.slots.get((g.order, r, c))
                    if slot is None:
                        tokens.append("∅missing")
                        continue
                    cell, role = slot
                    if role == "origin":
                        span = (f"({cell.rowspan}x{cell.colspan})"
                                if (cell.rowspan, cell.colspan) != (1, 1) else "")
                        tokens.append(f"{cell.cell_id}{span}")
                    else:
                        tokens.append(f"{cell.cell_id}~")
                lines.append(f"  r{r}: " + " | ".join(tokens))
        lines.append("cells:")
        for cell in result.cells:
            extra = []
            if cell.headers_attr:
                extra.append(f"headers={' '.join(cell.headers_attr)!r}")
                extra.append(f"expand={expand_headers(cell.cell_id, result.graph)}")
            if cell.scope:
                extra.append(f"scope={cell.scope}")
            if cell.tag == "td":
                extra.append(f"data-type={cell.data_type}")
            lines.append(f"  {cell.cell_id} <{cell.tag}> @{cell.group.group_id} "
                         f"r{cell.row} c{cell.col} span {cell.rowspan}x{cell.colspan} "
                         f"text={cell.text!r} {' '.join(extra)}")
        if result.status == "ok":
            lines.append("header relations (origin data cells):")
            for rel in result.headers_doc["relations"]:
                lines.append(f"  {rel['cell_id']} @{rel['row_group']} "
                             f"r{rel['row_index']} c{rel['col_index']} [{rel['source']}] "
                             f"row={rel['row_header_ids']} col={rel['column_header_ids']}")
        else:
            lines.append("header relations: 表被拒绝，未生成")
        _ = th_by_id
    lines.append("problems:")
    if not result.problems:
        lines.append("  (none)")
    for p in result.problems:
        loc = ""
        if p.row_group is not None:
            loc = f" @{p.row_group} r{p.row_index} c{p.col_index}"
        cell = f" cell={p.cell_id}" if p.cell_id else ""
        lines.append(f"  [{p.severity}] {p.code}{loc}{cell}: {p.message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def resolve_table_ids(scope, html_name, table_override):
    if table_override:
        return [table_override]
    ids = (scope.get("table_ids") or {}).get(html_name)
    if not ids:
        raise ConfigError(
            f"scope.json 的 table_ids 中没有 {html_name!r} 的条目，"
            "也未用 --table 指定")
    return list(ids)


def _load_inputs(args):
    try:
        html_text = Path(args.html).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取 HTML 文件：{exc}")
    try:
        scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取 scope.json：{exc}")
    table_ids = resolve_table_ids(scope, Path(args.html).name, args.table)
    return html_text, table_ids


def cmd_extract(args):
    try:
        html_text, table_ids = _load_inputs(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    results = extract_tables(html_text, table_ids)
    exit_code = EXIT_OK
    for result in results:
        out_dir = Path(args.out) / result.table_id
        write_outputs(result, out_dir)
        if result.status != "ok":
            exit_code = EXIT_REJECTED
        print(f"{result.table_id}: {result.status} "
              f"(fatal={result.fatal_count}, problems={len(result.problems)})"
              f" -> {out_dir}")
    return exit_code


def cmd_inspect(args):
    try:
        html_text, table_ids = _load_inputs(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    results = extract_tables(html_text, table_ids)
    for i, result in enumerate(results):
        if i:
            print()
        print(format_inspection(result))
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tablefreeze",
        description="冻结网页表格采集器：本地 HTML + scope.json → 长表/表头关系/问题记录")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("extract", "抽取并发布长表、表头关系与问题记录"),
                            ("inspect", "打印逻辑网格与表头关联，便于人工追查")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--html", required=True, help="本地 HTML 文件路径")
        p.add_argument("--scope", required=True, help="scope.json 路径")
        p.add_argument("--table", help="只处理该 table id（默认取 scope.table_ids[html文件名]）")
        if name == "extract":
            p.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args(argv)
    return cmd_extract(args) if args.command == "extract" else cmd_inspect(args)


if __name__ == "__main__":
    sys.exit(main())
