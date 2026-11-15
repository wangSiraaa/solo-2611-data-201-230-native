"""命令行入口:plan / load / verify。"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

from .errors import CatalogUnsupported, LoadFailed, RouteCopyError, StalePlan, UsageError
from .loader import load_plan_file, render_load_md, run_load
from .planner import build_plan, render_plan_md
from .verifier import render_verify_md, run_verify

REQUIRED_CONFIG_KEYS = ("parents", "batch_rows", "supported_types", "max_partition_depth")


def load_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError as e:
        raise UsageError(f"无法读取配置 {path!r}: {e}")
    except json.JSONDecodeError as e:
        raise UsageError(f"配置 {path!r} 不是合法 JSON: {e}")
    for k in REQUIRED_CONFIG_KEYS:
        if k not in cfg:
            raise UsageError(f"配置缺少键 {k!r}")
    if not isinstance(cfg["parents"], list) or not cfg["parents"]:
        raise UsageError("config.parents 必须是非空列表")
    if int(cfg["batch_rows"]) < 1:
        raise UsageError("config.batch_rows 必须 >= 1")
    return cfg


def write_text(path: str, text: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_json(path: str, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def connect(args, read_only: bool):
    dsn = args.dsn or os.environ.get("ROUTECOPY_DSN")
    if not dsn:
        raise UsageError("缺少数据库连接:请给 --dsn 或设置 ROUTECOPY_DSN")
    conn = psycopg.connect(dsn)
    if read_only:
        conn.read_only = True  # plan 阶段硬性只读:任何写都会直接报错
    return conn


def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    if args.parent not in cfg["parents"]:
        raise UsageError(
            f"父表 {args.parent!r} 不在 config.parents 允许列表 {cfg['parents']} 内"
        )
    conn = connect(args, read_only=True)
    try:
        with conn:  # 只读事务
            with conn.cursor() as cur:
                plan = build_plan(cur, args.parent, args.input, cfg)
    finally:
        conn.close()
    write_json(args.plan_out, plan)
    if args.report_out:
        write_text(args.report_out, render_plan_md(plan))
    s = plan["summary"]
    print(f"plan: {s['total']} 行 → 接受 {s['accepted']},拒绝 {s['rejected']};"
          f"指纹 {plan['catalog_fingerprint'][:16]}… → {args.plan_out}")
    return 0


def cmd_load(args) -> int:
    plan = load_plan_file(args.plan)
    conn = connect(args, read_only=False)
    try:
        try:
            report = run_load(conn, plan, args.input)
        except StalePlan as e:
            report = {"status": "stale_rejected", "parent": plan["parent"], **e.payload}
            write_json(args.out, report)
            if args.report_out:
                write_text(args.report_out, render_load_md(report))
            print(f"load: 计划已过期,拒绝执行 → {args.out}", file=sys.stderr)
            return e.exit_code
        except LoadFailed as e:
            report = {"status": "rolled_back", "parent": plan["parent"], **e.payload}
            write_json(args.out, report)
            if args.report_out:
                write_text(args.report_out, render_load_md(report))
            print(f"load: 失败并已整批回滚: {e.message} → {args.out}", file=sys.stderr)
            return e.exit_code
    finally:
        conn.close()
    write_json(args.out, report)
    if args.report_out:
        write_text(args.report_out, render_load_md(report))
    print(f"load: 已提交 {report['total_rows']} 行到 {len(report['leaves'])} 个叶 → {args.out}")
    return 0


def cmd_verify(args) -> int:
    plan = load_plan_file(args.plan)
    conn = connect(args, read_only=False)
    try:
        report = run_verify(conn, plan, args.expect)
    finally:
        conn.close()
    write_json(args.out, report)
    if args.report_out:
        write_text(args.report_out, render_verify_md(report))
    if report["ok"]:
        print(f"verify: 全部核对通过 → {args.out}")
        return 0
    print(f"verify: 存在不一致 → {args.out}", file=sys.stderr)
    return 5


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="routecopy",
        description="目录驱动的分区路由计划(plan)与批量 COPY 装载(load),含独立验证(verify)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="只读目录,生成全批路由计划")
    p.add_argument("--config", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--plan-out", required=True)
    p.add_argument("--report-out")
    p.add_argument("--dsn")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("load", help="复核计划后单事务批量 COPY 到叶表")
    p.add_argument("--plan", required=True)
    p.add_argument("--input", required=True, help="仅用于重算摘要核对新鲜度")
    p.add_argument("--out", required=True)
    p.add_argument("--report-out")
    p.add_argument("--dsn")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("verify", help="独立核对:readback + 父表路由 oracle + 拒绝行核对")
    p.add_argument("--plan", required=True)
    p.add_argument("--expect", choices=["loaded", "empty", "none"], default="loaded")
    p.add_argument("--out", required=True)
    p.add_argument("--report-out")
    p.add_argument("--dsn")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except CatalogUnsupported as e:
        print(f"不支持的目录形态: {e.message}", file=sys.stderr)
        return e.exit_code
    except RouteCopyError as e:
        print(f"错误: {e.message}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
