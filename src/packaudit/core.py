"""
packaudit.core
==============

定长训练块打包 (pack) 与可回查审计 (audit) 的共享实现。

术语
----
一篇文档 d 的**逻辑序列**（长度 n+2，n 为原 tokens 长度）::

    logical = [BOS] + tokens + [EOS]
    逻辑位置 lp = 0, 1, ..., n+1

监督边为相邻对 (lp-1 -> lp), lp=1..n+1。空文也有一条 BOS -> EOS。

打包后每个位置都带来源 (doc_id, logical_position)；PAD 的来源为 NULL。
所有判断一律以来源为准，token 数值相同不代表来源相同。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

# 保留符号（规格固定值；config 可覆盖，但必须互不相同且落在 3 以下）
DEFAULT_BOS_ID = 1
DEFAULT_EOS_ID = 2
DEFAULT_PAD_ID = 0
IGNORE_INDEX = -100  # 框架 shifted labels 中常用的“不计算损失”占位


class PackError(ValueError):
    """输入不合法时抛出（非法 token、重复 doc_id、配置错误等）。"""


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _is_plain_int(x: Any) -> bool:
    # bool 是 int 的子类，必须显式排除
    return isinstance(x, int) and not isinstance(x, bool)


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise PackError("config 必须是 JSON 对象")

    L = cfg.get("block_length", cfg.get("L"))
    if not _is_plain_int(L) or L < 3:
        raise PackError("block_length(=L) 必须是 >= 3 的整数")
    bos = cfg.get("bos_id", DEFAULT_BOS_ID)
    eos = cfg.get("eos_id", DEFAULT_EOS_ID)
    pad = cfg.get("pad_id", DEFAULT_PAD_ID)
    max_tok = cfg.get("max_token_id")
    for name, v in (("bos_id", bos), ("eos_id", eos), ("pad_id", pad),
                    ("max_token_id", max_tok)):
        if not _is_plain_int(v):
            raise PackError(f"config.{name} 必须是整数")
    if len({bos, eos, pad}) != 3:
        raise PackError("bos_id / eos_id / pad_id 必须互不相同")
    if min(bos, eos, pad) < 0 or max(bos, eos, pad) >= 3:
        raise PackError("保留符号 BOS/EOS/PAD 必须取自 0..2（合法 token 从 3 起）")
    if max_tok < 3:
        raise PackError("max_token_id 必须 >= 3")
    return {
        "block_length": int(L),
        "bos_id": int(bos),
        "eos_id": int(eos),
        "pad_id": int(pad),
        "max_token_id": int(max_tok),
    }


def load_documents(path: str | Path, max_token_id: int) -> list[dict[str, Any]]:
    """读取并校验 documents.jsonl，严格保留输入顺序。"""
    docs: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                raise PackError(f"第 {lineno} 行不是合法 JSON: {e}") from e
            if not isinstance(rec, dict) or "doc_id" not in rec or "tokens" not in rec:
                raise PackError(f"第 {lineno} 行必须是含 doc_id/tokens 的对象")

            doc_id = rec["doc_id"]
            if isinstance(doc_id, bool) or not isinstance(doc_id, (str, int)):
                raise PackError(f"第 {lineno} 行 doc_id 只能是字符串或整数（bool 非法）")
            if doc_id in seen_ids:
                raise PackError(f"重复 doc_id: {doc_id!r}（第 {lineno} 行）")
            seen_ids.add(doc_id)

            tokens = rec["tokens"]
            if not isinstance(tokens, list):
                raise PackError(f"doc {doc_id!r}: tokens 必须是数组")
            for j, t in enumerate(tokens):
                if not _is_plain_int(t):
                    kind = "bool" if isinstance(t, bool) else type(t).__name__
                    raise PackError(
                        f"doc {doc_id!r} tokens[{j}] 不是整数（{kind} 非法）"
                    )
                if t < 3 or t > max_token_id:
                    raise PackError(
                        f"doc {doc_id!r} tokens[{j}]={t} 超出合法区间 "
                        f"[3, {max_token_id}]（保留符号 0/1/2 不得出现在 tokens 中）"
                    )
            docs.append({"doc_id": doc_id, "tokens": [int(t) for t in tokens]})
    return docs


# --------------------------------------------------------------------------- #
# pack
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    block_id: int
    input_ids: list[int]
    loss_mask: list[bool]
    segment_ids: list[int]
    source_doc_id: list[Any]
    source_logical_position: list[int | None]


def _freeze(b: Block) -> Block:
    """快照入列：后续对工作块的 append 不得回写已生成的块。"""
    return Block(
        b.block_id,
        list(b.input_ids),
        list(b.loss_mask),
        list(b.segment_ids),
        list(b.source_doc_id),
        list(b.source_logical_position),
    )


def _fresh_block(block_id: int) -> Block:
    return Block(block_id, [], [], [], [], [])


def _logical_value(tokens: list[int], lp: int, bos: int, eos: int) -> int:
    if lp == 0:
        return bos
    if lp == len(tokens) + 1:
        return eos
    return tokens[lp - 1]


def pack_documents(docs: list[dict[str, Any]], cfg: dict[str, Any]) -> list[Block]:
    """
    严格按输入顺序贪心定长装块。

    规则要点：
      * 每篇逻辑序列 = BOS + tokens + EOS；空文也有 BOS->EOS 监督边。
      * 新文开始时若块内只剩 1 个位置 -> PAD 补齐，新文从下一块开始，
        孤立 BOS 不允许进末位。
      * 一块填满而该文未结束 -> 下一块首位重复上一块该文最后一个 token
        作为上下文（占位置、mask=0、来源同一逻辑位置，不产生新监督边）。
      * 末块 PAD 补足；不做最优装箱、不做数据集划分。
    """
    L = cfg["block_length"]
    bos, eos, pad = cfg["bos_id"], cfg["eos_id"], cfg["pad_id"]

    blocks: list[Block] = []
    cur = _fresh_block(0)
    pos = 0            # 当前块已用位置数
    seg_in_block = 0   # 块内已分配的 segment 数（编号从 0 起，每块重置）

    def place(value: int, mask: bool, seg: int,
              doc_id: Any, lp: int | None) -> None:
        cur.input_ids.append(value)
        cur.loss_mask.append(mask)
        cur.segment_ids.append(seg)
        cur.source_doc_id.append(doc_id)
        cur.source_logical_position.append(lp)

    def pad_and_flush() -> None:
        nonlocal pos
        while pos < L:
            place(pad, False, cur.segment_ids[-1] if cur.segment_ids else 0,
                  None, None)
            pos += 1
        blocks.append(_freeze(cur))

    def start_fresh_block() -> None:
        nonlocal cur, pos, seg_in_block
        cur = _fresh_block(len(blocks))
        pos = 0
        seg_in_block = 0

    for doc in docs:
        doc_id, tokens = doc["doc_id"], doc["tokens"]
        last_lp = len(tokens) + 1  # EOS 的逻辑位置

        # ---- 新文开始：只剩一个位置时禁止放孤立 BOS，PAD 收尾后换块 ----
        if pos == L - 1:
            pad_and_flush()
            start_fresh_block()

        seg = seg_in_block
        seg_in_block += 1

        # BOS：块首（pos==0）或接在同块前文之后，均不是监督目标
        place(bos, False, seg, doc_id, 0)
        pos += 1

        # 同文其余逻辑 token（原 tokens + EOS），每个都是监督目标
        lp = 1
        while lp <= last_lp:
            place(_logical_value(tokens, lp, bos, eos),
                  True, seg, doc_id, lp)  # 含 EOS
            pos += 1
            lp += 1

            if pos == L and lp <= last_lp:
                # 放置后块已满且该文还有后续逻辑 token：收尾满块，
                # 新块首位重复上一块该文最后一个 token 作为上下文
                # （占位置、mask=0、来源同一逻辑位置，不产生新监督边）
                blocks.append(_freeze(cur))
                start_fresh_block()
                place(_logical_value(tokens, lp - 1, bos, eos),
                      False, 0, doc_id, lp - 1)
                pos = 1
                seg_in_block = 1
                seg = 0  # 续写后该文在新块内的 segment 编号为 0

        # 该文结束时若恰好把块填满（EOS 落在末位），立即收尾，
        # 否则下一篇文档会继续写进已满的块
        if pos == L:
            blocks.append(_freeze(cur))
            start_fresh_block()

    if pos > 0:
        pad_and_flush()

    return blocks


# --------------------------------------------------------------------------- #
# Parquet 读写
# --------------------------------------------------------------------------- #
def _doc_arrow_type(docs: list[dict[str, Any]]) -> pa.DataType:
    if docs and isinstance(docs[0]["doc_id"], str):
        return pa.string()
    return pa.int64()


def write_parquet(blocks: list[Block], out_path: str | Path,
                  doc_type: pa.DataType) -> pa.Schema:
    schema = pa.schema([
        pa.field("block_id", pa.int64(), nullable=False),
        pa.field("input_ids", pa.list_(pa.int64()), nullable=False),
        pa.field("loss_mask", pa.list_(pa.bool_()), nullable=False),
        pa.field("segment_ids", pa.list_(pa.int32()), nullable=False),
        pa.field("source_doc_id", pa.list_(doc_type), nullable=False),
        pa.field("source_logical_position", pa.list_(pa.int32()), nullable=False),
    ])
    table = pa.table({
        "block_id": [b.block_id for b in blocks],
        "input_ids": [b.input_ids for b in blocks],
        "loss_mask": [b.loss_mask for b in blocks],
        "segment_ids": [b.segment_ids for b in blocks],
        "source_doc_id": [b.source_doc_id for b in blocks],
        "source_logical_position": [b.source_logical_position for b in blocks],
    }, schema=schema)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="snappy")
    return schema


def read_parquet(path: str | Path) -> list[dict[str, Any]]:
    table = pq.read_table(path)
    rows = table.to_pylist()
    return rows


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def _expected_logical(docs: list[dict[str, Any]], cfg: dict[str, Any]):
    """独立重建每篇逻辑序列的值表与全部监督边。"""
    bos, eos = cfg["bos_id"], cfg["eos_id"]
    values: dict[Any, list[int]] = {}
    edges: set[tuple[Any, int]] = set()       # (doc_id, 目标逻辑位置)
    for d in docs:
        logical = [bos] + d["tokens"] + [eos]
        values[d["doc_id"]] = logical
        for target_lp in range(1, len(logical)):
            edges.add((d["doc_id"], target_lp))
    return values, edges


def audit_blocks(rows: list[dict[str, Any]],
                 docs: list[dict[str, Any]],
                 cfg: dict[str, Any]) -> dict[str, Any]:
    """
    独立审计实际产物：
      1. 结构/取值/来源逐位置核对；
      2. 枚举实际 mask=1 的监督边，与原文档独立枚举的边集合比对；
      3. 检查跨文边、重复目标、上下文重复被误算等。
    """
    L = cfg["block_length"]
    bos, eos, pad = cfg["bos_id"], cfg["eos_id"], cfg["pad_id"]
    logical_values, expected_edges = _expected_logical(docs, cfg)
    doc_order = [d["doc_id"] for d in docs]

    violations: list[str] = []
    observed_edges: set[tuple[Any, int]] = set()
    seen_targets: set[tuple[Any, int]] = set()
    n_supervised = 0
    n_context_repeat = 0
    n_pad = 0
    n_cross_doc_edges = 0
    block_reports: list[dict[str, Any]] = []

    def addv(msg: str) -> None:
        violations.append(msg)

    if not rows:
        addv("Parquet 中没有任何块")

    for bi, row in enumerate(rows):
        ids = row["input_ids"]
        mask = row["loss_mask"]
        segs = row["segment_ids"]
        src_doc = row["source_doc_id"]
        src_lp = row["source_logical_position"]
        br = {"block_id": row.get("block_id", bi), "positions": []}

        for name, arr in (("loss_mask", mask), ("segment_ids", segs),
                          ("source_doc_id", src_doc),
                          ("source_logical_position", src_lp)):
            if not isinstance(arr, list) or len(arr) != len(ids):
                addv(f"块 {bi}: 字段 {name} 长度与 input_ids 不一致")
        if len(ids) != L:
            addv(f"块 {bi}: 长度 {len(ids)} != L={L}")
        if row.get("block_id") != bi:
            addv(f"块 {bi}: block_id={row.get('block_id')} 与顺序不符")

        # 块首必须是 BOS 或重复上下文；第 0 块首位必须是第一篇的 BOS
        if ids:
            if src_doc[0] is None:
                addv(f"块 {bi}: 块首是 PAD，不允许整块为空块")
            elif src_lp[0] == 0:
                if ids[0] != bos:
                    addv(f"块 {bi} 位置0: 逻辑位置0 的值应为 BOS={bos}")
                if bi == 0 and (not doc_order or src_doc[0] != doc_order[0]):
                    addv(f"块 {bi} 位置0: 首块必须以第一篇文档的 BOS 开始")
            else:
                if bi == 0:
                    addv(f"块 {bi} 位置0: 首块首位必须是 BOS，不能是重复上下文")
                n_context_repeat += 1

        pad_started = False
        for p in range(len(ids)):
            v, m = ids[p], bool(mask[p])
            d, lp = src_doc[p], src_lp[p]
            note = ""

            # ---- PAD：只能出现在块尾连续段，来源 NULL，mask=0 ----
            if v == pad:
                n_pad += 1
                pad_started = True
                if d is not None or lp is not None:
                    addv(f"块 {bi} 位置{p}: PAD 来源必须为 NULL")
                if m:
                    addv(f"块 {bi} 位置{p}: PAD 的 loss_mask 必须为 0")
                br["positions"].append({"p": p, "token": v, "mask": m,
                                        "segment": segs[p], "doc": None,
                                        "lp": None, "kind": "PAD"})
                continue
            if pad_started:
                addv(f"块 {bi} 位置{p}: PAD 之后又出现真实 token，PAD 必须在块尾")
            if d is None or lp is None:
                addv(f"块 {bi} 位置{p}: 真实 token 缺少来源 (doc_id/lp)")
                continue
            if d not in logical_values:
                addv(f"块 {bi} 位置{p}: 来源 doc_id={d!r} 不在原文档中")
                continue

            # ---- 值必须与该来源逻辑位置一致（同值不同来源不可混用）----
            logical = logical_values[d]
            if not (0 <= lp < len(logical)):
                addv(f"块 {bi} 位置{p}: 逻辑位置 lp={lp} 越界（doc={d!r}）")
                continue
            if v != logical[lp]:
                addv(f"块 {bi} 位置{p}: token={v} 与来源 "
                     f"(doc={d!r}, lp={lp}) 应有的值 {logical[lp]} 不符")

            is_bos = (lp == 0)
            is_eos = (lp == len(logical) - 1)
            is_block_head = (p == 0)

            # 是否为重复上下文：来源逻辑位置与前一位置相同
            is_repeat = False
            if p > 0 and src_doc[p - 1] == d and src_lp[p - 1] == lp:
                is_repeat = True
            if is_repeat:
                n_context_repeat += 1
                note = "context-repeat"

            # ---- mask 规则：BOS / 块首 / 重复上下文 / PAD -> 0；其余 -> 1 ----
            must_mask = is_bos or is_block_head or is_repeat
            if must_mask and m:
                addv(f"块 {bi} 位置{p} (doc={d!r},lp={lp}): "
                     f"该位置必须 mask=0（bos={is_bos}, head={is_block_head}, "
                     f"repeat={is_repeat}）却为 1，上下文重复被误算为目标")
            if (not must_mask) and not m:
                addv(f"块 {bi} 位置{p} (doc={d!r},lp={lp}): "
                     f"同文真实监督目标（含 EOS）的 mask 必须为 1")
            if is_eos and not m:
                addv(f"块 {bi} 位置{p}: EOS 必须是监督目标（mask=1）")

            # ---- 孤立 BOS 检查：BOS 不得在块末位，且块内后继必须同文 ----
            if is_bos and p == L - 1:
                addv(f"块 {bi} 位置{p}: 孤立 BOS 被塞入块末位，应先 PAD 补齐再换块")
            if is_bos and p + 1 < len(ids) and ids[p + 1] != pad:
                if src_doc[p + 1] != d or src_lp[p + 1] is None or src_lp[p + 1] == 0:
                    addv(f"块 {bi} 位置{p}: BOS 后必须紧跟同文的下一个逻辑 token")

            kind = "BOS" if is_bos else ("EOS" if is_eos else "token")
            if is_repeat:
                kind = "context-repeat"
            br["positions"].append({"p": p, "token": v, "mask": m,
                                    "segment": segs[p], "doc": d, "lp": lp,
                                    "kind": kind})

            # ---- 监督边枚举（仅 mask=1）----
            if m:
                n_supervised += 1
                if p == 0:
                    addv(f"块 {bi} 位置{p}: 块首不可能有前驱，mask 必须为 0")
                    continue
                prev_doc, prev_lp = src_doc[p - 1], src_lp[p - 1]
                if prev_doc != d:
                    n_cross_doc_edges += 1
                    addv(f"块 {bi} 位置{p}: 出现跨文档监督边 "
                         f"{prev_doc!r} -> {d!r}，拼接不得产生监督边")
                    continue
                if prev_lp is None or lp != prev_lp + 1:
                    addv(f"块 {bi} 位置{p}: 监督边逻辑位置不相邻 "
                         f"lp {prev_lp} -> {lp}（doc={d!r}）")
                    continue
                key = (d, lp)
                if key in seen_targets:
                    addv(f"块 {bi} 位置{p}: 监督目标 (doc={d!r}, lp={lp}) "
                         f"被重复计算（跨块上下文重复未正确屏蔽）")
                seen_targets.add(key)
                observed_edges.add(key)

        # ---- segment 检查：同块同文连续同 segment，编号从 0 递增 ----
        runs: list[tuple[Any, int, int, int]] = []
        for p in range(len(ids)):
            if ids[p] == pad:
                continue
            d, s = src_doc[p], segs[p]
            if runs and runs[-1][0] == d and runs[-1][3] == s:
                runs[-1] = (d, runs[-1][1], p, s)
            else:
                runs.append((d, p, p, s))
        expected_seg = 0
        for d, p0, p1, s in runs:
            if s != expected_seg:
                addv(f"块 {bi}: segment 编号应从 0 连续递增，"
                     f"doc={d!r} 段拿到 {s}，期望 {expected_seg}")
            expected_seg += 1
        block_reports.append(br)

    # ---- 跨块衔接：除首块外，块首只能是 (a) 上一块末位真实 token 的重复上下文
    #      或 (b) 一篇新文档的 BOS（上一块已被 PAD 收尾）----
    for bi in range(1, len(rows)):
        head = rows[bi]
        prev = rows[bi - 1]
        h_doc, h_lp, h_v, h_m, h_seg = (
            head["source_doc_id"][0], head["source_logical_position"][0],
            head["input_ids"][0], bool(head["loss_mask"][0]),
            head["segment_ids"][0])
        # 上一块末位：跳过 PAD，取最后一个真实 token
        j = len(prev["input_ids"]) - 1
        while j >= 0 and prev["input_ids"][j] == pad:
            j -= 1
        if j < 0:
            addv(f"块 {bi-1}: 整块都是 PAD，不合法")
            continue
        p_doc, p_lp, p_v = (prev["source_doc_id"][j],
                            prev["source_logical_position"][j],
                            prev["input_ids"][j])
        prev_ended_with_pad = (prev["input_ids"][-1] == pad)

        if h_m:
            addv(f"块 {bi} 位置0: 块首（BOS 或重复上下文）的 loss_mask 必须为 0")
        if h_seg != 0:
            addv(f"块 {bi} 位置0: 每块首个 segment 编号必须为 0，得到 {h_seg}")

        if h_lp == 0:
            # 情形 (b)：新文 BOS 开块——上一块必须已收尾（满块或 PAD 补齐），
            # 且末位真实 token 属于另一篇文档（同文跨块只能走重复上下文）
            if h_doc == p_doc:
                addv(f"块 {bi} 位置0: 块首 BOS 与上一块末位属于同一 doc_id，"
                     f"同文跨块必须用重复上下文而非新 BOS")
            if h_v != bos:
                addv(f"块 {bi} 位置0: 逻辑位置 0 的值应为 BOS={bos}")
        else:
            # 情形 (a)：长文续写——来源必须与上一块末位真实 token 完全相同
            if prev_ended_with_pad:
                addv(f"块 {bi} 位置0: 上一块已 PAD 收尾，块首不能是重复上下文，"
                     f"应开始新文 BOS")
            if (h_doc, h_lp) != (p_doc, p_lp):
                addv(f"块 {bi} 位置0: 块首来源 ({h_doc!r}, lp={h_lp}) 与上一块"
                     f"末位真实 token 来源 ({p_doc!r}, lp={p_lp}) 不一致，"
                     f"跨块上下文必须重复同一逻辑位置")
            if h_v != p_v:
                addv(f"块 {bi} 位置0: 块首 token={h_v} 与上一块末位 token={p_v} 不一致")

    # ---- 边集合比对：每条逻辑边恰好一次 ----
    missing = sorted(expected_edges - observed_edges, key=lambda x: (str(x[0]), x[1]))
    unexpected = sorted(observed_edges - expected_edges, key=lambda x: (str(x[0]), x[1]))
    for key in missing:
        addv(f"缺少监督边: doc={key[0]!r} 目标 lp={key[1]}")
    for key in unexpected:
        addv(f"出现不应存在的监督边: doc={key[0]!r} 目标 lp={key[1]}")

    total_expected = len(expected_edges)
    report = {
        "status": "PASS" if not violations else "FAIL",
        "config": cfg,
        "n_documents": len(docs),
        "n_blocks": len(rows),
        "block_length": L,
        "counts": {
            "expected_supervised_edges": total_expected,
            "observed_supervised_edges": len(observed_edges),
            "supervised_positions": n_supervised,
            "context_repeat_positions": n_context_repeat,
            "pad_positions": n_pad,
        },
        "edge_set_invariant": {
            "edges_match_original_documents": not missing and not unexpected,
            "every_edge_exactly_once": len(seen_targets) == total_expected
                                       and not missing and not unexpected,
            "cross_document_edges": n_cross_doc_edges,
        },
        "violations": violations,
        "blocks": block_reports,
    }
    return report


# --------------------------------------------------------------------------- #
# 训练框架对接（纯函数，不导入任何模型、不训练）
# --------------------------------------------------------------------------- #
def build_shifted_labels(input_ids: list[int], loss_mask: list[bool],
                         ignore_index: int = IGNORE_INDEX) -> dict[str, list[int]]:
    """
    由块字段构造两种常见框架约定的 shifted labels：

    1. HuggingFace 风格：labels 与 input_ids 等长，框架内部做
       `shift_logits`（logits[..., :-1, :] 对 labels[..., 1:]）。
       非监督位置填 ignore_index(-100)，损失只在 loss_mask=1 处累计。

    2. Megatron/显式 shift 风格：预测序列 target = input_ids[1:]，
       target_mask = loss_mask[1:]，由 input_ids[:-1] 的隐状态预测；
       target_mask=0 的位置不计损失。

    跨文档边界两侧 mask 天然为 0（前文 EOS 的后继是新文 BOS，BOS mask=0），
    重复上下文位置 mask=0，因此不会产生跨文边，也不会把上下文重复算成目标。
    """
    if len(input_ids) != len(loss_mask):
        raise ValueError("input_ids 与 loss_mask 长度必须一致")
    hf_labels = [t if m else ignore_index
                 for t, m in zip(input_ids, loss_mask)]
    target = input_ids[1:]
    target_mask = [bool(m) for m in loss_mask[1:]]
    return {
        "hf_labels": hf_labels,
        "explicit_target": target,
        "explicit_target_mask": target_mask,
    }


def allowed_attention(segment_ids: list[int],
                      source_doc_id: list[Any],
                      input_ids: list[int],
                      pad_id: int = DEFAULT_PAD_ID) -> list[tuple[int, int]]:
    """
    生成块内注意力许可关系 (query_position, key_position)：
      * PAD 不参与注意力（既不作 query 也不作 key）；
      * 同块、同 segment（等价于同文档段）；
      * 因果方向：key 位置不晚于 query（key <= query）。
    无需存储 L×L 矩阵，训练时可由 segment_ids 向量化展开。
    """
    pairs: list[tuple[int, int]] = []
    for q in range(len(input_ids)):
        if input_ids[q] == pad_id:
            continue
        for k in range(q + 1):
            if input_ids[k] == pad_id:
                continue
            if segment_ids[q] == segment_ids[k] and source_doc_id[q] == source_doc_id[k]:
                pairs.append((q, k))
    return pairs
