"""Command line interface:

    python -m zarr_patch plan   fixtures/source.zarr fixtures/patches.json plan.json
    python -m zarr_patch apply  fixtures/source.zarr fixtures/patches.json out/result.zarr report.json
    python -m zarr_patch run    fixtures/source.zarr fixtures/patches.json out/result.zarr \
        --plan-out plan.json --report-out report.json

Validation happens before any output is created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from zarr_patch.apply import apply_plan
from zarr_patch.errors import PatchValidationError
from zarr_patch.planning import compile_plan


def _load_patches(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str | None, payload) -> None:
    if path is None:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zarr_patch",
        description="Chunk-consolidating offline patches for 2-D Zarr v2 arrays.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="validate and compile patches into a JSON plan")
    p_plan.add_argument("source")
    p_plan.add_argument("patches")
    p_plan.add_argument("plan_out")

    p_apply = sub.add_parser("apply", help="plan and apply patches into a new Zarr directory")
    p_apply.add_argument("source")
    p_apply.add_argument("patches")
    p_apply.add_argument("output")
    p_apply.add_argument("--plan-out", default=None)
    p_apply.add_argument("--report-out", default=None)

    p_run = sub.add_parser("run", help="alias of apply (plan then apply)")
    p_run.add_argument("source")
    p_run.add_argument("patches")
    p_run.add_argument("output")
    p_run.add_argument("--plan-out", default=None)
    p_run.add_argument("--report-out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        patches = _load_patches(args.patches)
        plan = compile_plan(args.source, patches)

        if args.command == "plan":
            _write_json(args.plan_out, plan.to_dict())
            print(
                f"plan ok: {plan.operation_count} ops, "
                f"{len(plan.chunk_plans)} touched chunks, "
                f"{plan.total_update_count} local cell updates "
                f"(noop ops: {plan.noop_operations})"
            )
            return 0

        _write_json(args.plan_out, plan.to_dict())
        report = apply_plan(plan, args.output)
        _write_json(args.report_out, report.to_dict())
        print(
            f"apply ok: {len(report.touched)} chunks decoded+encoded "
            f"({report.total_decodes} decodes / {report.total_encodes} encodes), "
            f"{report.byte_copy_count} chunks byte-copied, "
            f"{len(report.missing_untouched_absent)} missing chunks left absent"
        )
        return 0
    except PatchValidationError as exc:
        print(f"patch batch rejected: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
