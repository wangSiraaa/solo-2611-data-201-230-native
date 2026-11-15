"""同步引擎：在工作副本上应用传送结果，全部核对通过后才原子发布。

关键不变量：
- 任何失败（连接中断、围栏不符、差量缺口、删除缺失、serial 不可比较）
  都只写状态文件，绝不触碰已发布快照；
- IXFR 先在快照副本上逐段应用，最终 SOA 与 serial 链核对完成后才落盘；
- 服务端回退的 AXFR 走整库重建，绝不按 IXFR 删增序列解释。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import dns.rdatatype

from . import serialutil
from .model import Record, Snapshot, utc_now
from . import transfer

SNAPSHOT_NAME = "snapshot.json"
STATUS_NAME = "status.json"
HISTORY_LIMIT = 20


@dataclass
class SyncConfig:
    zone: str
    host: str = "127.0.0.1"
    port: int = 11535
    timeout: float = transfer.DEFAULT_TIMEOUT


@dataclass
class SyncResult:
    ok: bool
    action: str                      # axfr / ixfr / forced-axfr / none
    outcome: str                     # published / unchanged / failed
    serial_before: int | None
    serial_after: int | None
    reason: str | None = None
    error_type: str | None = None
    folded: int = 0                  # 折叠掉的重复 RDATA 条数
    sources: list[str] = field(default_factory=list)


class Storage:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir)
        self.snapshot_path = self.state_dir / SNAPSHOT_NAME
        self.status_path = self.state_dir / STATUS_NAME

    def load_snapshot(self) -> Snapshot | None:
        if not self.snapshot_path.exists():
            return None
        return Snapshot.from_json(json.loads(self.snapshot_path.read_text()))

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot.to_json(), indent=2, ensure_ascii=False))
        os.replace(tmp, self.snapshot_path)  # 原子替换

    def load_status(self) -> dict | None:
        if not self.status_path.exists():
            return None
        return json.loads(self.status_path.read_text())

    def save_status(self, status: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        os.replace(tmp, self.status_path)


def _record_from_wire(rr: transfer.WireRR, source: str) -> Record:
    return Record.from_rr(rr.name, rr.ttl, rr.rdclass, rr.rdata, source)


def _build_snapshot_from_axfr(
    zone: str, serial: int, rrs: list[transfer.WireRR], source: str
) -> tuple[Snapshot, int]:
    snapshot = Snapshot(zone=zone, serial=serial)
    folded = 0
    for rr in rrs:  # 首尾 SOA 身份相同，重复 SOA 在此自然折叠为一条
        if not snapshot.add(_record_from_wire(rr, source)):
            folded += 1
    soa_serial = snapshot.soa_serial()
    if soa_serial is None:
        raise transfer.FenceError("AXFR 结果中缺少顶点 SOA 记录")
    if soa_serial != serial:
        # 兜底：SOA RDATA 内 serial 必须与围栏 serial 一致
        raise transfer.FenceError(
            f"SOA RDATA serial {soa_serial} 与围栏 serial {serial} 不一致"
        )
    return snapshot, folded


def _require_forward(old_serial: int, new_serial: int) -> None:
    try:
        direction = serialutil.compare(new_serial, old_serial)
    except serialutil.IncomparableSerial as exc:
        raise transfer.TransferError(str(exc)) from exc
    if direction == 0:
        raise transfer.IxfrStructureError(
            f"差量新旧 serial 相同（{old_serial}），不是有效前进"
        )
    if direction < 0:
        raise transfer.IxfrStructureError(
            f"差量 serial 回退：{old_serial} -> {new_serial}"
        )


class SyncEngine:
    def __init__(self, config: SyncConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    # ---- 状态留存 -----------------------------------------------------

    def _write_status(self, result: SyncResult) -> None:
        status = self.storage.load_status() or {
            "zone": self.config.zone,
            "history": [],
        }
        entry = {
            "at": utc_now(),
            "action": result.action,
            "outcome": result.outcome,
            "serial_before": result.serial_before,
            "serial_after": result.serial_after,
            "folded": result.folded,
            "sources": result.sources,
        }
        if result.reason is not None:
            entry["error_type"] = result.error_type
            entry["reason"] = result.reason
        status.update(
            {
                "zone": self.config.zone,
                "last_attempt_at": entry["at"],
                "last_action": result.action,
                "last_outcome": result.outcome,
                "last_reason": result.reason,
                "last_error_type": result.error_type,
                "current_serial": result.serial_after,
                "snapshot_present": self.storage.snapshot_path.exists(),
            }
        )
        history = status.setdefault("history", [])
        history.append(entry)
        del history[:-HISTORY_LIMIT]
        self.storage.save_status(status)

    # ---- 主入口 -------------------------------------------------------

    def sync(self, force_axfr: bool = False) -> SyncResult:
        snapshot = self.storage.load_snapshot()
        before = snapshot.serial if snapshot is not None else None
        action = "forced-axfr" if force_axfr else ("axfr" if snapshot is None else "ixfr")
        try:
            if snapshot is None or force_axfr:
                result = self._do_axfr(action, before)
            else:
                result = self._do_ixfr(snapshot, before)
        except transfer.TransferError as exc:
            result = SyncResult(
                ok=False,
                action=action,
                outcome="failed",
                serial_before=before,
                serial_after=before,  # 旧快照保留，serial 不变
                reason=str(exc),
                error_type=type(exc).__name__,
            )
        self._write_status(result)
        return result

    # ---- AXFR / 回退 AXFR --------------------------------------------

    def _do_axfr(self, action: str, before: int | None) -> SyncResult:
        serial, rrs = transfer.fetch_axfr(
            self.config.host, self.config.port, self.config.zone,
            self.config.timeout,
        )
        source = (
            f"axfr serial={serial} at {utc_now()}"
            if action == "axfr"
            else f"{action} serial={serial} at {utc_now()}"
        )
        snapshot, folded = _build_snapshot_from_axfr(
            self.config.zone, serial, rrs, source
        )
        snapshot.updated_at = utc_now()
        self.storage.save_snapshot(snapshot)  # 核对全部通过后才发布
        return SyncResult(
            ok=True, action=action, outcome="published",
            serial_before=before, serial_after=serial,
            folded=folded, sources=snapshot.sources(),
        )

    # ---- IXFR ---------------------------------------------------------

    def _do_ixfr(self, snapshot: Snapshot, before: int) -> SyncResult:
        local_soa = snapshot.soa_record()
        if local_soa is None:
            raise transfer.FenceError("本地快照缺少顶点 SOA，无法构造 IXFR 请求")
        template = transfer.make_template_soa(self.config.zone, snapshot.serial)
        response = transfer.fetch_ixfr(
            self.config.host, self.config.port, self.config.zone,
            snapshot.serial, template, self.config.timeout,
        )
        outcome, new_snapshot, folded = apply_ixfr_response(snapshot, response)
        if outcome == "unchanged":
            return SyncResult(
                ok=True, action="ixfr", outcome="unchanged",
                serial_before=before, serial_after=before,
                sources=snapshot.sources(),
            )
        if outcome == "axfr":
            new_snapshot.created_at = snapshot.created_at
        new_snapshot.updated_at = utc_now()
        self.storage.save_snapshot(new_snapshot)  # 全部核对完成后才发布
        return SyncResult(
            ok=True, action="ixfr", outcome="published",
            serial_before=before, serial_after=new_snapshot.serial,
            folded=folded, sources=new_snapshot.sources(),
        )


def apply_ixfr_response(
    snapshot: Snapshot, response: transfer.IxfrResponse
) -> tuple[str, Snapshot, int]:
    """在工作副本上应用已解析的 IXFR 响应（纯计算，不做 IO）。

    返回 (outcome, 新快照, 折叠数)，outcome 为 unchanged / axfr / ixfr。
    任何核对失败都抛 transfer.TransferError，调用方不得发布。
    """
    # 1) 相同 serial 的无变化响应：不重写任何记录内容
    if response.kind == "nochange":
        if response.high_serial != snapshot.serial:
            raise transfer.IxfrStructureError(
                f"无变化响应的 SOA serial {response.high_serial} "
                f"与本地 {snapshot.serial} 不一致"
            )
        return "unchanged", snapshot, 0

    # 2) 服务端回退完整 AXFR：识别后整库重建，绝不按删增序列解释
    if response.kind == "axfr":
        source = f"axfr-fallback serial={response.high_serial} at {utc_now()}"
        rebuilt, folded = _build_snapshot_from_axfr(
            snapshot.zone, response.high_serial, list(response.axfr_rrs), source
        )
        return "axfr", rebuilt, folded

    # 3) 真正的 RFC 1995 差量：在工作副本上逐段应用
    working = snapshot.copy()
    expected_serial = snapshot.serial
    folded = 0
    ts = utc_now()
    for index, delta in enumerate(response.deltas, start=1):
        # 每段旧 serial 必须接上本地版本或前段新 serial（严格相等链接）
        if delta.old_serial != expected_serial:
            raise transfer.DeltaGapError(
                f"第 {index} 段差量存在缺口：期望旧 serial "
                f"{expected_serial}，服务端给出 {delta.old_serial}；"
                f"本地停留在 serial {snapshot.serial}，可显式重新 AXFR"
            )
        _require_forward(delta.old_serial, delta.new_serial)

        tag = f"ixfr {delta.old_serial}->{delta.new_serial} at {ts}"
        # 删除必须在当时基线确实存在，失败即中止，不得宣布成功
        for rr in delta.deletes:
            victim = _record_from_wire(rr, tag)
            existing = working.get_identity(victim)
            if existing is None:
                raise transfer.DeleteMissingError(
                    f"第 {index} 段删除集要求删除的记录在当前基线不存在: "
                    f"{rr.name} {dns.rdatatype.to_text(rr.rdtype)} "
                    f"{rr.rdata.to_text()}"
                )
            working.remove(existing)
        for rr in delta.adds:
            if not working.add(_record_from_wire(rr, tag)):
                folded += 1
        expected_serial = delta.new_serial

    # 4) 最终核对：链尾 serial、收尾 SOA、整体 serial 一致
    if expected_serial != response.high_serial:
        raise transfer.IxfrStructureError(
            f"差量链尾 serial {expected_serial} 与收尾 SOA "
            f"{response.high_serial} 不一致"
        )
    closing = response.closing
    assert closing is not None
    if closing.serial != response.high_serial:
        raise transfer.FenceError("收尾 SOA 的 RDATA serial 与围栏不符")

    # 围栏 SOA 不在删增集里（mock 与多数实现如此）：用收尾 SOA 替换
    # 工作副本的顶点 SOA；若差量本身已替换过 SOA，则核对一致性即可。
    old_soa = working.soa_record()
    tag = f"ixfr-soa {snapshot.serial}->{response.high_serial} at {ts}"
    new_soa = _record_from_wire(closing, tag)
    if old_soa is not None:
        if old_soa.identity != new_soa.identity:
            working.remove(old_soa)
            if not working.add(new_soa):
                folded += 1
    elif not working.add(new_soa):
        folded += 1

    final_soa = working.soa_record()
    if final_soa is None:
        raise transfer.FenceError("最终快照缺少顶点 SOA")
    if final_soa.rdata_wire != closing.rdata.to_wire(None):
        raise transfer.FenceError("最终 SOA 与收尾围栏 RDATA 核对失败")
    if working.soa_serial() != response.high_serial:
        raise transfer.FenceError("最终 SOA serial 与差量链尾不一致")
    working.serial = response.high_serial
    return "ixfr", working, folded
