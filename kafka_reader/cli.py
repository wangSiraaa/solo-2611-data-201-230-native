"""命令行入口：inspect（排障诊断）与 export（导出可见消息）。"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from .analyze import (
    REASON_TEXT,
    AnalysisResult,
    analyze,
    load_metadata,
)
from .batch import FragmentResult, parse_fragment
from .errors import BatchCorrupt

EXIT_OK = 0
EXIT_CORRUPT = 2
EXIT_USAGE = 3


def _read_fragment(path: Path) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"日志片段不存在: {path}")
    return path.read_bytes()


def _run(log_path: Path, metadata_path: Path) -> tuple[FragmentResult, AnalysisResult | None, object]:
    data = _read_fragment(log_path)
    fragment = parse_fragment(data)
    metadata = load_metadata(metadata_path)
    analysis = analyze(fragment.batches, metadata) if fragment.batches else None
    return fragment, analysis, metadata


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def _preview(value: bytes | None, limit: int = 48) -> str:
    if value is None:
        return "<null>"
    if not value:
        return "<empty>"
    try:
        text = value.decode("utf-8")
        printable = text.replace("\n", "\\n")
    except UnicodeDecodeError:
        printable = "0x" + value.hex()
    if len(printable) > limit:
        return printable[:limit] + "…"
    return printable


def cmd_inspect(args: argparse.Namespace) -> int:
    fragment, analysis, metadata = _run(Path(args.log), Path(args.metadata))

    if args.json:
        payload = {
            "metadata": {
                "topic": metadata.topic,
                "partition": metadata.partition,
                "high_watermark": metadata.high_watermark,
                "last_stable_offset": metadata.last_stable_offset,
            },
            "parse": {
                "ok": fragment.ok,
                "bytes_consumed": fragment.bytes_consumed,
                "total_bytes": fragment.total_bytes,
                "batches": [batch.to_header_dict() for batch in fragment.batches],
                "error": fragment.error.to_dict() if fragment.error else None,
            },
        }
        if analysis is not None:
            payload["entries"] = [_entry_json(entry) for entry in analysis.entries]
            payload["transactions"] = [
                {
                    "producer_id": txn.producer_id,
                    "producer_epoch": txn.producer_epoch,
                    "state": txn.state,
                    "start_offset": txn.start_offset,
                    "end_offset": txn.end_offset,
                    "marker_offset": txn.marker_offset,
                }
                for txn in analysis.transactions
            ]
            payload["reason_counts"] = analysis.reason_counts()
            payload["visible_count"] = len(analysis.visible_entries)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _render_inspect_text(fragment, analysis, metadata)

    # inspect 的职责是排障诊断：损坏时保留已确认前缀并以非零码退出，
    # 但不会把片段伪装成完整成功数据。
    return EXIT_OK if fragment.ok else EXIT_CORRUPT


def _entry_json(entry) -> dict:
    return {
        "offset": entry.offset,
        "timestamp": entry.timestamp,
        "key": _json_bytes(entry.key),
        "value": _json_bytes(entry.value),
        "headers": [
            {"key": _json_bytes(h.key), "value": _json_bytes(h.value)}
            for h in entry.headers
        ],
        "producer_id": entry.producer_id,
        "producer_epoch": entry.producer_epoch,
        "sequence": entry.sequence,
        "is_control": entry.is_control,
        "control_type": entry.control_type,
        "reason": entry.reason,
        "reason_text": REASON_TEXT.get(entry.reason, entry.reason),
        "beyond_lso": entry.beyond_lso,
        "visible": entry.visible,
        "batch_index": entry.batch_index,
    }


def _json_bytes(value: bytes | None):
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(value).decode("ascii")}


def _render_inspect_text(
    fragment: FragmentResult,
    analysis: AnalysisResult | None,
    metadata,
) -> None:
    print("== 分区边界元数据 ==")
    print(f"topic/partition : {metadata.topic}/{metadata.partition}")
    print(f"high watermark  : {metadata.high_watermark} (exclusive)")
    print(f"last stable off : {metadata.last_stable_offset} (exclusive)")
    print()
    print("== RecordBatch 解析 ==")
    for batch in fragment.batches:
        flags = []
        if batch.is_control:
            flags.append("CONTROL")
        if batch.is_transactional:
            flags.append("TXN")
        flags.append(f"comp={'gzip' if batch.compression == 1 else 'none'}")
        print(
            f"batch#{batch.index} @file {batch.file_offset} "
            f"offset {batch.base_offset}..{batch.last_offset} "
            f"pid={batch.producer_id} epoch={batch.producer_epoch} "
            f"seq={batch.base_sequence} records={batch.record_count} "
            f"crc={batch.crc:08x} layout={batch.layout} [{' '.join(flags)}]"
        )
        if batch.control_marker is not None:
            marker = batch.control_marker
            print(
                f"    控制标记: {'COMMIT' if marker.is_commit else 'ABORT'} "
                f"coordinatorEpoch={marker.coordinator_epoch}"
            )
        if batch.offset_delta_warning is not None:
            print(f"    注意: {batch.offset_delta_warning}")

    if fragment.error:
        err = fragment.error
        print()
        print("== 致命损坏（已停止，保留此前已确认批） ==")
        print(
            f"类型: {err.kind}；批序号: {err.batch_index}；"
            f"文件偏移: {err.file_offset}"
        )
        print(f"原因: {err.message}")
        print(
            f"已消费 {fragment.bytes_consumed}/{fragment.total_bytes} 字节"
        )
        return

    print()
    print("== 记录分类（read_committed） ==")
    if analysis is None:
        print("(空片段)")
        return

    for entry in analysis.entries:
        marker = ""
        if entry.is_control:
            marker = f" <{entry.control_type}>"
        lso_flag = " [+LSO尾部]" if entry.beyond_lso and not entry.visible else ""
        print(
            f"offset={entry.offset:<2} ts={entry.timestamp:<6} "
            f"key={_preview(entry.key):<12} value={_preview(entry.value):<16} "
            f"headers={len(entry.headers)} "
            f"visible={'Y' if entry.visible else 'N'} "
            f"[{entry.reason}]{marker}{lso_flag}"
        )

    print()
    print("== 事务汇总 ==")
    for txn in analysis.transactions:
        print(
            f"pid={txn.producer_id} epoch={txn.producer_epoch} "
            f"state={txn.state.upper()} offsets {txn.start_offset}.."
            f"{txn.end_offset} marker={txn.marker_offset}"
        )
    if analysis.dangling_markers:
        for marker in analysis.dangling_markers:
            print(
                f"悬空控制标记 offset={marker.offset} pid={marker.producer_id} "
                f"epoch={marker.producer_epoch}（无同 epoch 在途事务，已忽略）"
            )

    print()
    print("== 原因计数 ==")
    for reason, count in sorted(analysis.reason_counts().items()):
        print(f"{reason:<24} {REASON_TEXT.get(reason, reason):<22} x{count}")
    print()
    print(f"read_committed 可见消息数: {len(analysis.visible_entries)}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    fragment, analysis, _ = _run(Path(args.log), Path(args.metadata))

    if fragment.error is not None:
        err = fragment.error
        print(
            f"导出中止: 批序号 {err.batch_index} 文件偏移 {err.file_offset} "
            f"发生 {err.kind} 错误: {err.message}",
            file=sys.stderr,
        )
        print(
            f"已确认前缀含 {len(fragment.batches)} 个批、"
            f"{fragment.bytes_consumed}/{fragment.total_bytes} 字节；"
            "为避免发布不完整数据集，未写出任何导出文件。",
            file=sys.stderr,
        )
        return EXIT_CORRUPT

    assert analysis is not None
    visible = analysis.visible_entries
    out_path = Path(args.output)
    # 先在内存中准备全部内容，最后一次性落盘，保证失败时不留下半成品。
    lines = [
        json.dumps(entry.to_export_dict(), ensure_ascii=False)
        for entry in visible
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )

    print(
        f"已导出 {len(visible)} 条 read_committed 可见消息 -> {out_path}",
        file=sys.stderr,
    )
    excluded = len(analysis.entries) - len(visible)
    print(f"排除记录 {excluded} 条，分类明细可用 inspect 查看", file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kafka-reader",
        description=(
            "离线读取 Kafka magic=2 RecordBatch 片段，"
            "按 read_committed 语义导出可见原始消息（不启动 broker）"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser(
        "inspect", help="解析并诊断整个片段（批/事务/可见性/损坏原因）"
    )
    p_inspect.add_argument("log", help="分区二进制日志片段路径")
    p_inspect.add_argument("metadata", help="分区边界 metadata.json 路径")
    p_inspect.add_argument("--json", action="store_true", help="输出 JSON")
    p_inspect.set_defaults(func=cmd_inspect)

    p_export = sub.add_parser(
        "export", help="导出 read_committed 可见消息为 JSON Lines"
    )
    p_export.add_argument("log", help="分区二进制日志片段路径")
    p_export.add_argument("metadata", help="分区边界 metadata.json 路径")
    p_export.add_argument("output", help="输出 JSONL 文件路径")
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BatchCorrupt as exc:  # 理论上 parse_fragment 已兜底
        print(f"错误: {exc.kind} @{exc.file_offset}: {exc.message}", file=sys.stderr)
        return EXIT_CORRUPT


if __name__ == "__main__":
    raise SystemExit(main())
