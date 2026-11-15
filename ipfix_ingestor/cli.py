"""命令行入口：ingest（批量入仓写 JSONL）与 inspect（单报文/捕获检视）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parser import (
    IEConfig,
    MessageError,
    TemplateRegistry,
    parse_capture,
    parse_message,
)

DEFAULT_IE = "fixtures/information-elements.json"
DEFAULT_CAPTURE = "fixtures/capture.json"
DEFAULT_OUTDIR = "output"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def _cmd_ingest(args: argparse.Namespace) -> int:
    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = parse_capture(args.capture, args.ie)

    paths = {
        "flowRecords": out_dir / "flow_records.jsonl",
        "templateHistory": out_dir / "template_history.jsonl",
        "unexplainedSets": out_dir / "unexplained_sets.jsonl",
        "messageErrors": out_dir / "message_errors.jsonl",
    }
    for key, path in paths.items():
        _write_jsonl(path, result[key])
    _write_jsonl(out_dir / "messages.jsonl", result["messages"])

    print(f"capture         : {args.capture}")
    for key, path in paths.items():
        print(f"{key:16s}: {len(result[key]):3d} 行 -> {path}")
    print(f"messages        : {len(result['messages']):3d} 行 -> {out_dir / 'messages.jsonl'}")
    return 1 if result["messageErrors"] else 0


def _print_message(parsed: dict) -> None:
    print(
        f"[{parsed['messageIndex']}] {parsed['source']} exporter={parsed['exporter']} "
        f"domain={parsed['domain']} len={parsed['messageLength']} seq={parsed['sequenceNumber']}"
    )
    for ev in parsed["templateEvents"]:
        kind = "撤回" if ev["event"] == "withdrawn" else f"定义 v{ev['version']}"
        names = ", ".join(
            ("ent:" if f["enterprise"] else "") + f["name"] for f in ev["fields"]
        )
        print(f"    模板 {ev['templateId']:<4d} {kind} 字段[{names}] setOffset={ev['setOffset']}")
    for rec in parsed["flowRecords"]:
        flat = ", ".join(f"{k}={v['value']!r}" for k, v in rec["values"].items())
        print(
            f"    记录 模板{rec['templateId']}v{rec['templateVersion']} "
            f"setOffset={rec['setOffset']} recOffset={rec['recordOffset']}  {flat}"
        )
    for bad in parsed["unexplainedSets"]:
        print(
            f"    无法解释 Set id={bad['setId']} setOffset={bad['setOffset']} "
            f"原因={bad['reason']} 原始字节={bad['rawHex'][:48]}{'...' if len(bad['rawHex']) > 48 else ''}"
        )


def _cmd_inspect(args: argparse.Namespace) -> int:
    ie_config = IEConfig.load(args.ie)
    exit_code = 0
    if args.capture:
        registry = TemplateRegistry()
        entries = json.loads(Path(args.capture).read_text(encoding="utf-8"))
        base = Path(args.capture).parent
        for index, entry in enumerate(entries):
            data = (base / entry["file"]).read_bytes()
            try:
                parsed = parse_message(
                    data, entry["exporter"], entry["file"], index, registry, ie_config
                )
            except MessageError as exc:
                print(f"[{index}] {entry['file']} exporter={entry['exporter']} 报文失败: {exc}")
                exit_code = 1
                continue
            _print_message(parsed)
    else:
        if not args.file:
            print("inspect 需要 --file 或 --capture", file=sys.stderr)
            return 2
        registry = TemplateRegistry()
        data = Path(args.file).read_bytes()
        try:
            parsed = parse_message(
                data, args.exporter, args.file, 0, registry, ie_config
            )
        except MessageError as exc:
            print(f"{args.file} 报文失败: {exc}")
            return 1
        _print_message(parsed)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipfix_ingestor",
        description="离线 IPFIX v10 入仓器（模板生命周期驱动，不接真实网络）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="按 capture.json 顺序入仓，输出 JSONL")
    p_ingest.add_argument("--capture", default=DEFAULT_CAPTURE)
    p_ingest.add_argument("--ie", default=DEFAULT_IE)
    p_ingest.add_argument("--outdir", default=DEFAULT_OUTDIR)
    p_ingest.set_defaults(func=_cmd_ingest)

    p_inspect = sub.add_parser("inspect", help="检视单个报文或整条捕获")
    p_inspect.add_argument("--file", help="单个 IPFIX 二进制报文")
    p_inspect.add_argument("--capture", help="按 capture.json 顺序检视")
    p_inspect.add_argument("--exporter", default="manual")
    p_inspect.add_argument("--ie", default=DEFAULT_IE)
    p_inspect.set_defaults(func=_cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
