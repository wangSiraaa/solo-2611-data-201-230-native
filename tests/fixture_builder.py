"""测试专用：按 Kafka RecordBatch v2 规则重新编码批。

提供两种线上布局：

* ``standard``：严格 Kafka DefaultRecord（length 为无符号 varint，
  无额外 attributes 字节），用于验证读取器的标准布局路径；
* ``variant``：复刻本仓库 fixtures 生成器的两处偏差（length 走 zigzag、
  offsetDelta 后多一个 attributes 字节），用于回归给定材料。

该模块只服务测试，不属于读取器核心，不调用任何 Kafka 工具。
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass

from kafka_reader.codec import (
    crc32c,
    encode_signed_varint,
    encode_unsigned_varint,
)
from kafka_reader.batch import (
    ATTR_CONTROL,
    ATTR_TRANSACTIONAL,
    COMPRESSION_GZIP,
    COMPRESSION_NONE,
    HEADER_SIZE,
    LOG_OVERHEAD,
    MAGIC_VALUE,
    NO_PRODUCER_EPOCH,
    NO_PRODUCER_ID,
)


@dataclass
class Rec:
    key: bytes | None
    value: bytes | None
    headers: tuple  # tuple[tuple[bytes, bytes | None], ...]
    offset_delta: int = 0
    timestamp_delta: int = 0
    record_attributes: int = 0


def _nullable(value: bytes | None) -> bytes:
    if value is None:
        return encode_signed_varint(-1)
    return encode_signed_varint(len(value)) + value


def encode_record(record: Rec, *, variant: bool) -> bytes:
    body = bytearray()
    body += encode_signed_varint(record.timestamp_delta)
    body += encode_signed_varint(record.offset_delta)
    if variant:
        body += struct.pack(">B", record.record_attributes)
    body += _nullable(record.key)
    body += _nullable(record.value)
    body += encode_signed_varint(len(record.headers))
    for header_key, header_value in record.headers:
        body += encode_signed_varint(len(header_key)) + header_key
        body += _nullable(header_value)

    if variant:
        length_prefix = encode_signed_varint(len(body))
    else:
        length_prefix = encode_unsigned_varint(len(body))
    return length_prefix + bytes(body)


def control_record(marker_type: int, coordinator_epoch: int) -> Rec:
    # key: int16 version(0) + int16 type；value: int16 version(0) + int32 epoch
    key = struct.pack(">hh", 0, marker_type)
    value = struct.pack(">hi", 0, coordinator_epoch)
    return Rec(key=key, value=value, headers=())


def build_batch(
    *,
    base_offset: int,
    records: list[Rec],
    base_timestamp: int,
    producer_id: int = NO_PRODUCER_ID,
    producer_epoch: int = NO_PRODUCER_EPOCH,
    base_sequence: int = 0,
    leader_epoch: int = 0,
    transactional: bool = False,
    control: bool = False,
    compression: int = COMPRESSION_NONE,
    variant: bool = False,
    last_offset_delta: int | None = None,
    max_timestamp: int | None = None,
) -> bytes:
    attributes = compression
    if transactional:
        attributes |= ATTR_TRANSACTIONAL
    if control:
        attributes |= ATTR_CONTROL

    raw = b"".join(encode_record(r, variant=variant) for r in records)
    if compression == COMPRESSION_GZIP:
        payload = gzip.compress(raw, mtime=0)
    else:
        payload = raw

    if last_offset_delta is None:
        last_offset_delta = records[-1].offset_delta if records else 0
    if max_timestamp is None:
        max_timestamp = base_timestamp + (
            max((r.timestamp_delta for r in records), default=0)
        )
    record_count = len(records)

    # batchLength 从 partitionLeaderEpoch 起算：总长度 - 12
    batch_length = (HEADER_SIZE - LOG_OVERHEAD) + len(payload)

    header = bytearray()
    header += struct.pack(">q", base_offset)
    header += struct.pack(">I", batch_length)
    header += struct.pack(">i", leader_epoch)
    header += struct.pack(">B", MAGIC_VALUE)
    crc_pos = len(header)
    header += b"\x00\x00\x00\x00"  # CRC 占位
    header += struct.pack(">H", attributes)
    header += struct.pack(">i", last_offset_delta)
    header += struct.pack(">q", base_timestamp)
    header += struct.pack(">q", max_timestamp)
    header += struct.pack(">q", producer_id)
    header += struct.pack(">h", producer_epoch)
    header += struct.pack(">i", base_sequence)
    header += struct.pack(">i", record_count)
    assert len(header) == HEADER_SIZE

    batch = bytes(header) + payload
    # CRC 覆盖 attributes（header 偏移 21）到批末尾
    covered = batch[21:]
    crc = crc32c(covered)
    batch = batch[:crc_pos] + struct.pack(">I", crc) + batch[crc_pos + 4 :]
    return batch
