"""事务状态机与 read_committed 可见性分类。

规则（题目给定前提：片段从所有事务开始之前切片；同一 producer id/epoch
同一时刻最多一笔在途事务）：

* 非事务批（无 transactional 标志）的 record 独立处理；
* transactional 数据批按 ``(producerId, producerEpoch)`` 归入一笔事务；
* 同一 producer epoch 的控制批 COMMIT/ABORT 结束该事务：
  COMMIT -> 事务内消息已提交可见；ABORT -> 全部排除；
* 旧 epoch 的控制标记不能结束新 epoch 的事务（按 epoch 严格配对）；
* read_committed 只输出 ``offset < last_stable_offset`` 且
  已提交或非事务的消息。LSO 及之后的内容（哪怕是完整的非事务消息）
  一律作为 LSO 尾部保留但不导出。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from .batch import (
    NO_PRODUCER_EPOCH,
    NO_PRODUCER_ID,
    Batch,
)

# 不可导出原因（英文 reason code，供机器处理；CLI 再渲染中文说明）
REASON_COMMITTED = "committed"
REASON_NON_TRANSACTIONAL = "non_transactional"
REASON_ABORTED = "aborted_transaction"
REASON_PENDING = "pending_transaction"
REASON_CONTROL = "control_record"
REASON_LSO_TAIL = "lso_tail"
REASON_DANGLING_MARKER = "dangling_control_marker"

REASON_TEXT = {
    REASON_COMMITTED: "已提交事务消息",
    REASON_NON_TRANSACTIONAL: "非事务消息",
    REASON_ABORTED: "事务已中止，排除",
    REASON_PENDING: "事务未决（无 COMMIT/ABORT 标记），排除",
    REASON_CONTROL: "控制记录，不作为用户消息",
    REASON_LSO_TAIL: "offset 达到或超过 LSO，read_committed 不可读",
    REASON_DANGLING_MARKER: "控制标记找不到同 producer epoch 的在途事务",
}


@dataclass
class Metadata:
    topic: str
    partition: int
    high_watermark: int
    last_stable_offset: int

    @property
    def lso(self) -> int:
        return self.last_stable_offset


def load_metadata(path: str | Path) -> Metadata:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return Metadata(
            topic=str(payload["topic"]),
            partition=int(payload["partition"]),
            high_watermark=int(payload["high_watermark"]),
            last_stable_offset=int(payload["last_stable_offset"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"metadata.json 字段缺失或类型错误: {exc}") from exc


@dataclass
class Entry:
    """一条记录的分类结果（保留原始 partition / offset，不重新编号）。"""

    topic: str
    partition: int
    offset: int
    timestamp: int
    key: bytes | None
    value: bytes | None
    headers: tuple  # tuple[Header]
    producer_id: int | None
    producer_epoch: int | None
    sequence: int | None
    batch_index: int
    record_index: int
    is_control: bool
    control_type: str | None
    reason: str
    visible: bool
    beyond_lso: bool = False

    def to_export_dict(self) -> dict:
        return {
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "timestamp": self.timestamp,
            # null -> JSON null；空字节 -> ""，二者严格区分
            "key": _bytes_to_json(self.key),
            "value": _bytes_to_json(self.value),
            "headers": [
                {
                    "key": _bytes_to_json(header.key),
                    "value": _bytes_to_json(header.value),
                }
                for header in self.headers
            ],
            "producer_id": self.producer_id,
            "producer_epoch": self.producer_epoch,
            "sequence": self.sequence,
        }


def _bytes_to_json(value: bytes | None):
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": _b64encode(value)}


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@dataclass
class TransactionSummary:
    producer_id: int
    producer_epoch: int
    state: str  # pending | committed | aborted
    start_offset: int
    end_offset: int | None
    marker_offset: int | None = None


@dataclass
class AnalysisResult:
    metadata: Metadata
    entries: list[Entry] = field(default_factory=list)
    transactions: list[TransactionSummary] = field(default_factory=list)
    dangling_markers: list[Entry] = field(default_factory=list)

    @property
    def visible_entries(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.visible]

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return counts


def _txn_key(batch: Batch) -> tuple[int, int]:
    return batch.producer_id, batch.producer_epoch


def analyze(batches: list[Batch], metadata: Metadata) -> AnalysisResult:
    result = AnalysisResult(metadata=metadata)

    # (pid, epoch) -> 事务摘要；同一 key 同时最多一笔在途事务
    open_txns: dict[tuple[int, int], TransactionSummary] = {}
    finished_txns: dict[tuple[int, int], TransactionSummary] = {}
    dangling_offsets: set[int] = set()

    # ---- 第一遍：建立事务生命周期 ----------------------------------------
    for batch in batches:
        key = _txn_key(batch)

        if batch.is_control:
            marker = batch.control_marker
            assert marker is not None  # 解析阶段已保证
            txn = open_txns.get(key)
            if txn is None:
                # 旧 epoch 标记 / 没有对应在途事务：登记为悬空标记，
                # 绝不允许它释放别的（尤其新 epoch）数据。
                dangling_offsets.add(batch.base_offset)
                result.dangling_markers.append(
                    _make_control_entry(batch, 0, metadata, REASON_DANGLING_MARKER)
                )
                continue

            if marker.is_commit:
                txn.state = "committed"
            elif marker.is_abort:
                txn.state = "aborted"
            txn.marker_offset = batch.base_offset
            txn.end_offset = batch.last_offset
            open_txns.pop(key)
            finished_txns[key] = txn
            result.transactions.append(txn)
            continue

        if batch.is_transactional:
            txn = open_txns.get(key)
            if txn is None:
                txn = TransactionSummary(
                    producer_id=batch.producer_id,
                    producer_epoch=batch.producer_epoch,
                    state="pending",
                    start_offset=batch.base_offset,
                    end_offset=batch.last_offset,
                )
                open_txns[key] = txn
            else:
                txn.end_offset = batch.last_offset
        # 非事务批不进入事务表

    # 片段结束仍未关闭的事务保持 pending
    for txn in open_txns.values():
        result.transactions.append(txn)

    # ---- 第二遍：逐条分类 -------------------------------------------------
    for batch in batches:
        for record_index, record in enumerate(batch.records):
            offset = batch.absolute_offset(record, record_index)
            timestamp = batch.absolute_timestamp(record)
            sequence = batch.record_sequence(record_index)

            pid = batch.producer_id if batch.producer_id != NO_PRODUCER_ID else None
            epoch = (
                batch.producer_epoch
                if batch.producer_epoch != NO_PRODUCER_EPOCH
                else None
            )

            if batch.is_control:
                marker = batch.control_marker
                control_type = (
                    "COMMIT" if marker and marker.is_commit else "ABORT"
                )
                reason = REASON_CONTROL
                if offset in dangling_offsets:
                    reason = REASON_DANGLING_MARKER
                entry = Entry(
                    topic=metadata.topic,
                    partition=metadata.partition,
                    offset=offset,
                    timestamp=timestamp,
                    key=record.key,
                    value=record.value,
                    headers=tuple(record.headers),
                    producer_id=pid,
                    producer_epoch=epoch,
                    sequence=sequence,
                    batch_index=batch.index,
                    record_index=record_index,
                    is_control=True,
                    control_type=control_type,
                    reason=reason,
                    visible=False,
                )
            else:
                if batch.is_transactional:
                    txn_key = _txn_key(batch)
                    txn = finished_txns.get(txn_key) or open_txns.get(txn_key)
                    if txn is not None and txn.state == "committed":
                        reason = REASON_COMMITTED
                    elif txn is not None and txn.state == "aborted":
                        reason = REASON_ABORTED
                    else:
                        reason = REASON_PENDING
                else:
                    reason = REASON_NON_TRANSACTIONAL

                entry = Entry(
                    topic=metadata.topic,
                    partition=metadata.partition,
                    offset=offset,
                    timestamp=timestamp,
                    key=record.key,
                    value=record.value,
                    headers=tuple(record.headers),
                    producer_id=pid,
                    producer_epoch=epoch,
                    sequence=sequence,
                    batch_index=batch.index,
                    record_index=record_index,
                    is_control=False,
                    control_type=None,
                    reason=reason,
                    visible=False,
                )

            # LSO 闸门：只有 offset < LSO 才可能对 read_committed 可见。
            # 未决/中止/控制等排除原因优先保留；LSO 同时记录为独立事实，
            # 报告里能同时看出“未决事务”与“位于 LSO 尾部”两层原因。
            entry.beyond_lso = offset >= metadata.lso
            if entry.beyond_lso and entry.reason in (
                REASON_COMMITTED,
                REASON_NON_TRANSACTIONAL,
            ):
                entry.reason = REASON_LSO_TAIL
            entry.visible = (
                not entry.beyond_lso
                and entry.reason
                in (REASON_COMMITTED, REASON_NON_TRANSACTIONAL)
            )

            result.entries.append(entry)

    return result


def _make_control_entry(
    batch: Batch,
    record_index: int,
    metadata: Metadata,
    reason: str,
) -> Entry:
    record = batch.records[record_index]
    marker = batch.control_marker
    return Entry(
        topic=metadata.topic,
        partition=metadata.partition,
        offset=batch.absolute_offset(record, record_index),
        timestamp=batch.absolute_timestamp(record),
        key=record.key,
        value=record.value,
        headers=tuple(record.headers),
        producer_id=batch.producer_id,
        producer_epoch=batch.producer_epoch,
        sequence=batch.record_sequence(record_index),
        batch_index=batch.index,
        record_index=record_index,
        is_control=True,
        control_type="COMMIT" if marker and marker.is_commit else "ABORT",
        reason=reason,
        visible=False,
    )
