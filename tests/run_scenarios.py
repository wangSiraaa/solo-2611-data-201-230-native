#!/usr/bin/env python3
"""端到端场景运行器：驱动 fixtures/mock_dns.py 的真实 TCP 服务。

覆盖题目要求的全部场景：
  1. 基线 AXFR
  2. 相同 serial 的无变化 IXFR（不重写内容）
  3. 连续两段 IXFR（100->101->102，含 TTL 变化）
  4. serial 回绕 4294967295 -> 0
  5. 服务端 IXFR 回退完整 AXFR 的识别与整库重建
  6. 坏差量（serial 缺口）保留旧快照，随后显式重新 AXFR
  7. 半传输（最后消息截断）保留旧快照

每阶段以 fixtures 下冻结 .zone 文件独立比对 RRset 集合与 TTL。
另含进程内单元级检查：TXT 多字串单 RDATA、重复 RDATA 折叠与来源、
大小写不敏感、删除失败语义、AXFR 围栏失败语义。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype

from collector.model import Record, Snapshot
from collector import transfer

ROOT = Path(__file__).resolve().parent.parent
fixtures = ROOT / "fixtures"
PORT = 11535
ZONE = "example.test."

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(f"{label} {detail}")


class MockServer:
    def __init__(self, stage: str, work: Path) -> None:
        self.stage = stage
        self.log = work / f"dns-{stage}.jsonl"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(fixtures / "mock_dns.py"),
             "--stage", self.stage, "--port", str(PORT), "--log", str(self.log)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"mock {self.stage} 启动失败: "
                        f"{self.proc.stderr.read()}"
                    )
                continue
            payload = json.loads(line)
            if payload.get("ready"):
                return
        raise RuntimeError(f"mock {self.stage} 等待 ready 超时")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        # 确保端口释放后再进入下一阶段
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", PORT)) != 0:
                    return
            time.sleep(0.1)


def run_cli(state: Path, *extra: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "collector.cli",
         "--state-dir", str(state), "--zone", ZONE,
         "--host", "127.0.0.1", "--port", str(PORT), *extra],
        capture_output=True, text=True,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"parse_error": proc.stdout, "stderr": proc.stderr}
    return proc.returncode, payload


def frozen_signature(zone_file: Path) -> set[tuple[str, str, int, str]]:
    """解析冻结 zone 文件为 (owner, 类型, TTL, RDATA) 集合。"""
    sig: set[tuple[str, str, int, str]] = set()
    for line in zone_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        owner, ttl, _class, rtype, rdata_text = line.split(" ", 4)
        rdata = dns.rdata.from_text(
            dns.rdataclass.IN, dns.rdatatype.from_text(rtype), rdata_text
        )
        sig.add((owner.lower(), rtype, int(ttl), rdata.to_text()))
    return sig


def snapshot_signature(state: Path) -> set[tuple[str, str, int, str]]:
    snapshot = Snapshot.from_json(json.loads((state / "snapshot.json").read_text()))
    return snapshot.rrset_signature()


def expect_match(state: Path, zone_name: str, label: str) -> None:
    actual = snapshot_signature(state)
    expected = frozen_signature(fixtures / zone_name)
    check(actual == expected, label,
          f"差异: 缺={sorted(expected - actual)} 多={sorted(actual - expected)}")


def scenario(server_stage: str, state: Path, sync_args: list[str],
             expect_rc: int, expect_outcome: str, expect_serial: int | None,
             zone_match: str | None, label: str,
             frozen_before: set | None = None) -> None:
    print(f"场景: {label}")
    work = state.parent
    server = MockServer(server_stage, work)
    server.start()
    try:
        rc, payload = run_cli(state, "sync", *sync_args)
    finally:
        server.stop()
    check(rc == expect_rc, f"退出码 {rc}=={expect_rc}", json.dumps(payload, ensure_ascii=False))
    check(payload.get("outcome") == expect_outcome,
          f"outcome={payload.get('outcome')} 期望 {expect_outcome}")
    check(payload.get("serial_after") == expect_serial,
          f"serial_after={payload.get('serial_after')} 期望 {expect_serial}")
    if zone_match is not None and expect_rc == 0:
        expect_match(state, zone_match, "RRset 集合与 TTL 与冻结 zone 一致")
    if frozen_before is not None:
        check(snapshot_signature(state) == frozen_before,
              "失败后旧快照未被替换（半传输/坏差量保护）")


def unit_checks() -> None:
    print("进程内数据模型与失败语义检查:")

    snap = Snapshot(zone=ZONE, serial=100)
    txt = dns.rdata.from_text(
        dns.rdataclass.IN, dns.rdatatype.TXT, '"first part" "second part"'
    )
    # TXT 两个字串是一条 RDATA，不展开
    rec = Record.from_rr(dns.name.from_text(f"{ZONE}"), 60,
                         dns.rdataclass.IN, txt, "test")
    assert snap.add(rec)
    assert len(snap.records) == 1
    view = next(v for v in snap.rrsets() if v.rtype == dns.rdatatype.TXT)
    check(len(view.rdata) == 1 and len(view.rdata[0].rdata_wire) > 0,
          "TXT 多字串仍为一条 RDATA（不展开成多条记录）")

    # 同一 RRset 内重复 RDATA 折叠并保留最早来源
    duplicate = Record.from_rr(dns.name.from_text(ZONE), 999,
                               dns.rdataclass.IN, txt, "later-transfer")
    check(snap.add(duplicate) is False and len(snap.records) == 1,
          "重复 RDATA 被折叠")
    stored = next(iter(snap.records.values()))
    check(stored.source == "test", "折叠后保留最早来源标注")

    # 大小写不敏感：大写 owner 与小写 owner 身份相同
    upper = Record.from_rr(dns.name.from_text("EXAMPLE.TEST."), 60,
                           dns.rdataclass.IN, txt, "upper")
    check(upper.identity == rec.identity, "域名比较大小写不敏感")

    # 删除不存在的身份必须报错而不是静默成功
    a_rdata = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.A, "192.0.2.9")
    ghost = Record.from_rr(dns.name.from_text(f"ghost.{ZONE}"), 60,
                           dns.rdataclass.IN, a_rdata, "test")
    try:
        snap.remove(ghost)
        check(False, "删除缺失记录应抛 KeyError")
    except KeyError:
        check(True, "删除缺失记录按身份判定为失败（引擎据此中止 IXFR）")

    # AXFR 围栏：首尾 SOA serial 不符必须拒绝
    apex = dns.name.from_text(ZONE)
    def soa_rr(serial: int) -> transfer.WireRR:
        rd = dns.rdata.from_text(
            dns.rdataclass.IN, dns.rdatatype.SOA,
            f"ns.{ZONE} hm.{ZONE} {serial} 3600 600 86400 60",
        )
        return transfer.WireRR(apex, 60, dns.rdataclass.IN, dns.rdatatype.SOA, rd)
    try:
        transfer.validate_axfr([soa_rr(1), soa_rr(2)], apex)
        check(False, "围栏 serial 不符应抛 FenceError")
    except transfer.FenceError:
        check(True, "AXFR 围栏 serial 不符被拒绝（保留旧快照）")

    # 连接中断的半传输：消息体收不齐
    class DeadSocket:
        def settimeout(self, *_): ...
        def recv(self, _n):
            return b""  # 立即 EOF
    try:
        transfer._recv_exact(DeadSocket(), 10)  # type: ignore[arg-type]
        check(False, "半传输应抛 EOFError")
    except EOFError:
        check(True, "消息中间 EOF 识别为不完整传送")


def main() -> int:
    work = ROOT / "test-output"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    unit_checks()

    # 1. 基线 AXFR
    state = work / "state-main"
    scenario("base", state, [], 0, "published", 100, "base.zone",
             "基线：首次 AXFR 建立快照（围栏确认完整）")

    # 2. 相同 serial 无变化响应
    before = snapshot_signature(state)
    scenario("unchanged", state, [], 0, "unchanged", 100, None,
             "无变化：相同 serial 的单 SOA 响应不重写记录")
    check(snapshot_signature(state) == before, "无变化响应后记录内容保持不变")

    # 3. 连续两段 IXFR 100->101->102（第二段是 TTL 60->120 的删旧加新）
    scenario("two", state, [], 0, "published", 102, "two.zone",
             "连续两段 IXFR：逐段链接应用，TTL 变化正确")

    # 4. 半传输：最后消息截断，旧快照必须保留
    before = snapshot_signature(state)
    scenario("truncated", state, [], 1, "failed", 102, None,
             "半传输：截断消息不发布新快照", frozen_before=before)
    status = json.loads((state / "status.json").read_text())
    check(status["last_error_type"] == "ConnectionTransferError",
          f"状态留存失败原因: {status.get('last_error_type')}")

    # 5. 坏差量：首段旧 serial 99 接不上本地 100（独立目录，保持 serial 100）
    gap_state = work / "state-gap"
    scenario("base", gap_state, [], 0, "published", 100, "base.zone",
             "缺口前置：独立目录建立 serial 100 基线")
    gap_before = snapshot_signature(gap_state)
    scenario("bad-gap", gap_state, [], 1, "failed", 100, None,
             "坏差量：首段旧 serial 99 接不上本地 100，拒绝整段差量",
             frozen_before=gap_before)
    status = json.loads((gap_state / "status.json").read_text())
    check(status["last_error_type"] == "DeltaGapError",
          f"缺口原因已留存: {status.get('last_error_type')}")
    check("缺口" in (status["last_reason"] or "") and "重新 AXFR" in (status["last_reason"] or ""),
          f"状态原因提示显式重新 AXFR: {status.get('last_reason')}")
    # 显式重新 AXFR 恢复，不混合两个版本
    scenario("bad-gap", gap_state, ["--full"], 0, "published", 102,
             "bad-gap.zone", "显式重新 AXFR：整库重建到冻结内容")

    # 6. 服务端 AXFR 回退（从 serial 102 的快照发起 IXFR，服务端给 AXFR 形态 103）
    scenario("fallback", state, [], 0, "published", 103, "fallback.zone",
             "AXFR 回退：识别完整 AXFR 形态并整库重建，不按删增解释")
    snap = Snapshot.from_json(json.loads((state / "snapshot.json").read_text()))
    check(any("axfr-fallback" in src for src in snap.sources()),
          f"回退重建带来源标注: {snap.sources()}")

    # 7. serial 回绕：独立状态目录，先 wrap-base 再 wrap
    wrap_state = work / "state-wrap"
    scenario("wrap-base", wrap_state, [], 0, "published", 4294967295,
             "wrap-base.zone", "回绕基线：serial 4294967295 的 AXFR")
    scenario("wrap", wrap_state, [], 0, "published", 0, "wrap.zone",
             "回绕增量：4294967295 -> 0 按序列号算术合法前进")

    print()
    if failures:
        print(f"共 {len(failures)} 项失败:")
        for item in failures:
            print(" -", item)
        return 1
    print("全部场景通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
