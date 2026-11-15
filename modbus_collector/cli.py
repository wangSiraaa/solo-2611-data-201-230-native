"""Modbus TCP register batch collector — plan, collect, inspect."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .collect import Collection, collect
from .errors import CollectorError, ConfigError, PlanError
from .planner import Plan, build_plan
from .points import load_point_table

GROUPED_FIELDS = (
    "id",
    "address",
    "type",
    "quality",
    "valid",
    "raw_registers",
    "raw_bytes_hex",
    "value",
    "decoded",
    "source_request",
    "error",
)


def _emit(payload: Any, output: str | None) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _collect_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "host": args.host,
        "port": args.port,
        "timeout": args.timeout,
        "retries": args.retries,
        "retry_delay": args.retry_delay,
        "first_transaction_id": args.first_transaction_id,
    }


def plan_command(args: argparse.Namespace) -> int:
    table = load_point_table(args.points)
    plan = build_plan(table)
    _emit(plan.as_dict(), args.output)
    return 0


def collect_command(args: argparse.Namespace) -> int:
    table = load_point_table(args.points)
    plan = build_plan(table)
    collection = collect(plan, **_collect_options(args))
    payload = collection.as_dict()
    if args.brief:
        payload = _brief(collection)
    _emit(payload, args.output)
    return 0 if collection.complete_snapshot else 2


def _brief(collection: Collection) -> dict[str, Any]:
    full = collection.as_dict()
    return {
        "quality": full["quality"],
        "complete_snapshot": full["complete_snapshot"],
        "version_fence": full["version_fence"],
        "selected_round": full["selected_round"],
        "rounds_executed": len(full["rounds"]),
        "requests": [
            {
                "sequence": item["sequence"],
                "start": item["start"],
                "count": item["count"],
                "ok": item["exchange"]["ok"] if item["exchange"] else False,
                "exception_code": (
                    item["exchange"]["exception_code"] if item["exchange"] else None
                ),
            }
            for item in full["requests"]
        ],
        "points": [
            {key: record[key] for key in GROUPED_FIELDS}
            for record in full["points"]
        ],
        "response_mismatches": full["response_mismatches"],
    }


def inspect_command(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "points" not in data:
        raise CollectorError(
            f"{args.input} does not look like a collect result (no 'points')"
        )
    quality = args.quality
    known_qualities = {record.get("quality") for record in data["points"]}
    if quality and quality not in known_qualities:
        raise CollectorError(
            f"no point has quality {quality!r}; observed: {sorted(known_qualities)}"
        )

    rows: list[dict[str, Any]] = []
    for record in data["points"]:
        if quality and record.get("quality") != quality:
            continue
        if args.point_id and record.get("id") != args.point_id:
            continue
        if args.only_invalid and record.get("valid"):
            continue
        rows.append(
            {
                key: record.get(key)
                for key in ("id", "address", "type", "quality", "valid")
            }
            | {
                "raw_registers": record.get("raw_registers"),
                "raw_bytes_hex": record.get("raw_bytes_hex"),
                "value": record.get("value"),
                "source_request": record.get("source_request"),
                "error": record.get("error"),
            }
        )

    payload: dict[str, Any] = {
        "source": args.input,
        "collect_quality": data.get("quality"),
        "complete_snapshot": data.get("complete_snapshot"),
        "version_fence": data.get("version_fence"),
        "selected_round": data.get("selected_round"),
        "point_count": len(rows),
        "points": rows,
    }
    if args.json:
        _emit(payload, None)
        return 0
    for row in rows:
        status = row["quality"] or "unknown"
        value = row["value"]
        raw = row["raw_registers"]
        print(
            f"{str(row['id']):<14} {str(row['address']):>4} "
            f"{str(row['type']):<7} {status:<24} raw={raw} value={value!r}"
            f" bytes={row['raw_bytes_hex']}"
        )
    print(
        f"\nround quality={payload['collect_quality']} "
        f"complete_snapshot={payload['complete_snapshot']} "
        f"version_fence={payload['version_fence']}"
    )
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--points", default="fixtures/points.json")
    parser.add_argument("--output", default=None, help="write JSON here")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modbus-collector",
        description=(
            "Read-only Modbus TCP function 03 collector: merge overlapping "
            "point ranges into a bounded plan, fence one round with the "
            "version register, and export quality-tagged observations."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the request plan without I/O")
    _add_common(plan)
    plan.set_defaults(func=plan_command)

    collect_parser = sub.add_parser("collect", help="run one fenced collection")
    _add_common(collect_parser)
    collect_parser.add_argument("--host", default="127.0.0.1")
    collect_parser.add_argument("--port", type=int, default=11502)
    collect_parser.add_argument("--timeout", type=float, default=3.0)
    collect_parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="whole-round re-reads allowed after an odd/changed version",
    )
    collect_parser.add_argument("--retry-delay", type=float, default=0.0)
    collect_parser.add_argument("--first-transaction-id", type=int, default=1)
    collect_parser.add_argument(
        "--brief", action="store_true", help="omit exchange-level detail"
    )
    collect_parser.set_defaults(func=collect_command)

    inspect_parser = sub.add_parser(
        "inspect", help="summarize a collect result, point by point"
    )
    inspect_parser.add_argument("--input", required=True)
    inspect_parser.add_argument("--quality", default=None)
    inspect_parser.add_argument("--point-id", default=None)
    inspect_parser.add_argument(
        "--only-invalid", action="store_true", help="hide valid points"
    )
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(func=inspect_command)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return args.func(args)
    except (ConfigError, PlanError, CollectorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
