"""Command-line entry point: parse one or more 856 files and export tables.

    python3 -m x12_856 parse fixtures/shipment.edi --out out/shipment
    python3 -m x12_856 parse fixtures/*.edi --out out/all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .loader import SUPPORT_BOUNDARY, export, parse, parse_file


def _parse_one(path: Path, out_dir: Path) -> dict:
    result = parse_file(path)
    paths = export(result, out_dir)
    return {
        "source": str(path),
        "ok": result.ok,
        "out_dir": str(out_dir),
        "tables": {name: str(p) for name, p in sorted(paths.items())},
        "transactions": [
            {
                "scope_id": status.scope_id,
                "status": status.status,
                "reason": status.reason,
            }
            for status in result.scope_statuses
            if status.scope == "transaction"
        ],
        "counts": {
            "shipments": len(result.shipments),
            "orders": len(result.orders),
            "packages": len(result.packages),
            "items": len(result.items),
            "hl_groups": len(result.hl_facts),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m x12_856",
        description="Restricted X12 856 ship-notice loader (local, offline).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="parse EDI file(s) and export tables")
    parse_cmd.add_argument("files", nargs="+", type=Path)
    parse_cmd.add_argument("--out", type=Path, required=True)
    parse_cmd.add_argument(
        "--stdin", action="store_true", help="(unused) reserved for future use"
    )

    sub.add_parser("boundary", help="print the supported subset and exclusions")

    args = parser.parse_args(argv)
    if args.command == "boundary":
        print(SUPPORT_BOUNDARY, end="")
        return 0

    reports = []
    exit_code = 0
    multiple = len(args.files) > 1
    for path in args.files:
        out_dir = args.out / path.stem if multiple else args.out
        report = _parse_one(path, out_dir)
        reports.append(report)
        if not report["ok"]:
            exit_code = 2
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
