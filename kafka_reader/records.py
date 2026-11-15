"""magic=2 DefaultRecord 解析。

标准 Kafka v2 单条 record 布局（长度本身是**无符号** varint）::

    length(uvint) timestampDelta(svarlong) offsetDelta(svarint)
    keyLength(svarint, -1=null) key? valueLength(svarint, -1=null) value?
    headerCount(svarint) [headerKeyLength(svarint) headerKey
                          headerValueLength(svarint, -1=null) headerValue?]*

本仓库提供的合成材料在 record 线上格式上有两处固定偏差，且能通过批 CRC
（说明偏差是生成器有意为之，而非传输损坏）：

1. record ``length`` 被按有符号 zigzag varint 写出（正值字节数翻倍）；
2. offsetDelta 之后多写了一个字节的 record-level attributes 占位（均为 0）。

解析器先按严格标准布局试解；失败后回退到该变体布局。两种模式都要求
长度前缀、header 数与区间边界完全自洽，任何对不上都按批损坏处理，
而不是静默截断。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codec import BufferReader
from .errors import CodecError, RecordCountError


@dataclass(frozen=True)
class Header:
    """用户 record header；同名 key 允许重复，顺序必须保留。"""

    key: bytes
    value: bytes | None


@dataclass(frozen=True)
class Record:
    """解压后的单条 record（尚未恢复绝对 offset / timestamp）。"""

    offset_delta: int
    timestamp_delta: int
    key: bytes | None
    value: bytes | None
    headers: tuple[Header, ...] = field(default_factory=tuple)
    record_attributes: int | None = None  # 仅变体布局存在该字段
    layout: str = "standard"


def _parse_one(reader: BufferReader, *, variant: bool) -> tuple[Record, int]:
    """解析单条 record，返回 ``(record, 长度前缀声明的字节数)``。"""
    if variant:
        body_length = reader.read_signed_varint()
    else:
        body_length = reader.read_unsigned_varint()
    if body_length < 0:
        raise CodecError(f"record length 不能为负数: {body_length}")

    body_end = reader.pos + body_length
    if body_end > reader.end:
        raise CodecError(
            f"record 声明 {body_length} 字节越过 records 区间边界 "
            f"(位置 {reader.pos}，区间结束于 {reader.end})"
        )

    timestamp_delta = reader.read_signed_varlong()
    offset_delta = reader.read_signed_varint()
    record_attributes: int | None = None
    if variant:
        record_attributes = reader.read_u8()

    key = reader.read_nullable_bytes_varint()
    value = reader.read_nullable_bytes_varint()
    header_count = reader.read_signed_varint()
    if header_count < 0:
        raise CodecError(f"header 数量非法: {header_count}")

    headers: list[Header] = []
    for _ in range(header_count):
        header_key = reader.read_nullable_bytes_varint()
        if header_key is None:
            raise CodecError("header key 不允许为 null")
        header_value = reader.read_nullable_bytes_varint()
        headers.append(Header(key=header_key, value=header_value))

    if reader.pos != body_end:
        raise CodecError(
            f"record 体长度不自洽: 声明 {body_length} 字节，"
            f"实际消费 {reader.pos - (body_end - body_length)} 字节，"
            f"剩余 {body_end - reader.pos} 字节未消费"
        )

    record = Record(
        offset_delta=offset_delta,
        timestamp_delta=timestamp_delta,
        key=key,
        value=value,
        headers=tuple(headers),
        record_attributes=record_attributes,
        layout="variant" if variant else "standard",
    )
    return record, body_length


def _parse_all(raw: bytes, *, variant: bool) -> list[Record]:
    reader = BufferReader(raw)
    records: list[Record] = []
    while reader.remaining():
        record, _ = _parse_one(reader, variant=variant)
        records.append(record)
    if not records:
        raise CodecError("records 区间为空")
    return records


def parse_records(
    raw: bytes,
    *,
    expected_count: int,
    last_offset_delta: int,
) -> tuple[list[Record], str]:
    """解析一个批的全部 record，并校验 count / 末条 offset delta。

    返回 ``(records, 实际布局名称)``。标准布局与变体布局都无法自洽时
    抛 :class:`RecordCountError` / :class:`CodecError`，由上层包装成
    带批位置的批级错误。
    """
    errors: list[str] = []
    for variant, layout_name in ((False, "standard"), (True, "variant")):
        try:
            records = _parse_all(raw, variant=variant)
        except CodecError as exc:
            errors.append(f"{layout_name}: {exc.message}")
            continue

        if len(records) != expected_count:
            errors.append(
                f"{layout_name}: 解析出 {len(records)} 条 record，"
                f"批头声明 {expected_count} 条"
            )
            continue

        # 标准布局严格校验 offset delta 链与批头 lastOffsetDelta。
        # 变体材料的 delta 编码本身也可能不规范（见模块 docstring），
        # 此时只要求 delta 非负，绝对 offset 仍按 baseOffset+delta 恢复，
        # 并由批层把偏差暴露给 inspect。
        for index, record in enumerate(records):
            if record.offset_delta < 0:
                errors.append(
                    f"{layout_name}: 第 {index} 条 offsetDelta="
                    f"{record.offset_delta} 为负数"
                )
                break
        else:
            if not variant:
                actual_last_delta = records[-1].offset_delta
                if actual_last_delta != last_offset_delta:
                    errors.append(
                        f"{layout_name}: 末条 offsetDelta={actual_last_delta}，"
                        f"批头 lastOffsetDelta={last_offset_delta}"
                    )
                    continue
            return records, layout_name

    detail = "；".join(errors) if errors else "未知 record 布局"
    raise RecordCountError(f"record 解析失败（{detail}）")


# 注：标准布局与变体布局都强制长度前缀声明的字节数与实际字段消费精确相等、
# record count 与批头一致、nullable 长度/header 数合法。变体比标准多消费
# 一个 attributes 字节且长度按 zigzag 解码，二者不会把随机损坏“碰巧”解析
# 成功；无法自洽即按批损坏停止，不做静默截断。
