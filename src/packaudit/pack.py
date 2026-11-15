#!/usr/bin/env python3
"""
pack 子命令：documents.jsonl + config -> 定长 Parquet 块 + 打包清单。

用法：
    python -m packaudit.pack \
        --documents fixtures/documents.jsonl \
        --config fixtures/config.json \
        --out-dir out/L8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    PackError,
    _doc_arrow_type,
    load_config,
    load_documents,
    pack_documents,
    write_parquet,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="定长训练块贪心打包")
    ap.add_argument("--documents", required=True, help="documents.jsonl 路径")
    ap.add_argument("--config", required=True, help="config.json 路径")
    ap.add_argument("--out-dir", required=True, help="输出目录")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        docs = load_documents(args.documents, cfg["max_token_id"])
        blocks = pack_documents(docs, cfg)
    except PackError as e:
        print(f"[pack] 拒绝输入: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"[pack] 文件不存在: {e}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    parquet_path = out_dir / "blocks.parquet"
    schema = write_parquet(blocks, parquet_path, _doc_arrow_type(docs))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_documents": str(Path(args.documents).resolve()),
        "input_config": str(Path(args.config).resolve()),
        "config": cfg,
        "n_documents": len(docs),
        "n_blocks": len(blocks),
        "parquet": parquet_path.name,
        "schema": {f.name: str(f.type) for f in schema},
        "doc_order": [d["doc_id"] for d in docs],
        "document_lengths": {
            str(d["doc_id"]): len(d["tokens"]) for d in docs
        },
        "rules": [
            "逻辑序列 = BOS + tokens + EOS；空文保留 BOS->EOS 监督边",
            "块内仅剩 1 个位置时新文不启动，PAD 补齐后换块（禁止孤立 BOS 入末位）",
            "长文跨块时新块首位重复上一块末位 token 作上下文，mask=0 且不产生新监督边",
            "末块 PAD 补足；严格输入顺序贪心；不做最优装箱或数据集划分",
        ],
    }
    with open(out_dir / "pack_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[pack] {len(docs)} 篇文档 -> {len(blocks)} 个块 (L={cfg['block_length']})")
    print(f"[pack] Parquet : {parquet_path}")
    print(f"[pack] 清单    : {out_dir / 'pack_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
