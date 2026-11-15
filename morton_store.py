#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
morton_store.py — 8 位 Morton 码不可变块存储 + 矩形范围查询（Python 3.11，仅标准库）。

子命令：
  build    从 JSONL 构建不可变块 + 块目录（layout.json）
  plan     展示矩形 -> 四叉网格分解 -> 区间合并 -> 候选码区间 -> 选中块
  query    只打开候选块，码区间过滤 + 原 x/y 精确残差过滤，输出按 row_id 字节序
  verify   校验布局与全部块的完整性/索引绑定
  selftest 用原 JSONL 逐点过滤作 oracle 的全面核对（含随机小矩形、损坏注入）

Morton 码：16 位，bit 0 = x 的 bit 0，bit 1 = y 的 bit 0，……交错。
排序键：(morton_code, row_id 的 UTF-8 字节)。所有区间左闭右开 [lo, hi)。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile

FORMAT_VERSION = 1
BITS = 8                     # 每轴 8 位
DOMAIN = 1 << BITS           # 256，整数域 [0,256)
CODE_SPACE = 1 << (2 * BITS) # 65536，码域 [0,65536)
BLOCK_HEADER_FORMAT = "morton-block/1"
LAYOUT_FILE = "layout.json"


class StoreError(Exception):
    """存储层通用错误。"""


class BuildError(StoreError):
    """build 输入非法。"""


class QueryError(StoreError):
    """查询参数非法。"""


class CorruptBlockError(StoreError):
    """块文件损坏或与块目录绑定不一致。"""


# ---------------------------------------------------------------- Morton 码

def _spread8(v: int) -> int:
    """把 8 位整数散到 16 位的偶数位。"""
    v = (v | (v << 4)) & 0x0F0F
    v = (v | (v << 2)) & 0x3333
    v = (v | (v << 1)) & 0x5555
    return v


def morton_encode(x: int, y: int) -> int:
    """交错 x/y 各 8 位；码 bit0 = x bit0，码 bit1 = y bit0，依次类推。"""
    return _spread8(x) | (_spread8(y) << 1)


def morton_decode(code: int) -> tuple[int, int]:
    def compact(v: int) -> int:
        v &= 0x5555
        v = (v | (v >> 1)) & 0x3333
        v = (v | (v >> 2)) & 0x0F0F
        v = (v | (v >> 4)) & 0x00FF
        return v
    return compact(code), compact(code >> 1)


# ------------------------------------------------------- 矩形 -> Morton 区间

def decompose_rect(x0: int, x1: int, y0: int, y1: int):
    """
    把 [x0,x1)×[y0,y1) 与完整 256×256 整数域的四叉网格相交，
    返回 (精确覆盖的 Morton 区间列表, 裁剪后的矩形)。
    区间左闭右开、按码升序、两两不相交；相邻胞格已在码上连续时合并（不引入额外点）。
    空矩形返回空列表。绝不只用两个角点近似。
    """
    cx0, cx1 = max(0, x0), min(DOMAIN, x1)
    cy0, cy1 = max(0, y0), min(DOMAIN, y1)
    clamped = [cx0, cx1, cy0, cy1]
    if cx0 >= cx1 or cy0 >= cy1:
        return [], clamped

    intervals: list[list[int]] = []

    def visit(x_lo: int, y_lo: int, side: int, level: int, prefix: int) -> None:
        # 胞格 [x_lo,x_lo+side)×[y_lo,y_lo+side)，level 0 = 根（全域）
        if x_lo + side <= cx0 or x_lo >= cx1 or y_lo + side <= cy0 or y_lo >= cy1:
            return  # 不相交
        if cx0 <= x_lo and x_lo + side <= cx1 and cy0 <= y_lo and y_lo + side <= cy1:
            # prefix 已按码位对齐（占据高 2*level 位），剩余 2*(BITS-level) 位为本胞格范围
            intervals.append([prefix, prefix + (1 << (2 * (BITS - level)))])
            return  # 完全包含：整个胞格即一个码区间
        half = side >> 1
        child_shift = 2 * (BITS - level - 1)
        for q in (0, 1, 2, 3):  # q 的 bit0=x 选择位, bit1=y 选择位，即 Morton 顺序
            xb = q & 1
            yb = (q >> 1) & 1
            visit(x_lo + xb * half, y_lo + yb * half, half,
                  level + 1, prefix | (q << child_shift))

    visit(0, 0, DOMAIN, 0, 0)
    # 子胞格按 q=0..3 访问，输出天然按码升序；把码上首尾相接的区间并掉（精确，无保守化）
    merged: list[list[int]] = []
    for lo, hi in intervals:
        if merged and merged[-1][1] == lo:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged], clamped


