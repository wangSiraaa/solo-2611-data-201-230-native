"""Command-line interface: ingest-events, status, export."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import Pipeline
from .errors import AssemblyError, ConfigError
from .events import parse_date, read_jsonl
from .store import atomic_write_bytes


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None
    if not isinstance(config, dict):
        raise ConfigError("config must be a JSON object")
    dates = config.get("target_dates")
    if not isinstance(dates, list) or not dates:
        raise ConfigError("config.target_dates must be a non-empty list")
    if len(set(dates)) != len(dates):
        raise ConfigError("config.target_dates contains duplicates")
    config["target_dates"] = [parse_date(day, "target date") for day in dates]
    root = config.get("input_root", ".")
    if not isinstance(root, str) or not root:
        raise ConfigError("config.input_root must be a non-empty string")
    config["input_root"] = root
    return config


def _pipeline(args: argparse.Namespace) -> Pipeline:
    config = load_config(Path(args.config))
    return Pipeline(config, Path(args.out))


def cmd_ingest_events(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    out = Path(args.out)
    pipeline = Pipeline(config, out)
    try:
        text = Path(args.events).read_text("utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read events {args.events}: {exc}") from None
    result = pipeline.ingest(read_jsonl(text))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    print(json.dumps(pipeline.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    target = parse_date(args.date, "export date")
    if target not in pipeline.target_dates:
        raise ConfigError(f"{target} is not a configured target date")
    binding_path = pipeline.out_dir / "assemblies" / target / "current.json"
    if not binding_path.exists():
        raise ConfigError(f"no successful output version exists for {target}")
    binding = json.loads(binding_path.read_text("utf-8"))
    source_bytes = (pipeline.out_dir / binding["output"]["path"]).read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != binding["output"]["sha256"]:
        raise ConfigError(f"current output for {target} does not match its binding")
    destination = Path(args.to)
    if args.to == "-":
        sys.stdout.buffer.write(source_bytes)
        return 0
    if destination.exists() and destination.is_dir():
        destination = destination / f"{target}.csv"
    atomic_write_bytes(destination, source_bytes)
    print(
        json.dumps(
            {
                "date": target,
                "written": str(destination),
                "sha256": binding["output"]["sha256"],
                "generation": binding["generation"],
                "input_vector": binding["input_vector"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partition-etl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", required=True, help="path to config.json")
        sp.add_argument("--out", required=True, help="output/state directory (fresh per run set)")

    ingest = sub.add_parser("ingest-events", help="process an ordered JSONL completion stream")
    add_common(ingest)
    ingest.add_argument("--events", required=True, help="JSONL completion-event file")
    ingest.set_defaults(func=cmd_ingest_events)

    status = sub.add_parser("status", help="show dependencies, vectors, failures and run history")
    add_common(status)
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export", help="write the current CNY sales detail for a target date")
    add_common(export)
    export.add_argument("--date", required=True)
    export.add_argument("--to", required=True, help="file, directory, or - for stdout")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AssemblyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
