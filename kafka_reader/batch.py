"""RecordBatch v2 批头解析、CRC/压缩校验与片段驱动循环。"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass, field

from .codec import BufferReader, crc32c
from .errors import (
    BatchCorrupt,
    CodecError,
    CompressionError,
    CrcMismatch,
    TruncatedBatch,
)
from .records import Record, parse_records

# 批头固定布局（大端）：
#   baseOffset        int64   @0
#   batchLength       int32   @8   （从 partitionLeaderEpoch 开始计数）
#   partitionLeaderEpoch int32 @12
#   magic             int8    @16
#   crc               uint32  @17
#   attributes        uint16  @21
#   lastOffsetDelta   int32   @23
#   baseTimestamp     int64   @27
#   maxTimestamp      int64   @35
#   producerId        int64   @43
#   producerEpoch     int16   @51
#   baseSequence      int32   @53
#   recordCount       int32   @57
HEADER_SIZE = 61
LOG_OVERHEAD = 12          # baseOffset + batchLength
MIN_BATCH_LENGTH = HEADER_SIZE - LOG_OVERHEAD  # 49
MAGIC_VALUE = 2

ATTR_COMPRESSION_MASK = 0x0007
ATTR_TIMESTAMP_TYPE = 0x0008
ATTR_TRANSACTIONAL = 0x0010
ATTR_CONTROL = 0x0020

COMPRESSION_NONE = 0
COMPRESSION_GZIP = 1

# 控制记录：key = int16 version + int16 type，value = int16 version + int32 coordinatorEpoch
CONTROL_KEY_ABORT = 0
CONTROL_KEY_COMMIT = 1
NO_PRODUCER_ID = -1
NO_PRODUCER_EPOCH = -1


@dataclass(frozen=True)
class ControlMarker:
    """控制批中唯一一条控制记录的解析结果。"""

    key_version: int
    marker_type: int          # 0=ABORT, 1=COMMIT
    value_version: int
    coordinator_epoch: int

    @property
    def is_commit(self) -> bool:
        return self.marker_type == CONTROL_KEY_COMMIT

    @property
    def is_abort(self) -> bool:
        return self.marker_type == CONTROL_KEY_ABORT


@dataclass
class Batch:
    """一个完整 RecordBatch 的解析结果。"""

    index: int
    file_offset: int
    total_size: int
    base_offset: int
    batch_length: int
    leader_epoch: int
    magic: int
    crc: int
    attributes: int
    last_offset_delta: int
    base_timestamp: int
    max_timestamp: int
    producer_id: int
    producer_epoch: int
    base_sequence: int
    record_count: int
    records: list[Record] = field(default_factory=list)
    layout: str = "standard"
    compression: int = COMPRESSION_NONE
    control_marker: ControlMarker | None = None
    offset_delta_warning: str | None = None
    _delta_chain_broken: bool = False

    @property
    def is_control(self) -> bool:
        return bool(self.attributes & ATTR_CONTROL)

    @property
    def is_transactional(self) -> bool:
        return bool(self.attributes & ATTR_TRANSACTIONAL)

    @property
    def last_offset(self) -> int:
        return self.base_offset + self.last_offset_delta

    def absolute_offset(self, record: Record, record_index: int = 0) -> int:
        """恢复 record 绝对 offset。

        标准布局严格按 ``baseOffset + offsetDelta``。变体材料的 delta
        链可能不连续/重复，此时退回批内顺序编号（Kafka 批内 offset 必然
        从 baseOffset 连续递增），并由 :attr:`offset_delta_warning` 暴露。
        """
        if self.layout == "variant" and self._delta_chain_broken:
            return self.base_offset + record_index
        return self.base_offset + record.offset_delta

    def absolute_timestamp(self, record: Record) -> int:
        return self.base_timestamp + record.timestamp_delta

    def record_sequence(self, record_index: int) -> int | None:
        """恢复 record 序列号；控制批 baseSequence=-1 时返回 None。"""
        if self.base_sequence < 0:
            return None
        return self.base_sequence + record_index

    def to_header_dict(self) -> dict:
        return {
            "index": self.index,
            "file_offset": self.file_offset,
            "total_size": self.total_size,
            "base_offset": self.base_offset,
            "last_offset": self.last_offset,
            "batch_length": self.batch_length,
            "leader_epoch": self.leader_epoch,
            "magic": self.magic,
            "crc": f"0x{self.crc:08x}",
            "attributes": f"0x{self.attributes:04x}",
            "compression": ("gzip" if self.compression == COMPRESSION_GZIP else "none"),
            "transactional": self.is_transactional,
            "control": self.is_control,
            "producer_id": self.producer_id,
            "producer_epoch": self.producer_epoch,
            "base_sequence": self.base_sequence,
            "record_count": self.record_count,
            "record_layout": self.layout,
        }


def _parse_control_marker(record: Record) -> ControlMarker:
    if record.key is None or len(record.key) != 4:
        raise CodecError("控制记录 key 必须是 4 字节（version:int16 + type:int16）")
    if record.value is None or len(record.value) != 6:
        raise CodecError(
            "控制记录 value 必须是 6 字节（version:int16 + coordinatorEpoch:int32）"
        )
    key_version, marker_type = struct.unpack_from(">hh", record.key, 0)
    value_version, coordinator_epoch = struct.unpack_from(">hi", record.value, 0)
    if key_version != 0 or value_version != 0:
        raise CodecError(
            f"控制记录版本非 0: keyVersion={key_version} valueVersion={value_version}"
        )
    if marker_type not in (CONTROL_KEY_ABORT, CONTROL_KEY_COMMIT):
        raise CodecError(f"未知控制记录类型: {marker_type}")
    return ControlMarker(
        key_version=key_version,
        marker_type=marker_type,
        value_version=value_version,
        coordinator_epoch=coordinator_epoch,
    )


def parse_batch(data: bytes, file_offset: int, index: int) -> Batch:
    """解析位于 ``data[file_offset:]`` 的一个批。"""
    readable = len(data) - file_offset

    if readable < LOG_OVERHEAD:
        raise TruncatedBatch(
            f"文件偏移 {file_offset} 处只剩 {max(readable, 0)} 字节，"
            "连 baseOffset/batchLength（12 字节）都不完整"
        )

    reader = BufferReader(data, file_offset)
    base_offset = reader.read_i64()
    batch_length = reader.read_u32()

    if batch_length < MIN_BATCH_LENGTH:
        raise BatchCorrupt(
            f"batchLength={batch_length} 小于最小合法值 {MIN_BATCH_LENGTH}",
            kind="length",
        )

    total_size = LOG_OVERHEAD + batch_length
    if file_offset + total_size > len(data):
        raise TruncatedBatch(
            f"批声明总长度 {total_size} 字节，但文件偏移 {file_offset} 之后"
            f"只剩 {len(data) - file_offset} 字节"
        )

    batch_end = file_offset + total_size
    leader_epoch = reader.read_i32()
    magic = reader.read_u8()
    if magic != MAGIC_VALUE:
        raise BatchCorrupt(
            f"仅支持 magic=2，实际 magic={magic}", kind="magic"
        )

    stored_crc = reader.read_u32()
    crc_region_start = reader.pos  # attributes 起点
    attributes = reader.read_u16()
    last_offset_delta = reader.read_i32()
    base_timestamp = reader.read_i64()
    max_timestamp = reader.read_i64()
    producer_id = reader.read_i64()
    producer_epoch = reader.read_i16()
    base_sequence = reader.read_i32()
    record_count = reader.read_i32()

    # 批头固定 61 字节，读完 recordCount 后必须正好落在 records 区间起点。
    if reader.pos != file_offset + HEADER_SIZE:
        raise BatchCorrupt("批头尺寸计算异常", kind="length")

    # CRC32C 覆盖 attributes（偏移21）到批末尾；先验 CRC 再解压。
    calculated_crc = crc32c(data[crc_region_start:batch_end])
    if calculated_crc != stored_crc:
        raise CrcMismatch(
            f"CRC32C 校验失败: 批内存储 0x{stored_crc:08x}，"
            f"按 attributes..批尾 {batch_end - crc_region_start} 字节计算得 "
            f"0x{calculated_crc:08x}"
        )

    if record_count < 0:
        raise BatchCorrupt(f"recordCount 不能为负数: {record_count}", kind="record_count")

    compression = attributes & ATTR_COMPRESSION_MASK
    if compression not in (COMPRESSION_NONE, COMPRESSION_GZIP):
        raise CompressionError(
            f"仅支持 none(0)/gzip(1) 压缩，attributes 低 3 位为 {compression}"
        )

    records_region = data[file_offset + HEADER_SIZE : batch_end]
    if compression == COMPRESSION_GZIP:
        try:
            raw_records = gzip.decompress(records_region)
        except (OSError, EOFError, zlib.error) as exc:
            raise CompressionError(f"gzip 解压失败: {exc}") from exc
    else:
        raw_records = records_region

    try:
        records, layout = parse_records(
            raw_records,
            expected_count=record_count,
            last_offset_delta=last_offset_delta,
        )
    except CodecError as exc:
        # record 体/长度/数量错误统一归类为 record_count 类批损坏
        raise BatchCorrupt(exc.message, kind="record_count") from exc

    batch = Batch(
        index=index,
        file_offset=file_offset,
        total_size=total_size,
        base_offset=base_offset,
        batch_length=batch_length,
        leader_epoch=leader_epoch,
        magic=magic,
        crc=stored_crc,
        attributes=attributes,
        last_offset_delta=last_offset_delta,
        base_timestamp=base_timestamp,
        max_timestamp=max_timestamp,
        producer_id=producer_id,
        producer_epoch=producer_epoch,
        base_sequence=base_sequence,
        record_count=record_count,
        records=records,
        layout=layout,
        compression=compression,
    )

    # 校验 offset delta 链：标准 Kafka 批内 delta 必须从 0 严格递增到
    # lastOffsetDelta。变体材料不满足时退回顺序编号恢复绝对 offset。
    deltas = [record.offset_delta for record in records]
    expected = list(range(record_count))
    chain_ok = deltas == expected and last_offset_delta == (
        record_count - 1
    )
    if not chain_ok:
        if layout == "standard":
            raise BatchCorrupt(
                f"标准布局 offset delta 链非法: deltas={deltas}，"
                f"lastOffsetDelta={last_offset_delta}，recordCount={record_count}",
                kind="record_count",
            )
        batch._delta_chain_broken = True
        batch.offset_delta_warning = (
            f"变体批 offsetDelta 链 {deltas} 与 lastOffsetDelta="
            f"{last_offset_delta} 不自洽，绝对 offset 已按批内顺序编号恢复"
        )

    if batch.is_control:
        if len(records) != 1:
            raise BatchCorrupt(
                f"控制批必须且只能包含 1 条控制记录，实际 {len(records)} 条",
                kind="record_count",
            )
        try:
            batch.control_marker = _parse_control_marker(records[0])
        except CodecError as exc:
            raise BatchCorrupt(exc.message, kind="control_record") from exc

    return batch


@dataclass
class FragmentResult:
    """片段解析结果：成功批 + 可选致命错误（保留已确认前缀）。"""

    batches: list[Batch]
    error: BatchCorrupt | None
    bytes_consumed: int
    total_bytes: int

    @property
    def ok(self) -> bool:
        return self.error is None and self.bytes_consumed == self.total_bytes


def parse_fragment(data: bytes) -> FragmentResult:
    """顺序解析一个分区片段，遇到致命批错误时停止并保留前缀。"""
    batches: list[Batch] = []
    offset = 0
    index = 0
    while offset < len(data):
        try:
            batch = parse_batch(data, offset, index)
        except BatchCorrupt as exc:
            if exc.batch_index is None:
                exc = exc.with_location(index, offset)
            return FragmentResult(
                batches=batches,
                error=exc,
                bytes_consumed=offset,
                total_bytes=len(data),
            )
        batches.append(batch)
        offset += batch.total_size
        index += 1

    return FragmentResult(
        batches=batches,
        error=None,
        bytes_consumed=offset,
        total_bytes=len(data),
    )
