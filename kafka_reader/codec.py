"""底层编解码原语：CRC32C、zigzag varint/varlong、有界字节读取器。"""

from __future__ import annotations

import struct

from .errors import CodecError

# ---------------------------------------------------------------------------
# CRC32C / Castagnoli（Kafka magic v2 使用的 CRC，不是 zlib.crc32）
# 优先使用可选的 google-crc32c 加速，没有则走纯 Python 表驱动实现。
# ---------------------------------------------------------------------------

try:  # pragma: no cover - 取决于运行环境是否安装扩展
    from google_crc32c import value as _google_crc32c  # type: ignore

    def crc32c(data: bytes) -> int:
        return int(_google_crc32c(data))

except ImportError:
    _CRC32C_POLY = 0x82F63B78  # Castagnoli 反射多项式

    def _build_table() -> tuple[int, ...]:
        table = []
        for i in range(256):
            crc = i
            for _ in range(8):
                crc = (crc >> 1) ^ _CRC32C_POLY if crc & 1 else crc >> 1
            table.append(crc)
        return tuple(table)

    _CRC32C_TABLE = _build_table()

    def crc32c(data: bytes) -> int:
        crc = 0xFFFFFFFF
        table = _CRC32C_TABLE
        for byte in data:
            crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
        return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# zigzag 有符号变长整数
# Kafka ByteUtils.writeVarint/writeVarlong 对有符号整数先做 zigzag，
# 再按无符号 varint 写入。
# ---------------------------------------------------------------------------

def zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def zigzag_encode(value: int) -> int:
    # 32/64 位在小整数上编码一致；编码器同时用于 varint 与 varlong。
    if value < 0:
        return ((-value) << 1) - 1
    return value << 1


class BufferReader:
    """在固定字节区间内顺序读取，越界统一抛 :class:`CodecError`。"""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, start: int = 0, end: int | None = None):
        self.data = data
        self.pos = start
        self.end = len(data) if end is None else end

    def remaining(self) -> int:
        return self.end - self.pos

    def require(self, n: int) -> None:
        if self.remaining() < n:
            raise CodecError(
                f"需要再读取 {n} 字节，剩余 {max(self.remaining(), 0)} 字节"
            )

    def read(self, n: int) -> bytes:
        self.require(n)
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def read_u8(self) -> int:
        self.require(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def read_i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def read_u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def read_i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def read_u32(self) -> int:
        return struct.unpack(">I", self.read(4))[0]

    def read_i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def read_unsigned_varint(self, max_bytes: int = 5) -> int:
        """读取无符号 varint；``max_bytes`` 防止损坏数据无限吞字节。"""
        result = 0
        shift = 0
        for _ in range(max_bytes):
            byte = self.read_u8()
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return result
            shift += 7
        raise CodecError(f"varint 超过 {max_bytes} 字节仍未终止")

    def read_signed_varint(self, max_bytes: int = 5) -> int:
        return zigzag_decode(self.read_unsigned_varint(max_bytes))

    def read_unsigned_varlong(self) -> int:
        return self.read_unsigned_varint(max_bytes=10)

    def read_signed_varlong(self) -> int:
        return zigzag_decode(self.read_unsigned_varlong())

    def read_nullable_bytes_varint(self) -> bytes | None:
        """Kafka ``CompactNullableBytes`` 风格、长度走有符号 zigzag varint。

        * 长度 -1 -> ``None``（null key/value/header value）
        * 长度  0 -> ``b""``（空字节，与 null 严格区分）
        """
        length = self.read_signed_varint()
        if length < -1:
            raise CodecError(f"nullable bytes 长度非法: {length}")
        if length == -1:
            return None
        return self.read(length)


def encode_unsigned_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("无符号 varint 不能为负数")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_signed_varint(value: int) -> bytes:
    return encode_unsigned_varint(zigzag_encode(value))
