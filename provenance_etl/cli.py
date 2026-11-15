"""Command line interface: ``run`` / ``trace`` / ``verify``.

Examples
--------
    python -m provenance_etl run    --plan fixtures/plan.json --out out
    python -m provenance_etl trace  --plan fixtures/plan.json
    python -m provenance_etl verify --plan fixtures/plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import run, trace, verify
from .engine import EtlError, PlanError, SourceError


def _print_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _format_group(group: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    keys = ", ".join(f"{k}={v!r}" for k, v in group["group"].items()) or "(global)"
    lines.append(f"group {keys}")
    for name, info in group["aggregates"].items():
        field = f" on {info['field']}" if info["field"] else ""
        lines.append(
            f"  aggregate {name!r} ({info['fn']}{field}) = {info['value']!r}"
            f"  contributions={info['contribution_count']}"
            f"  distinct_values={info['distinct_value_count']}"
        )
        values = ", ".join(
            f"{v['value']!r} x{v['occurrences']}" for v in info["contributing_values"]
        )
        if values:
            lines.append(f"    contributing values: {values}")
        for rec in info["source_records"]:
            lines.append(
                f"    source {rec['table']}/{rec['row_id']}: "
                f"{rec['occurrences']} occurrence(s)"
            )
            for path in rec["paths"]:
                ops = " -> ".join(
                    f"{step['op']}({step['detail']})" for step in path["path"]
                ) or "source"
                lines.append(f"      path [{ops}] x{path['occurrences']}")
    return lines


def _cmd_run(args: argparse.Namespace) -> int:
    report = run(args.plan, args.out)
    if args.json:
        _print_json(report)
    else:
        print(
            f"output {report['output']!r} ({report['op']}): "
            f"{report['row_count']} row(s) -> {args.out}/results.json"
        )
        for row in report["rows"]:
            print(f"  {row['values']}")
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    report = trace(args.plan)
    if args.json:
        _print_json(report)
        return 0
    print(
        f"trace for output {report['output']!r} "
        f"(keys={report['keys']}, aggregates={report['aggregates']})"
    )
    for group in report["groups"]:
        for line in _format_group(group):
            print(line)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify(args.plan)
    if args.json:
        _print_json(report)
    else:
        status = "OK" if report["ok"] else "FAILED"
        print(f"verify {status} for output {report['output']!r}")
        for group in report["groups"]:
            keys = ", ".join(f"{k}={v!r}" for k, v in group["group"].items()) or "(global)"
            for name, info in group["aggregates"].items():
                mark = "ok" if info["ok"] else "MISMATCH"
                print(
                    f"  group {keys} aggregate {name!r}: "
                    f"actual={info['actual']!r} replayed={info['replayed']!r} [{mark}]"
                )
        print(f"  sqlite rows: {report['sqlite']}")
        for error in report["errors"]:
            print(f"  error: {error}")
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provenance_etl",
        description="Restricted relational ETL executor with row provenance.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="evaluate the plan and write results")
    p_run.add_argument("--plan", required=True, help="path to plan.json")
    p_run.add_argument("--out", required=True, help="output directory")
    p_run.add_argument("--json", action="store_true", help="print the full report")
    p_run.set_defaults(func=_cmd_run)

    p_trace = sub.add_parser("trace", help="list per-group aggregate provenance")
    p_trace.add_argument("--plan", required=True, help="path to plan.json")
    p_trace.add_argument("--json", action="store_true", help="print raw JSON")
    p_trace.set_defaults(func=_cmd_trace)

    p_verify = sub.add_parser("verify", help="replay contributions and compare to SQLite")
    p_verify.add_argument("--plan", required=True, help="path to plan.json")
    p_verify.add_argument("--json", action="store_true", help="print raw JSON")
    p_verify.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (EtlError, PlanError, SourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
