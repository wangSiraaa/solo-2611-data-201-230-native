"""命令行入口：sync / status / export。

仅面向本地单 zone、单主服务器的受限场景；不做自动发现、多主合并或 DNSSEC。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Storage, SyncConfig, SyncEngine
from .model import Snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="单区域 AXFR/IXFR 权威记录快照采集器",
    )
    parser.add_argument(
        "--state-dir", default="state",
        help="快照与状态文件目录（默认 ./state）",
    )
    parser.add_argument("--zone", default="example.test.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11535)
    parser.add_argument("--timeout", type=float, default=10.0)

    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="采集或增量同步（首次自动 AXFR）")
    sync_p.add_argument(
        "--full", action="store_true",
        help="显式重新 AXFR（差量缺口后用它整库重建，不混合两个版本）",
    )

    sub.add_parser("status", help="查看当前快照与最近同步状态")

    export_p = sub.add_parser("export", help="导出快照")
    export_p.add_argument(
        "--format", choices=("zone", "json"), default="zone",
        help="zone=冻结文件风格文本，json=含来源标注的结构化数据",
    )
    return parser


def cmd_sync(args: argparse.Namespace) -> int:
    config = SyncConfig(args.zone, args.host, args.port, args.timeout)
    engine = SyncEngine(config, Storage(args.state_dir))
    result = engine.sync(force_axfr=args.full)
    print(json.dumps(
        {
            "ok": result.ok,
            "action": result.action,
            "outcome": result.outcome,
            "serial_before": result.serial_before,
            "serial_after": result.serial_after,
            "folded": result.folded,
            "sources": result.sources,
            "error_type": result.error_type,
            "reason": result.reason,
        },
        ensure_ascii=False,
    ))
    return 0 if result.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    storage = Storage(args.state_dir)
    status = storage.load_status()
    snapshot = storage.load_snapshot()
    payload: dict = {
        "state_dir": str(storage.state_dir),
        "snapshot_present": snapshot is not None,
    }
    if snapshot is not None:
        payload["snapshot"] = {
            "zone": snapshot.zone,
            "serial": snapshot.serial,
            "soa_serial": snapshot.soa_serial(),
            "rrset_count": len(snapshot.rrsets()),
            "rdata_count": len(snapshot.records),
            "sources": snapshot.sources(),
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
        }
    payload["status"] = status
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    storage = Storage(args.state_dir)
    snapshot = storage.load_snapshot()
    if snapshot is None:
        print(json.dumps(
            {"ok": False, "reason": "本地尚无快照，先执行 sync"},
            ensure_ascii=False,
        ))
        return 2
    if args.format == "zone":
        sys.stdout.write(snapshot.to_zone_text())
    else:
        print(json.dumps(snapshot.to_json(), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "export":
        return cmd_export(args)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
