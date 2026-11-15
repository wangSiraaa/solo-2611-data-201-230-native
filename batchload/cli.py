"""命令行入口：init / load-batch / report / verify。

用法见 --help。一条本机演示命令由仓库根目录 demo.sh 提供。
"""
from __future__ import annotations

import argparse
import sys
import uuid

import psycopg

from .config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_USER, DEFAULT_INPUT, ConnInfo, InputConfig
from .db import init_database
from .loader import Loader
from .report import (
    print_copy_log,
    print_full_report,
    print_rejects,
)
from .verify import verify_against_reference


def _conn_info(args: argparse.Namespace) -> ConnInfo:
    return ConnInfo(host=args.host, port=args.port, user=args.user,
                    dbname=args.dbname)


def cmd_init(args: argparse.Namespace) -> int:
    conn = _conn_info(args)
    init_database(conn, args.dbname, recreate=args.recreate,
                  schema_path=args.schema)
    print(f"[init] 数据库 {args.dbname} 就绪({args.host}:{args.port})，"
          "已部署 schema.sql 与审计表 rejects/copy_attempt_log")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    conn = _conn_info(args)
    input_cfg = InputConfig.load(args.input)
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"
    # 业务连接：每块一个事务；审计连接：自动提交，失败尝试也留存。
    with psycopg.connect(conn.dsn()) as main_conn, psycopg.connect(
        conn.dsn(), autocommit=True
    ) as audit_conn:
        loader = Loader(main_conn, audit_conn, run_id, input_cfg, args.block_rows)
        outcomes = loader.run()
    print(f"[load-batch] run_id={run_id} block_rows={args.block_rows}")
    total_in = total_acc = total_rej = 0
    for table, oc in outcomes.items():
        print(f"  {table}: 输入 {oc.total} | 接受 {oc.accepted} | 拒绝 {oc.rejected}")
        total_in += oc.total
        total_acc += oc.accepted
        total_rej += oc.rejected
    print(f"  合计: 输入 {total_in} = 接受 {total_acc} + 拒绝 {total_rej}")
    if args.print_log:
        print_copy_log(conn, args.dbname, run_id, block_rows=args.block_rows)
    print(f"RUN_ID={run_id}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = _conn_info(args)
    if not args.run_id:
        with psycopg.connect(conn.dsn()) as c:
            row = c.execute(
                "SELECT run_id FROM import_receipts "
                "GROUP BY run_id ORDER BY max(run_id) DESC LIMIT 1"
            ).fetchone()
        if not row:
            print("没有可用 run_id，请先用 load-batch 导入", file=sys.stderr)
            return 2
        args.run_id = row[0]
    if args.rejects_only:
        print_rejects(conn, args.dbname, args.run_id)
    elif args.log_only:
        print_copy_log(conn, args.dbname, args.run_id, block_rows=args.block_rows)
    else:
        print_full_report(conn, args.dbname, args.run_id,
                          block_rows=args.block_rows)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    conn = _conn_info(args)
    input_cfg = InputConfig.load(args.input)
    if not args.run_id:
        with psycopg.connect(conn.dsn()) as c:
            row = c.execute(
                "SELECT run_id FROM import_receipts "
                "GROUP BY run_id ORDER BY max(run_id) DESC LIMIT 1"
            ).fetchone()
        if not row:
            print("没有可用 run_id，请先用 load-batch 导入", file=sys.stderr)
            return 2
        args.run_id = row[0]

    report, reference = verify_against_reference(
        conn, args.dbname, args.run_id, input_cfg,
        ref_dbname=args.ref_dbname,
    )
    print(f"[verify] run_id={args.run_id} 目标库={args.dbname}")
    for line in report.checks:
        print("  " + line)
    print("\n独立参考库(逐条 INSERT)接受集合：")
    for table, sets in reference.items():
        print(f"  {table}: 接受 {sorted(sets['accepted'])} 拒绝 {sorted(sets['rejected'])}")
    print("\nVERIFY:", "ALL CHECKS PASSED" if report.ok else "FAILED")
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m batchload",
        description="坏行二分隔离的批量 COPY 入库(仅 customers/orders 两表)。",
    )
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--dbname", default="batchload")
    p.add_argument("--input", default=str(DEFAULT_INPUT))

    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="建独立库并部署 schema + 审计表")
    pi.add_argument("--recreate", action="store_true", help="已存在则先删后建")
    pi.add_argument("--schema", default="fixtures/schema.sql")
    pi.set_defaults(func=cmd_init)

    pl = sub.add_parser("load-batch", help="按 block_rows 真实 COPY 分批导入并二分隔离坏行")
    pl.add_argument("--block-rows", type=int, default=5, dest="block_rows")
    pl.add_argument("--run-id", default=None)
    pl.add_argument("--print-log", action="store_true", help="结束后打印 COPY 调用日志")
    pl.set_defaults(func=cmd_load)

    pr = sub.add_parser("report", help="输出回执、拒绝明细与 COPY 调用日志")
    pr.add_argument("--run-id", default=None)
    pr.add_argument("--rejects-only", action="store_true")
    pr.add_argument("--log-only", action="store_true")
    pr.add_argument("--block-rows", type=int, default=None, dest="block_rows",
                    help="可选，用于核对全好块是否为真实整块 COPY")
    pr.set_defaults(func=cmd_report)

    pv = sub.add_parser("verify", help="库内对账 + 独立参考库逐条 INSERT 核对")
    pv.add_argument("--run-id", default=None)
    pv.add_argument("--ref-dbname", default=None, dest="ref_dbname")
    pv.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
