"""命令行：build / query / verify。

  python -m gtfsfact build  --feed fixtures/feed --start 2026-01-01 --end 2026-01-03 --out out.sqlite
  python -m gtfsfact build  --config fixtures/build.json --out out.sqlite
  python -m gtfsfact query  --db out.sqlite --stop s1 \
      --from 2026-01-01T23:00:00+08:00 --to 2026-01-02T02:00:00+08:00
  python -m gtfsfact verify --db out.sqlite
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .errors import BuildError
from .expand import expand
from .gtfs import load_feed
from .store import TZ, query_stop_calls, verify_db, write_db


def _parse_date(raw: str, what: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BuildError(f"{what} 日期 {raw!r} 无效，应为 YYYY-MM-DD") from None


def _parse_dt(raw: str, what: str) -> int:
    """ISO 8601 时刻 -> epoch 秒；无时区时按 Asia/Shanghai 解释。"""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise BuildError(f"{what} 时刻 {raw!r} 无效，应为 ISO 8601（如 2026-01-01T23:00:00+08:00）") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp())


def _cmd_build(args: argparse.Namespace) -> int:
    cfg: dict = {}
    if args.config:
        try:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildError(f"无法读取配置文件 {args.config}: {exc}") from exc
    feed_dir = args.feed or cfg.get("feed")
    start_raw = args.start or cfg.get("service_start")
    end_raw = args.end or cfg.get("service_end")
    out = args.out or cfg.get("out")
    missing = [
        name
        for name, val in (("--feed", feed_dir), ("--start", start_raw), ("--end", end_raw), ("--out", out))
        if not val
    ]
    if missing:
        raise BuildError(f"缺少参数: {', '.join(missing)}（可用 --config 提供 feed/service_start/service_end/out）")
    service_start = _parse_date(start_raw, "服务起点")
    service_end = _parse_date(end_raw, "服务终点")
    if service_start > service_end:
        raise BuildError(f"服务日期区间无效: {service_start} 晚于 {service_end}")

    feed = load_feed(feed_dir)
    instances = expand(feed, service_start, service_end)
    n_inst, n_calls = write_db(out, feed, instances, service_start, service_end)
    print(f"build 完成: {out}")
    print(f"  服务日期区间: [{service_start}, {service_end}]（闭区间）")
    print(f"  trip_instances: {n_inst} 行")
    print(f"  stop_calls:     {n_calls} 行")
    per_trip: dict[str, int] = {}
    for inst in instances:
        per_trip[inst.trip_id] = per_trip.get(inst.trip_id, 0) + 1
    for trip_id in sorted(per_trip):
        print(f"    {trip_id}: {per_trip[trip_id]} 个实例")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    from_epoch = _parse_dt(getattr(args, "from"), "--from")
    to_epoch = _parse_dt(args.to, "--to")
    if from_epoch >= to_epoch:
        raise BuildError("查询区间无效: --from 必须早于 --to（半开区间 [from, to)）")
    rows = query_stop_calls(args.db, args.stop, from_epoch, to_epoch)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    results = verify_db(args.db)
    all_ok = True
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        line = f"[{mark}] {r.name}"
        if r.detail:
            line += f" — {r.detail}"
        print(line)
        all_ok = all_ok and r.ok
    print("verify: 全部通过" if all_ok else "verify: 存在失败项")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gtfsfact",
        description="GTFS 服务日 -> 跨午夜站点时刻事实表（SQLite），不含路线规划",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="读取 feed，展开并写出 SQLite 事实表")
    p_build.add_argument("--feed", help="feed 目录（含 agency.txt 等）")
    p_build.add_argument("--start", help="服务日期起点 YYYY-MM-DD（闭区间）")
    p_build.add_argument("--end", help="服务日期终点 YYYY-MM-DD（闭区间）")
    p_build.add_argument("--out", help="输出 SQLite 文件路径")
    p_build.add_argument("--config", help="JSON 配置（feed/service_start/service_end/out），命令行优先")
    p_build.set_defaults(func=_cmd_build)

    p_query = sub.add_parser("query", help="按实际时刻半开区间与 stop_id 读取 stop_calls")
    p_query.add_argument("--db", required=True, help="build 产出的 SQLite 文件")
    p_query.add_argument("--stop", required=True, help="stop_id")
    p_query.add_argument("--from", required=True, help="区间起点 ISO 8601（含）；无时区按 Asia/Shanghai")
    p_query.add_argument("--to", required=True, help="区间终点 ISO 8601（不含）")
    p_query.set_defaults(func=_cmd_query)

    p_verify = sub.add_parser("verify", help="自检已生成的数据库")
    p_verify.add_argument("--db", required=True, help="build 产出的 SQLite 文件")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: 文件不存在: {exc.filename or exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
