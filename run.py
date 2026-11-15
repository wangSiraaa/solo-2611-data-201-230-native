"""命令行入口：对一个 typed JSON plan 执行受限批量关联。

用法:
    python run.py DB PLAN SCHEMA OUT_RESULTS OUT_STATS

成功：写出结果 JSON 与执行统计 JSON，退出码 0。
标量多行等查询错误：退出码 3，结果文件不会被创建或覆盖（不发布部分结果）。
plan / schema 不合法：退出码 2。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from batchjoin.engine import CardinalityError, execute_plan
from batchjoin.plan import PlanError, load_plan, load_schema

EXIT_PLAN_ERROR = 2
EXIT_QUERY_ERROR = 3


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(
            "用法: python run.py DB PLAN SCHEMA OUT_RESULTS OUT_STATS",
            file=sys.stderr,
        )
        return 64
    db_path, plan_path, schema_path, out_path, stats_path = argv[1:]

    try:
        schema = load_schema(schema_path)
        plan = load_plan(plan_path, schema)
    except PlanError as exc:
        print(f"plan 校验失败: {exc}", file=sys.stderr)
        return EXIT_PLAN_ERROR

    out = Path(out_path)
    if out.exists():
        # 显式保护：失败的旧结果不能被误读为本次产物；成功路径会原子替换。
        out.unlink()

    try:
        stats = execute_plan(db_path, plan, schema, out)
    except CardinalityError as exc:
        print(f"查询错误（未发布任何结果）: {exc}", file=sys.stderr)
        if out.exists():  # 理论上不会发生，双保险。
            out.unlink()
        return EXIT_QUERY_ERROR

    stats_file = Path(stats_path)
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    stats_file.write_text(
        json.dumps(stats.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"OK: {stats.outer_rows} 外行 / {stats.batches} 批 / "
        f"内表批量查询 {stats.counters['inner_batch_queries']} 次 / "
        f"朴素 N+1 需 {stats.counters['naive_n_plus_1_inner_queries']} 次"
    )
    print(f"结果 -> {out}")
    print(f"统计 -> {stats_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
