#!/usr/bin/env python3
"""
端到端演示与回归检查（不训练、不调模型、不碰 tokenizer）。

覆盖：
  1. 空文 / 短文 / 恰好成块 / 长文多次跨块 / 重复 token 的逐块视图；
  2. 改变 L 后原始监督边集合不变；
  3. 非法保留 token、bool、重复 doc_id、L<3 一律拒绝；
  4. audit 对损坏产物（跨文边、重复目标、来源污染等）判 FAIL；
  5. shifted labels 与块内注意力许可的构造演示。

运行：PYTHONPATH=src .venv/bin/python tests/demo.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from packaudit.core import (  # noqa: E402
    PackError,
    allowed_attention,
    audit_blocks,
    build_shifted_labels,
    load_config,
    load_documents,
    pack_documents,
    read_parquet,
    write_parquet,
    _doc_arrow_type,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def hr(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def show_blocks(blocks) -> None:
    for b in blocks:
        print(f"块 {b.block_id} (L={len(b.input_ids)})")
        print("  token :", b.input_ids)
        print("  mask  :", [int(m) for m in b.loss_mask], "  (1=该位置是被预测目标)")
        print("  seg   :", b.segment_ids)
        print("  doc   :", [d if d is not None else "NULL" for d in b.source_doc_id])
        print("  lp    :", [lp if lp is not None else "NULL"
                            for lp in b.source_logical_position])


def expected_edge_set(docs, cfg):
    edges = set()
    for d in docs:
        for target_lp in range(1, len(d["tokens"]) + 2):  # lp=1..n+1
            edges.add((d["doc_id"], target_lp))
    return edges


def edges_from_rows(rows):
    edges = set()
    for r in rows:
        for i, m in enumerate(r["loss_mask"]):
            if m:
                edges.add((r["source_doc_id"][i], r["source_logical_position"][i]))
    return edges


def main() -> int:
    base_cfg = load_config(FIX / "config.json")
    docs = load_documents(FIX / "documents.jsonl", base_cfg["max_token_id"])
    expected = expected_edge_set(docs, base_cfg)
    print(f"文档 {len(docs)} 篇；独立枚举原始监督边 {len(expected)} 条")

    # 1) 主用例 L=8 逐块视图 ------------------------------------------------
    hr("1. L=8 逐块视图（空文/短文/恰好成块/长文跨块/重复 token）")
    blocks = pack_documents(docs, base_cfg)
    show_blocks(blocks)
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "b.parquet"
        write_parquet(blocks, pq, _doc_arrow_type(docs))
        rows = read_parquet(pq)
        rep = audit_blocks(rows, docs, base_cfg)
        good = rows
    print(f"\naudit = {rep['status']}；计数 = {rep['counts']}")
    assert rep["status"] == "PASS"

    # 2) 变 L 不变性 --------------------------------------------------------
    hr("2. 改变 L：块布局变化，但原始监督边集合恒定")
    ref = None
    for L in (3, 4, 5, 7, 8, 16):
        cfg = dict(base_cfg, block_length=L)
        bl = pack_documents(docs, cfg)
        with tempfile.TemporaryDirectory() as td:
            pq = Path(td) / "b.parquet"
            write_parquet(bl, pq, _doc_arrow_type(docs))
            rows_L = read_parquet(pq)
            r = audit_blocks(rows_L, docs, cfg)
            edges = edges_from_rows(rows_L)
        ref = edges if ref is None else ref
        assert r["status"] == "PASS" and edges == expected == ref
        print(f"  L={L:2d}: 块数={len(bl):2d}  边数={len(edges)}  "
              f"重复上下文位={r['counts']['context_repeat_positions']:2d}  "
              f"PASS={r['status'] == 'PASS'}")
    print("  -> 所有 L 下边集合完全相同，且等于原始文档独立枚举结果")

    # 3) 非法输入拒绝 -------------------------------------------------------
    hr("3. 非法输入必须被拒绝（exit 语义：抛 PackError）")
    def expect_reject(name, fn):
        try:
            fn()
        except PackError as e:
            print(f"  [拒绝] {name}: {e}")
            return
        raise AssertionError(f"{name} 应当被拒绝")

    expect_reject("保留 token PAD=0",
                  lambda: load_documents(FIX / "invalid.jsonl",
                                         base_cfg["max_token_id"]))
    import json, os
    with tempfile.TemporaryDirectory() as td:
        dup = Path(td) / "dup.jsonl"
        dup.write_text('{"doc_id":"x","tokens":[5]}\n'
                       '{"doc_id":"x","tokens":[6]}\n')
        expect_reject("重复 doc_id", lambda: load_documents(dup, 1000000))
        boolf = Path(td) / "b.jsonl"
        boolf.write_text('{"doc_id":"b","tokens":[true]}\n')
        expect_reject("bool token", lambda: load_documents(boolf, 1000000))
        over = Path(td) / "o.jsonl"
        over.write_text('{"doc_id":"o","tokens":[1000001]}\n')
        expect_reject("超 max_token_id", lambda: load_documents(over, 1000000))
    expect_reject("L<3", lambda: load_config_from({"block_length": 2}))
    expect_reject("BOS=EOS",
                  lambda: load_config_from(
                      {"block_length": 8, "bos_id": 1, "eos_id": 1,
                       "pad_id": 0, "max_token_id": 1000000}))

    # 4) audit 抓损坏 -------------------------------------------------------
    hr("4. audit 必须识别损坏产物（全部期望 FAIL）")
    def corrupt(name, mut):
        r = copy.deepcopy(good)
        mut(r)
        status = audit_blocks(r, docs, base_cfg)["status"]
        print(f"  [{status}] {name}")
        assert status == "FAIL"
    corrupt("块首 mask 置 1", lambda r: r[2]["loss_mask"].__setitem__(0, True))
    corrupt("重复上下文 mask 置 1",
            lambda r: r[3]["loss_mask"].__setitem__(0, True))
    corrupt("伪造跨文边（BOS 置目标）",
            lambda r: r[0]["loss_mask"].__setitem__(2, True))
    corrupt("来源 doc_id 篡改",
            lambda r: r[0]["source_doc_id"].__setitem__(3, "empty"))
    corrupt("token 与来源不符",
            lambda r: r[0]["input_ids"].__setitem__(3, 999))
    corrupt("PAD 来源非 NULL",
            lambda r: (r[5]["source_doc_id"].__setitem__(6, "x"),
                       r[5]["source_logical_position"].__setitem__(6, 0)))
    corrupt("删除监督目标",
            lambda r: r[0]["loss_mask"].__setitem__(1, False))

    # 5) shifted labels 与注意力 -------------------------------------------
    hr("5. shifted labels 与块内注意力许可构造")
    b = blocks[0]
    sl = build_shifted_labels(b.input_ids, b.loss_mask)
    print("  input_ids      :", b.input_ids)
    print("  loss_mask      :", [int(m) for m in b.loss_mask])
    print("  HF labels      :", sl["hf_labels"], "(-100 忽略)")
    print("  explicit target:", sl["explicit_target"])
    print("  target mask    :", [int(m) for m in sl["explicit_target_mask"]])
    pairs = allowed_attention(b.segment_ids, b.source_doc_id, b.input_ids)
    print("  注意力许可(q,k) :", pairs)
    assert all(b.source_doc_id[q] == b.source_doc_id[k] for q, k in pairs)
    assert all(k <= q for q, k in pairs)

    hr("全部演示与回归检查通过 ✔")
    return 0


def load_config_from(d):
    import tempfile, json
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(d, f)
        path = f.name
    try:
        return load_config(path)
    finally:
        import os
        os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(main())