def merge_to_budget(intervals: list[tuple[int, int]], budget: int):
    """
    在 max_intervals 预算内合并区间。每次选择相邻间隔（gap）最小的一对合并，
    合并结果 [lo, hi_next) 是更保守的覆盖（gap 内的码可能落在矩形外，
    由 query 的残差过滤剔除），但绝不漏掉矩形内任何整数点。
    返回 (合并后区间, 合并日志)。
    """
    if budget < 1:
        raise QueryError(f"max_intervals 必须 >= 1，得到 {budget}")
    ints = [[lo, hi] for lo, hi in intervals]
    log: list[dict] = []
    while len(ints) > budget:
        best_i, best_gap = 0, ints[1][0] - ints[0][1]
        for i in range(1, len(ints) - 1):
            gap = ints[i + 1][0] - ints[i][1]
            if gap < best_gap:
                best_i, best_gap = i, gap
        a, b = ints[best_i], ints[best_i + 1]
        log.append({
            "reason": f"区间数 {len(ints)} 超过预算 {budget}，合并相邻间隔最小的一对",
            "merged": [[a[0], a[1]], [b[0], b[1]]],
            "into": [a[0], b[1]],
            "gap_codes": best_gap,
        })
        ints[best_i:best_i + 2] = [[a[0], b[1]]]
    return [(lo, hi) for lo, hi in ints], log


def intervals_contain(intervals: list[tuple[int, int]], code: int) -> bool:
    los = [lo for lo, _ in intervals]
    i = bisect.bisect_right(los, code) - 1
    return i >= 0 and code < intervals[i][1]


# ------------------------------------------------------------------ 布局读写

