"""命令行入口：

python -m otlp_normalize normalize <file.json> [<file2.json> ...] [--out-dir DIR]
python -m otlp_normalize inspect   <file.json> [<file2.json> ...]
"""
from __future__ import annotations

import argparse
import sys

from .normalizer import normalize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m otlp_normalize",
        description="Offline OTLP metric temporality normalizer (monotonic Sum + explicit-bucket Histogram only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_norm = sub.add_parser("normalize", help="emit fact table / origin mapping / unconfirmed records as JSON")
    p_norm.add_argument("files", nargs="+")
    p_norm.add_argument("--out-dir", default=None,
                        help="if set, write facts.jsonl / origins.jsonl / unconfirmed.jsonl into this directory")

    p_insp = sub.add_parser("inspect", help="print a human-readable diagnostic report")
    p_insp.add_argument("files", nargs="+")

    args = parser.parse_args(argv)
    result = normalize(args.files)

    if args.command == "inspect":
        print(result.inspect_text())
        return 0

    if args.out_dir:
        paths = result.write_jsonl(args.out_dir)
        for path in paths:
            print(f"wrote {path}")
    else:
        print(result.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
