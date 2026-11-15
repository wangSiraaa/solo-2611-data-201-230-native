"""Command line interface: receive, inspect and export.

  python -m receiver receive --db out/recv.db --port 11883 [--once]
  python -m receiver inspect --db out/recv.db [--json] [--tail N]
  python -m receiver export  --db out/recv.db [--format jsonl|csv] [-o file]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import signal
import sys
import time
from pathlib import Path

from . import store as store_mod
from . import wire

DEFAULT_CLIENT_ID = "persistent-qos2-receiver-01"
DEFAULT_TOPIC = "telemetry/line"
STATE_LEGEND = {
    "received": "收到PUBLISH、负载已落库、待PUBREL（PUBREC已发）",
    "committed": "收到PUBREL、业务记录已入仓、PUBCOMP尚未开始",
    "completing": "PUBCOMP已置为发送中（崩溃恢复时只重发PUBCOMP）",
    "completed": "PUBCOMP已写出，包号已释放可承载新交付",
    "abandoned": "旧会话代因broker丢失会话而终止的未完成握手",
}


# -------------------------------------------------------------------- receive
def cmd_receive(args: argparse.Namespace) -> int:
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = store_mod.Store(args.db, args.client_id, args.topic)
    stop = {"flag": False}

    def _handle_signal(signum, _frame):
        stop["flag"] = True
        print(f"\n[receive] signal {signum} received, stopping after current work",
              file=sys.stderr)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    conn_ordinal = 0
    try:
        while not stop["flag"]:
            if args.max_connections and conn_ordinal >= args.max_connections:
                print(f"[receive] reached --max-connections={args.max_connections}, exit")
                break
            try:
                conn_ordinal += 1
                wire.run_connection(
                    db, host=args.host, port=args.port,
                    client_id=args.client_id, topic=args.topic,
                    conn_ordinal=conn_ordinal, keepalive=args.keepalive,
                    crash_before_pubcomp=args.crash_before_pubcomp)
            except (OSError, wire.BrokerClosed) as exc:
                # Connection refused -> wait for the broker; broker EOF ->
                # reconnect to continue the durable session. No readiness probe
                # is opened: a probe TCP connection would itself consume one of
                # the scripted fixture's exactly-N sessions.
                if stop["flag"]:
                    break
                if isinstance(exc, ConnectionRefusedError):
                    if conn_ordinal == 1 and args.wait_broker > 0:
                        deadline = time.monotonic() + args.wait_broker
                        while time.monotonic() < deadline and not stop["flag"]:
                            time.sleep(0.2)
                        if stop["flag"]:
                            break
                        continue
                    print(f"[receive] broker unavailable: {exc}", file=sys.stderr)
                if args.once:
                    print("[receive] --once: not reconnecting", file=sys.stderr)
                    break
                print(f"[receive] connection ended ({exc}); reconnect in 0.3s",
                      file=sys.stderr)
                time.sleep(0.3)
            except wire.ProtocolError as exc:
                print(f"[receive] protocol error, aborting: {exc}", file=sys.stderr)
                return 2
    finally:
        db.close()
    return 0


# -------------------------------------------------------------------- inspect
def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def cmd_inspect(args: argparse.Namespace) -> int:
    db = store_mod.Store(args.db, args.client_id, args.topic)
    try:
        if args.json:
            payload = {
                "counts": db.counts(),
                "sessions": [_row_to_dict(r) for r in db.list_sessions()],
                "deliveries": [
                    {**_row_to_dict(r),
                     "payload": bytes(r["payload"]).decode("utf-8", "replace")}
                    for r in db.list_deliveries()],
                "business_records": [
                    {**_row_to_dict(r),
                     "payload": bytes(r["payload"]).decode("utf-8", "replace")}
                    for r in db.list_business()],
                "protocol_events": [_row_to_dict(r)
                                    for r in db.list_events(args.tail)],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0

        print("=== 状态图例 ===")
        for state, meaning in STATE_LEGEND.items():
            print(f"  {state:<10} {meaning}")

        print("\n=== 计数 ===")
        for key, value in db.counts().items():
            print(f"  {key:<18} {value}")

        print("\n=== 会话代 (sessions) ===")
        for row in db.list_sessions():
            print(f"  gen={row['generation']}  started={row['started_at']}  "
                  f"ended={row['ended_at'] or '-'}  reason={row['end_reason'] or '-'}")

        print("\n=== 交付握手 (deliveries: 每行是一代中的一个包号握手) ===")
        for row in db.list_deliveries():
            payload_text = bytes(row["payload"]).decode("utf-8", "replace")
            print(f"  delivery_id={row['delivery_id']} gen={row['generation']} "
                  f"pkt={row['packet_id']} state={row['state']:<9} "
                  f"sha256={row['payload_sha256'][:16]}… payload={payload_text}")

        print("\n=== 业务记录 (business_records: 每次完成交付恰好一条) ===")
        for row in db.list_business():
            payload_text = bytes(row["payload"]).decode("utf-8", "replace")
            print(f"  business_seq={row['business_seq']} "
                  f"delivery_id={row['delivery_id']} gen={row['generation']} "
                  f"pkt={row['packet_id']} payload={payload_text} "
                  f"committed_at={row['committed_at']}")

        print(f"\n=== 协议事件 (protocol_events, tail={args.tail}) ===")
        for row in db.list_events(args.tail):
            pid = f"pkt={row['packet_id']}" if row["packet_id"] is not None else ""
            extra = row["detail"] or ""
            transition = ""
            if row["state_from"] or row["state_to"]:
                transition = f" {row['state_from'] or '-'} -> {row['state_to'] or '-'}"
            print(f"  #{row['event_id']:<3} {row['ts']} "
                  f"{row['direction']:<8} {row['packet_type']:<8} "
                  f"{row['event']:<34} gen={row['generation']} {pid}{transition} {extra}")
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------- export
def cmd_export(args: argparse.Namespace) -> int:
    db = store_mod.Store(args.db, args.client_id, args.topic)
    try:
        rows = []
        for row in db.list_business():
            rows.append({
                "business_seq": row["business_seq"],
                "delivery_id": row["delivery_id"],
                "generation": row["generation"],
                "packet_id": row["packet_id"],
                "payload_sha256": row["payload_sha256"],
                "payload": bytes(row["payload"]).decode("utf-8", "replace"),
                "committed_at": row["committed_at"],
            })

        if args.format == "jsonl":
            text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer,
                fieldnames=["business_seq", "delivery_id", "generation",
                            "packet_id", "payload_sha256", "payload",
                            "committed_at"])
            writer.writeheader()
            writer.writerows(rows)
            text = buffer.getvalue()

        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"exported {len(rows)} business records -> {args.output}")
        else:
            sys.stdout.write(text)
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------------ cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m receiver",
        description="Persistent MQTT 3.1.1 QoS2 receiver (single client/topic)")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="out/receiver.db")
    common.add_argument("--host", default="127.0.0.1")
    common.add_argument("--port", type=int, default=11883)
    common.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    common.add_argument("--topic", default=DEFAULT_TOPIC)

    recv = sub.add_parser("receive", parents=[common],
                          help="run the durable receiver (reconnects)")
    recv.add_argument("--once", action="store_true",
                      help="handle one connection then exit (process recovery)")
    recv.add_argument("--max-connections", type=int, default=0,
                      help="stop after N connections (0 = unlimited)")
    recv.add_argument("--wait-broker", type=float, default=15.0,
                      help="seconds to wait for the broker on the first attempt")
    recv.add_argument("--keepalive", type=int, default=60)
    recv.add_argument("--crash-before-pubcomp", action="store_true",
                      help="fault injection: hard-exit after the durable "
                           "completing marker, before writing PUBCOMP")
    recv.set_defaults(func=cmd_receive)

    insp = sub.add_parser("inspect", parents=[common],
                          help="show durable protocol/business state")
    insp.add_argument("--json", action="store_true")
    insp.add_argument("--tail", type=int, default=200,
                      help="show the last N protocol events in text mode")
    insp.set_defaults(func=cmd_inspect)

    exp = sub.add_parser("export", parents=[common],
                         help="export completed business records")
    exp.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    exp.add_argument("-o", "--output", help="output file (default stdout)")
    exp.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
