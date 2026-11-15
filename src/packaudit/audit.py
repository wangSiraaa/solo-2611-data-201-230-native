#!/usr/bin/env python3
"""
audit 子命令：读取实际 Parquet 产物与原文档，独立重建监督边集合并核对。

用法：
    python -m packaudit.audit \
        --blocks out/L8/blocks.parquet \
        --documents fixtures/documents.jsonl \
        --config fixtures/config.json \
        --report out/L8/audit_report.json
退出码：PASS=0，FAIL=1，输入错误=2。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    PackError,
    audit_blocks,
    load_config,
    load_documents,
    read_parquet,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练块可回查审计")
    ap.add_argument("--blocks", required=True, help="pack 产出的 blocks.parquet")
    ap.add_argument("--documents", required=True, help="原始 documents.jsonl")
    ap.add_argument("--config", required=True, help="config.json")
    ap.add_argument("--report", required=True, help="审计报告输出路径 (JSON)")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        docs = load_documents(args.documents, cfg["max_token_id"])
        rows = read_parquet(args.blocks)
    except PackError as e:
        print(f"[audit] 输入不合法: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"[audit] 文件不存在: {e}", file=sys.stderr)
        return 2

    report = audit_blocks(rows, docs, cfg)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    c = report["counts"]
    print(f"[audit] 状态: {report['status']}")
    print(f"[audit] 块数={report['n_blocks']} "
          f"期望监督边={c['expected_supervised_edges']} "
          f"实际监督边={c['observed_supervised_edges']} "
          f"上下文重复位={c['context_repeat_positions']} "
          f"PAD 位={c['pad_positions']}")
    if report["violations"]:
        print(f"[audit] 违规 {len(report['violations'])} 条，详见 {report_path}",
              file=sys.stderr)
        for v in report["violations"][:20]:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print(f"[audit] 报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