def _dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def load_layout(store_dir: str) -> dict:
    path = os.path.join(store_dir, LAYOUT_FILE)
    if not os.path.isfile(path):
        raise StoreError(f"布局文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        layout = json.load(f)
    for key in ("format_version", "bits_per_axis", "block_rows", "code_min",
                "code_max", "summary", "directory"):
        if key not in layout:
            raise StoreError(f"布局缺少字段 {key!r}: {path}")
    if layout["format_version"] != FORMAT_VERSION:
        raise StoreError(f"不支持的格式版本 {layout['format_version']}（期望 {FORMAT_VERSION}）")
    if layout["bits_per_axis"] != BITS:
        raise StoreError(f"不支持的 bits_per_axis {layout['bits_per_axis']}（期望 {BITS}）")
    if not isinstance(layout["directory"], list):
        raise StoreError("布局 directory 必须是列表")
    return layout


def read_block(store_dir: str, entry: dict) -> list[dict]:
    """
    打开一个块并做完整性/索引绑定校验：
    sha256、块头 block_id/行数、行数与目录一致、码与 x/y 重算一致、
    (code,row_id) 严格升序、目录码界限与实际首末行一致。任何不符即 CorruptBlockError。
    """
    bid = entry["block_id"]
    path = os.path.join(store_dir, entry["path"])
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise CorruptBlockError(f"块 {bid}: 无法读取 {path}: {e}") from e
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry["sha256"]:
        raise CorruptBlockError(
            f"块 {bid}: sha256 不匹配（文件 {digest[:12]}… != 目录 {entry['sha256'][:12]}…），"
            f"块已损坏或目录被篡改: {path}")
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise CorruptBlockError(f"块 {bid}: 空文件: {path}")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as e:
        raise CorruptBlockError(f"块 {bid}: 块头 JSON 损坏: {e}") from e
    if header.get("format") != BLOCK_HEADER_FORMAT:
        raise CorruptBlockError(f"块 {bid}: 块头 format 非法: {header.get('format')!r}")
    if header.get("block_id") != bid:
        raise CorruptBlockError(
            f"块 {bid}: 索引绑定失败，块头 block_id={header.get('block_id')}（文件被调换？）: {path}")
    rows: list[dict] = []
    for lineno, line in enumerate(lines[1:], start=2):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise CorruptBlockError(f"块 {bid}: 第 {lineno} 行 JSON 损坏: {e}") from e
    if len(rows) != entry["rows"] or header.get("rows") != len(rows):
        raise CorruptBlockError(
            f"块 {bid}: 行数不符（实际 {len(rows)}，块头 {header.get('rows')}，目录 {entry['rows']}）")
    prev_key = None
    for r in rows:
        try:
            x, y, code, rid = r["x"], r["y"], r["code"], r["row_id"]
        except KeyError as e:
            raise CorruptBlockError(f"块 {bid}: 行缺字段 {e}") from e
        if morton_encode(x, y) != code:
            raise CorruptBlockError(f"块 {bid}: 行 {rid!r} 的 code={code} 与 morton({x},{y}) 不符")
        key = (code, rid.encode("utf-8"))
        if prev_key is not None and key <= prev_key:
            raise CorruptBlockError(f"块 {bid}: 行序破坏，(code,row_id) 未严格升序于 {rid!r}")
        prev_key = key
    if rows:
        if rows[0]["code"] != entry["code_min"] or rows[-1]["code"] != entry["code_max"]:
            raise CorruptBlockError(
                f"块 {bid}: 目录码界限 [{entry['code_min']},{entry['code_max']}] 与实际 "
                f"[{rows[0]['code']},{rows[-1]['code']}] 不符（索引绑定失败）")
    return rows


# --------------------------------------------------------------------- build

def _valid_int(v, lo, hi) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def build_store(input_path: str, out_dir: str, config: dict,
                block_rows_override: int | None = None) -> dict:
    bits = config.get("bits_per_axis", BITS)
    if bits != BITS:
        raise BuildError(f"仅支持 bits_per_axis=8，config 给出 {bits}")
    block_rows = block_rows_override or config.get("rows_per_block", 128)
    if not _valid_int(block_rows, 1, 1 << 30):
        raise BuildError(f"rows_per_block 非法: {block_rows!r}")

    rows: list[dict] = []
    seen: dict[str, int] = {}
    with open(input_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise BuildError(f"第 {lineno} 行不是合法 JSON: {e}") from e
            if not isinstance(obj, dict):
                raise BuildError(f"第 {lineno} 行不是 JSON 对象")
            missing = {"row_id", "x", "y", "value"} - obj.keys()
            if missing:
                raise BuildError(f"第 {lineno} 行缺字段: {sorted(missing)}")
            rid = obj["row_id"]
            if not isinstance(rid, str):
                raise BuildError(f"第 {lineno} 行 row_id 必须是字符串，得到 {type(rid).__name__}")
            if rid in seen:
                raise BuildError(f"row_id 重复: {rid!r}（第 {seen[rid]} 行与第 {lineno} 行）")
            x, y, value = obj["x"], obj["y"], obj["value"]
            if not _valid_int(x, 0, DOMAIN - 1):
                raise BuildError(f"第 {lineno} 行 x 越界或非整数: {x!r}（允许 0..{DOMAIN - 1}）")
            if not _valid_int(y, 0, DOMAIN - 1):
                raise BuildError(f"第 {lineno} 行 y 越界或非整数: {y!r}（允许 0..{DOMAIN - 1}）")
            if not _valid_int(value, -(1 << 63), 1 << 63):
                raise BuildError(f"第 {lineno} 行 value 必须是整数: {value!r}")
            seen[rid] = lineno
            rows.append({"row_id": rid, "x": x, "y": y, "value": value,
                         "code": morton_encode(x, y)})

    rows.sort(key=lambda r: (r["code"], r["row_id"].encode("utf-8")))

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "blocks"))

    directory = []
    for start in range(0, len(rows), block_rows):
        chunk = rows[start:start + block_rows]
        bid = len(directory)
        rel = f"blocks/block-{bid:04d}.jsonl"
        payload = json.dumps({"format": BLOCK_HEADER_FORMAT, "block_id": bid,
                              "rows": len(chunk)},
                             ensure_ascii=False, separators=(",", ":")) + "\n"
        for r in chunk:
            payload += json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = payload.encode("utf-8")
        with open(os.path.join(out_dir, rel), "wb") as f:
            f.write(data)
        directory.append({
            "block_id": bid, "path": rel, "rows": len(chunk),
            "code_min": chunk[0]["code"], "code_max": chunk[-1]["code"],
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    with open(input_path, "rb") as f:
        source_sha256 = hashlib.sha256(f.read()).hexdigest()
    layout = {
        "format_version": FORMAT_VERSION,
        "bits_per_axis": BITS,
        "block_rows": block_rows,
        "default_max_intervals": config.get("max_intervals", 8),
        "code_min": directory[0]["code_min"] if directory else None,
        "code_max": directory[-1]["code_max"] if directory else None,
        "summary": {
            "total_rows": len(rows),
            "num_blocks": len(directory),
            "source_path": os.path.abspath(input_path),
            "source_sha256": source_sha256,
            "sort_key": "(morton_code, row_id utf-8 bytes)",
            "morton": "16-bit, bit0=x0, bit1=y0, ... interleaved",
        },
        "directory": directory,
    }
    with open(os.path.join(out_dir, LAYOUT_FILE), "w", encoding="utf-8") as f:
        f.write(_dump_json(layout))
    return layout


# --------------------------------------------------------------- plan / query

def validate_rect(rect) -> list[int]:
    if len(rect) != 4:
        raise QueryError(f"矩形需 4 个端点 [x0,x1,y0,y1]，得到 {rect!r}")
    out = []
    for v in rect:
        if not _valid_int(v, 0, DOMAIN):
            raise QueryError(f"矩形端点必须是 0..{DOMAIN} 的整数，得到 {v!r}")
        out.append(v)
    return out  # [x0, x1, y0, y1]；x0>=x1 或 y0>=y1 为空区间，合法


def build_plan(layout: dict, rect, max_intervals: int) -> dict:
    x0, x1, y0, y1 = validate_rect(rect)
    exact, clamped = decompose_rect(x0, x1, y0, y1)
    intervals, merge_log = merge_to_budget(exact, max_intervals)
    selected = []
    for entry in layout["directory"]:
        hits = [[lo, hi] for lo, hi in intervals
                if lo <= entry["code_max"] and entry["code_min"] <= hi - 1]
        if hits:
            selected.append({"block_id": entry["block_id"],
                             "code_min": entry["code_min"],
                             "code_max": entry["code_max"],
                             "matched_intervals": hits})
    return {
        "rect": {"x": [x0, x1], "y": [y0, y1]},
        "clamped_rect": {"x": clamped[0:2], "y": clamped[2:4]},
        "empty": not intervals,
        "max_intervals": max_intervals,
        "decomposition": {
            "method": ("矩形与 256×256 整数域四叉网格相交；完全包含的胞格输出为一个 "
                       "左闭右开码区间 [prefix, prefix+4^(8-level))，相邻码连续区间精确合并"),
            "exact_intervals": [list(t) for t in exact],
            "exact_count": len(exact),
        },
        "merge_log": merge_log,
        "candidate_intervals": [list(t) for t in intervals],
        "selected_blocks": selected,
        "blocks_total": len(layout["directory"]),
    }


def execute_query(store_dir: str, rect, max_intervals: int) -> dict:
    """只打开候选块；码区间过滤得候选行，再用原 x/y 精确残差过滤。"""
    layout = load_layout(store_dir)
    plan = build_plan(layout, rect, max_intervals)
    intervals = [tuple(t) for t in plan["candidate_intervals"]]
    cx0, cx1 = plan["clamped_rect"]["x"]
    cy0, cy1 = plan["clamped_rect"]["y"]

    by_id = {e["block_id"]: e for e in layout["directory"]}
    read_log: list[dict] = []
    results: list[dict] = []
    for sel in plan["selected_blocks"]:
        entry = by_id[sel["block_id"]]
        rows = read_block(store_dir, entry)  # 损坏/绑定失败在此明确抛错
        cand = 0
        for r in rows:
            if intervals_contain(intervals, r["code"]):
                cand += 1  # 候选行：码落在（可能保守的）候选区间内
                if cx0 <= r["x"] < cx1 and cy0 <= r["y"] < cy1:
                    results.append({"row_id": r["row_id"], "x": r["x"],
                                    "y": r["y"], "value": r["value"]})
        read_log.append({"block_id": entry["block_id"], "path": entry["path"],
                         "rows_read": len(rows), "candidate_rows": cand})

    results.sort(key=lambda r: r["row_id"].encode("utf-8"))
    return {
        "rect": plan["rect"],
        "max_intervals": max_intervals,
        "candidate_intervals": plan["candidate_intervals"],
        "blocks_total": len(layout["directory"]),
        "blocks_opened": [e["block_id"] for e in read_log],
        "read_log": read_log,
        "candidate_rows_total": sum(e["candidate_rows"] for e in read_log),
        "result_rows": len(results),
        "results": results,
    }


# -------------------------------------------------------------------- verify

def verify_store(store_dir: str) -> dict:
    """全量校验布局与所有块；任何问题抛 StoreError/CorruptBlockError。"""
    layout = load_layout(store_dir)
    directory = layout["directory"]
    ids = [e["block_id"] for e in directory]
    if ids != list(range(len(directory))):
        raise StoreError(f"块目录 block_id 不是 0..n-1 连续序列: {ids[:8]}…")
    paths = [e["path"] for e in directory]
    if len(set(paths)) != len(paths):
        raise StoreError("块目录存在重复 path")
    total = 0
    prev_max = -1
    for entry in directory:
        for key in ("block_id", "path", "rows", "code_min", "code_max", "sha256"):
            if key not in entry:
                raise StoreError(f"目录条目缺字段 {key!r}: {entry}")
        if not (0 <= entry["code_min"] <= entry["code_max"] < CODE_SPACE):
            raise StoreError(f"块 {entry['block_id']}: 目录码界限越界 "
                             f"[{entry['code_min']},{entry['code_max']}]")
        if entry["code_min"] < prev_max:
            raise StoreError(f"块 {entry['block_id']}: 目录码界限未按排序键单调排列")
        prev_max = entry["code_max"]
        rows = read_block(store_dir, entry)  # 含 hash/绑定/行序/界限全部校验
        total += len(rows)
    if total != layout["summary"]["total_rows"]:
        raise StoreError(f"summary.total_rows={layout['summary']['total_rows']} 与实际 {total} 不符")
    if layout["summary"]["num_blocks"] != len(directory):
        raise StoreError("summary.num_blocks 与目录长度不符")
    exp_min = directory[0]["code_min"] if directory else None
    exp_max = directory[-1]["code_max"] if directory else None
    if layout["code_min"] != exp_min or layout["code_max"] != exp_max:
        raise StoreError(f"布局全局码界限 [{layout['code_min']},{layout['code_max']}] 与目录 "
                         f"[{exp_min},{exp_max}] 不符")
    return {"ok": True, "blocks": len(directory), "total_rows": total,
            "code_min": layout["code_min"], "code_max": layout["code_max"]}


# ------------------------------------------------------------------ selftest

def _project(rows: list[dict]) -> list[dict]:
    return [{"row_id": r["row_id"], "x": r["x"], "y": r["y"], "value": r["value"]}
            for r in rows]


def oracle_filter(source_rows: list[dict], rect) -> list[dict]:
    """独立 oracle：对原 JSONL 逐点过滤，按 row_id 字节序排序。"""
    x0, x1, y0, y1 = rect
    cx0, cx1 = max(0, x0), min(DOMAIN, x1)
    cy0, cy1 = max(0, y0), min(DOMAIN, y1)
    out = [r for r in _project(source_rows)
           if cx0 <= r["x"] < cx1 and cy0 <= r["y"] < cy1]
    out.sort(key=lambda r: r["row_id"].encode("utf-8"))
    return out


def _coverage_misses(rect, intervals) -> list[tuple[int, int]]:
    """枚举 8 位域内矩形全部整数点，返回未被任何区间覆盖的点（必须为空）。"""
    x0, x1, y0, y1 = rect
    cx0, cx1 = max(0, x0), min(DOMAIN, x1)
    cy0, cy1 = max(0, y0), min(DOMAIN, y1)
    misses = []
    for x in range(cx0, cx1):
        for y in range(cy0, cy1):
            if not intervals_contain(intervals, morton_encode(x, y)):
                misses.append((x, y))
    return misses


def _intervals_sane(intervals) -> str | None:
    prev_hi = 0
    for lo, hi in intervals:
        if not (0 <= lo < hi <= CODE_SPACE):
            return f"区间越界 [{lo},{hi})"
        if lo < prev_hi:
            return f"区间重叠或乱序于 [{lo},{hi})"
        prev_hi = hi
    return None


def cmd_selftest(args) -> int:
    layout = load_layout(args.store)
    queries = json.load(open(args.queries, encoding="utf-8"))
    with open(args.input, encoding="utf-8") as f:
        source_rows = [json.loads(line) for line in f if line.strip()]
    checks: list[dict] = []

    def check(name: str, ok: bool, detail=None):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    # 1) Morton 码基本性质
    rt = all(morton_decode(morton_encode(x, y)) == (x, y)
             for x in range(DOMAIN) for y in range(DOMAIN))
    check("morton_roundtrip_all_65536", rt)
    check("morton_bit_order",
          morton_encode(1, 0) == 1 and morton_encode(0, 1) == 2
          and morton_encode(255, 255) == CODE_SPACE - 1,
          {"enc(1,0)": morton_encode(1, 0), "enc(0,1)": morton_encode(0, 1)})

    # 2) 布局完整性
    try:
        rep = verify_store(args.store)
        check("store_verify", True, rep)
    except StoreError as e:
        check("store_verify", False, str(e))

    budgets = [1, 2, 3, 4, 8, 16, 64, 1024]
    query_runs: dict[str, dict] = {}

    # 3) 现成查询：结果 == oracle（每个预算），plan 枚举全域点无漏覆盖
    for q in queries:
        rect = [q["x"][0], q["x"][1], q["y"][0], q["y"][1]]
        expected = oracle_filter(source_rows, rect)
        per_budget, cov_fail, sane_fail, exact_fail = {}, [], [], []
        for b in budgets:
            res = execute_query(args.store, rect, b)
            plan = build_plan(layout, rect, b)
            intervals = [tuple(t) for t in plan["candidate_intervals"]]
            ok = res["results"] == expected
            per_budget[str(b)] = {"match_oracle": ok,
                                  "result_rows": res["result_rows"],
                                  "blocks_opened": len(res["blocks_opened"])}
            misses = _coverage_misses(rect, intervals)
            if misses:
                cov_fail.append({"budget": b, "misses": misses[:5]})
            s = _intervals_sane(intervals)
            if s:
                sane_fail.append({"budget": b, "problem": s})
            if not plan["merge_log"]:  # 未合并时必须恰好覆盖，不多不少
                covered = set()
                for lo, hi in intervals:
                    covered.update(range(lo, hi))
                want = {morton_encode(x, y)
                        for x in range(max(0, rect[0]), min(DOMAIN, rect[1]))
                        for y in range(max(0, rect[2]), min(DOMAIN, rect[3]))}
                if covered != want:
                    exact_fail.append({"budget": b,
                                       "extra": len(covered - want),
                                       "missing": len(want - covered)})
            if b == max(budgets):
                query_runs[q["id"]] = res
        all_ok = (all(v["match_oracle"] for v in per_budget.values())
                  and not cov_fail and not sane_fail and not exact_fail)
        check(f"fixture_query[{q['id']}]_all_budgets_match_oracle", all_ok,
              {"rect": rect, "per_budget": per_budget,
               "coverage_failures": cov_fail, "sanity_failures": sane_fail,
               "exact_cover_failures": exact_fail})

    # 4) 预算不变性：同一查询不同预算结果逐一相同
    invariance_ok = True
    invariance_detail = {}
    for q in queries:
        rect = [q["x"][0], q["x"][1], q["y"][0], q["y"][1]]
        results_by_budget = [execute_query(args.store, rect, b)["results"] for b in budgets]
        same = all(r == results_by_budget[0] for r in results_by_budget)
        invariance_ok = invariance_ok and same
        invariance_detail[q["id"]] = same
    check("budget_invariance_fixture_queries", invariance_ok, invariance_detail)

    # 5) 固定种子随机小矩形 + 边界/条带矩形
    rng = random.Random(20261115)
    widths = [0, 1, 2, 3, 5, 8, 13, 21]
    rand_rects = []
    for _ in range(300):
        x0 = rng.randint(0, 255)
        y0 = rng.randint(0, 255)
        rand_rects.append([x0, min(DOMAIN, x0 + rng.choice(widths)),
                           y0, min(DOMAIN, y0 + rng.choice(widths))])
    rand_rects += [[0, 256, 0, 256], [255, 256, 255, 256], [0, 1, 0, 256],
                   [128, 129, 0, 256], [250, 256, 250, 256], [7, 7, 9, 200]]
    rand_budgets = [1, 4, 8, 64]
    rand_fail = []
    for rect in rand_rects:
        expected = oracle_filter(source_rows, rect)
        for b in rand_budgets:
            res = execute_query(args.store, rect, b)
            if res["results"] != expected:
                rand_fail.append({"rect": rect, "budget": b, "kind": "result_mismatch"})
            plan = build_plan(layout, rect, b)
            intervals = [tuple(t) for t in plan["candidate_intervals"]]
            misses = _coverage_misses(rect, intervals)
            if misses:
                rand_fail.append({"rect": rect, "budget": b,
                                  "kind": "coverage_miss", "misses": misses[:5]})
            s = _intervals_sane(intervals)
            if s:
                rand_fail.append({"rect": rect, "budget": b, "kind": "insane", "p": s})
    check(f"random_rects_seed20261115_n{len(rand_rects)}_x{len(rand_budgets)}budgets",
          not rand_fail, {"failures": rand_fail[:10], "rects": len(rand_rects)})

    # 6) tiny 查询实际少读块（读取记录来自真实 query 执行，非 plan 推测）
    tiny = next(q for q in queries if q["id"] == "tiny")
    tiny_rect = [tiny["x"][0], tiny["x"][1], tiny["y"][0], tiny["y"][1]]
    tiny_res = execute_query(args.store, tiny_rect, layout["default_max_intervals"])
    opened, total = len(tiny_res["blocks_opened"]), tiny_res["blocks_total"]
    check("tiny_query_reads_fewer_blocks", 0 < opened < total,
          {"blocks_opened": opened, "blocks_total": total,
           "read_log": tiny_res["read_log"]})
    query_runs["tiny"] = tiny_res

    # 7) 重复坐标身份保留：fixtures 中 (0,0) 有两条事件
    dup_events = [r for r in query_runs["full"]["results"] if r["x"] == 0 and r["y"] == 0]
    src_dup = [r for r in source_rows if r["x"] == 0 and r["y"] == 0]
    check("duplicate_coordinate_identity_(0,0)",
          len(dup_events) == len(src_dup) == 2
          and {r["row_id"] for r in dup_events} == {r["row_id"] for r in src_dup},
          {"returned": [r["row_id"] for r in dup_events]})

    # 8) 空查询：无区间、不开块、结果为空
    empty_res = query_runs["empty"]
    check("empty_query_opens_nothing",
          empty_res["blocks_opened"] == [] and empty_res["results"] == []
          and empty_res["candidate_intervals"] == [],
          {"blocks_opened": empty_res["blocks_opened"]})

    # 9) 损坏注入：bit-flip / 块文件调换 / 目录篡改，都必须明确失败
    def expect_store_error(fn, kinds):
        try:
            fn()
            return False, "未抛错"
        except kinds as e:
            return True, f"{type(e).__name__}: {e}"

    full_rect = [0, 256, 0, 256]
    with tempfile.TemporaryDirectory() as tmp:
        tampered = os.path.join(tmp, "flip")
        shutil.copytree(args.store, tampered)
        victim = os.path.join(tampered, "blocks", "block-0001.jsonl")
        raw = bytearray(open(victim, "rb").read())
        pos = raw.index(b"\n") + 5  # 块头之后的 payload 区
        raw[pos] = raw[pos] ^ 0x01
        open(victim, "wb").write(bytes(raw))
        ok, msg = expect_store_error(lambda: execute_query(tampered, full_rect, 8),
                                     CorruptBlockError)
        check("corruption_bitflip_fails_clearly", ok, msg)

        swapped = os.path.join(tmp, "swap")
        shutil.copytree(args.store, swapped)
        b0 = os.path.join(swapped, "blocks", "block-0000.jsonl")
        b1 = os.path.join(swapped, "blocks", "block-0001.jsonl")
        os.replace(b0, b0 + ".tmp")
        os.replace(b1, b0)
        os.replace(b0 + ".tmp", b1)
        ok, msg = expect_store_error(lambda: execute_query(swapped, full_rect, 8),
                                     CorruptBlockError)
        check("corruption_swapped_blocks_fail_clearly", ok, msg)

        lied = os.path.join(tmp, "lied")
        shutil.copytree(args.store, lied)
        lay = load_layout(lied)
        lay["directory"][0]["code_min"] = max(0, lay["directory"][0]["code_min"] - 1) \
            if lay["directory"][0]["code_min"] > 0 else lay["directory"][0]["code_min"] + 1
        with open(os.path.join(lied, LAYOUT_FILE), "w", encoding="utf-8") as f:
            f.write(_dump_json(lay))
        ok1, msg1 = expect_store_error(lambda: verify_store(lied), StoreError)
        ok2, msg2 = expect_store_error(lambda: execute_query(lied, full_rect, 8),
                                       CorruptBlockError)
        check("tampered_directory_binding_fails", ok1 and ok2,
              {"verify": msg1, "query": msg2})

    # 10) build 输入校验：重复 ID / 坐标越界 / 类型错误
    bad_cases = {
        "duplicate_row_id": '{"row_id":"a","x":1,"y":1,"value":1}\n'
                            '{"row_id":"a","x":2,"y":2,"value":2}\n',
        "x_out_of_range_256": '{"row_id":"a","x":256,"y":1,"value":1}\n',
        "x_negative": '{"row_id":"a","x":-1,"y":1,"value":1}\n',
        "bool_coordinate": '{"row_id":"a","x":true,"y":1,"value":1}\n',
        "non_integer_value": '{"row_id":"a","x":1,"y":1,"value":1.5}\n',
        "missing_field": '{"row_id":"a","x":1,"y":1}\n',
    }
    bad_results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, payload in bad_cases.items():
            src = os.path.join(tmp, name + ".jsonl")
            open(src, "w", encoding="utf-8").write(payload)
            ok, msg = expect_store_error(
                lambda s=src: build_store(s, os.path.join(tmp, "out_" + name), {}),
                BuildError)
            bad_results[name] = {"rejected": ok, "error": msg}
    check("build_input_validation", all(v["rejected"] for v in bad_results.values()),
          bad_results)

    report = {
        "store": os.path.abspath(args.store),
        "source": os.path.abspath(args.input),
        "budgets_tested": budgets,
        "random_seed": 20261115,
        "random_rects": len(rand_rects),
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks),
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(_dump_json(report))
    n_pass = sum(1 for c in checks if c["pass"])
    print(f"selftest: {n_pass}/{len(checks)} 项通过"
          + ("，全部通过 ✔" if report["all_pass"] else "，存在失败 �’"))
    for c in checks:
        if not c["pass"]:
            print(f"  FAIL {c['name']}: {json.dumps(c['detail'], ensure_ascii=False)[:400]}")
    return 0 if report["all_pass"] else 1


# ----------------------------------------------------------------------- cli

def _load_queries_and_rect(args):
    if args.rect is not None:
        return args.id or "adhoc", args.rect
    queries = json.load(open(args.queries, encoding="utf-8"))
    for q in queries:
        if q["id"] == args.id:
            return q["id"], [q["x"][0], q["x"][1], q["y"][0], q["y"][1]]
    raise QueryError(f"在 {args.queries} 中找不到查询 id={args.id!r}")


def _resolve_budget(args, layout) -> int:
    if args.max_intervals is not None:
        return args.max_intervals
    return layout.get("default_max_intervals", 8)


def cmd_build(args) -> int:
    config = {}
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
    layout = build_store(args.input, args.out, config, args.block_rows)
    s = layout["summary"]
    print(f"build 完成: {s['total_rows']} 行 -> {s['num_blocks']} 块 "
          f"(每块 <= {layout['block_rows']} 行), 码域 [{layout['code_min']},{layout['code_max']}], "
          f"布局: {os.path.join(args.out, LAYOUT_FILE)}")
    return 0


def cmd_plan(args) -> int:
    layout = load_layout(args.store)
    budget = _resolve_budget(args, layout)
    if args.all:
        queries = json.load(open(args.queries, encoding="utf-8"))
        os.makedirs(args.out, exist_ok=True)
        for q in queries:
            rect = [q["x"][0], q["x"][1], q["y"][0], q["y"][1]]
            plan = build_plan(layout, rect, budget)
            plan["query_id"] = q["id"]
            path = os.path.join(args.out, f"plan_{q['id']}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_dump_json(plan))
            print(f"plan[{q['id']}]: 精确区间 {plan['decomposition']['exact_count']} 个, "
                  f"合并后 {len(plan['candidate_intervals'])} 个, "
                  f"选中块 {len(plan['selected_blocks'])}/{plan['blocks_total']} -> {path}")
        return 0
    qid, rect = _load_queries_and_rect(args)
    plan = build_plan(layout, rect, budget)
    plan["query_id"] = qid
    text = _dump_json(plan)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text, end="")
    return 0


def cmd_query(args) -> int:
    layout = load_layout(args.store)
    budget = _resolve_budget(args, layout)
    if args.all:
        queries = json.load(open(args.queries, encoding="utf-8"))
        os.makedirs(args.out, exist_ok=True)
        for q in queries:
            rect = [q["x"][0], q["x"][1], q["y"][0], q["y"][1]]
            res = execute_query(args.store, rect, budget)
            res["query_id"] = q["id"]
            path = os.path.join(args.out, f"query_{q['id']}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_dump_json(res))
            print(f"query[{q['id']}]: 打开块 {len(res['blocks_opened'])}/{res['blocks_total']}, "
                  f"候选行 {res['candidate_rows_total']}, 结果 {res['result_rows']} 行 -> {path}")
        return 0
    qid, rect = _load_queries_and_rect(args)
    res = execute_query(args.store, rect, budget)
    res["query_id"] = qid
    text = _dump_json(res)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text, end="")
    return 0


def cmd_verify(args) -> int:
    rep = verify_store(args.store)
    print(_dump_json(rep), end="")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="从 JSONL 构建不可变块与块目录")
    p.add_argument("--input", required=True)
    p.add_argument("--config", help='JSON，如 {"bits_per_axis":8,"rows_per_block":16,"max_intervals":8}')
    p.add_argument("--out", required=True)
    p.add_argument("--block-rows", type=int, default=None, help="覆盖 config 的 rows_per_block")
    p.set_defaults(fn=cmd_build)

    def add_q(sp):
        sp.add_argument("--store", required=True)
        sp.add_argument("--queries", help="查询清单 JSON（配合 --id 或 --all）")
        sp.add_argument("--id", help="查询清单中的 id")
        sp.add_argument("--rect", type=int, nargs=4, metavar=("X0", "X1", "Y0", "Y1"),
                        help="左闭右开区间端点，0..256")
        sp.add_argument("--max-intervals", type=int, default=None,
                        help="码区间预算，默认取布局中的 default_max_intervals")
        sp.add_argument("--all", action="store_true", help="对清单中全部查询执行，--out 作为目录")
        sp.add_argument("--out", help="输出文件（--all 时为目录）")

    p = sub.add_parser("plan", help="展示分解/合并依据、候选码区间与选中块")
    add_q(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("query", help="只打开候选块并残差过滤，输出按 row_id 字节序")
    add_q(p)
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("verify", help="校验布局与全部块的完整性/索引绑定")
    p.add_argument("--store", required=True)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("selftest", help="oracle 核对 + 覆盖枚举 + 预算不变性 + 损坏注入")
    p.add_argument("--store", required=True)
    p.add_argument("--input", required=True, help="原始 JSONL（oracle 数据源）")
    p.add_argument("--queries", required=True)
    p.add_argument("--out", help="验证报告 JSON 路径")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (StoreError, BuildError, QueryError, CorruptBlockError) as e:
        print(f"错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
