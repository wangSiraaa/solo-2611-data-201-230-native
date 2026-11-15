"""底层原语测试：CRC32C 向量、zigzag varint/varlong、nullable bytes。"""

import unittest

from kafka_reader.codec import (
    BufferReader,
    crc32c,
    encode_signed_varint,
    encode_unsigned_varint,
    zigzag_decode,
    zigzag_encode,
)
from kafka_reader.errors import CodecError


class Crc32CTest(unittest.TestCase):
    def test_known_vector(self):
        # CRC32C/ Castagnoli 标准校验向量
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)

    def test_empty(self):
        self.assertEqual(crc32c(b""), 0x00000000)

    def test_deterministic(self):
        data = b"kafka-record-batch-v2"
        self.assertEqual(crc32c(data), crc32c(data))
        self.assertNotEqual(crc32c(data), crc32c(data + b"x"))


class ZigzagTest(unittest.TestCase):
    CASES = [0, 1, -1, 2, -2, 63, -64, 64, -65, 2**30, -(2**30)]

    def test_roundtrip(self):
        for value in self.CASES:
            encoded = encode_signed_varint(value)
            decoded = BufferReader(encoded).read_signed_varint()
            self.assertEqual(decoded, value, value)

    def test_known_encoding(self):
        # Kafka ByteUtils 约定
        self.assertEqual(encode_unsigned_varint(0), b"\x00")
        self.assertEqual(encode_signed_varint(0), b"\x00")
        self.assertEqual(encode_signed_varint(-1), b"\x01")
        self.assertEqual(encode_signed_varint(1), b"\x02")
        self.assertEqual(encode_signed_varint(15), b"\x1e")  # 材料中的正长度

    def test_zigzag_math(self):
        self.assertEqual(zigzag_encode(0), 0)
        self.assertEqual(zigzag_encode(-1), 1)
        self.assertEqual(zigzag_decode(1), -1)
        self.assertEqual(zigzag_decode(2), 1)
        for n in (-1000, -1, 0, 1, 1000):
            self.assertEqual(zigzag_decode(zigzag_encode(n)), n)


class BufferReaderTest(unittest.TestCase):
    def test_nullable_null_vs_empty(self):
        # -1 -> null；0 -> 空字节，二者必须区分
        reader = BufferReader(encode_signed_varint(-1) + encode_signed_varint(0))
        self.assertIsNone(reader.read_nullable_bytes_varint())
        self.assertEqual(reader.read_nullable_bytes_varint(), b"")

    def test_nullable_value(self):
        payload = b"hello"
        reader = BufferReader(encode_signed_varint(len(payload)) + payload)
        self.assertEqual(reader.read_nullable_bytes_varint(), payload)

    def test_truncated_varint(self):
        with self.assertRaises(CodecError):
            BufferReader(b"\xff\xff\xff\xff\xff").read_unsigned_varint()

    def test_read_past_end(self):
        with self.assertRaises(CodecError):
            BufferReader(b"\x01").read(2)

    def test_bounded_region(self):
        reader = BufferReader(b"\x00\x01\x02", start=1, end=2)
        self.assertEqual(reader.remaining(), 1)
        self.assertEqual(reader.read_u8(), 1)
        with self.assertRaises(CodecError):
            reader.read_u8()


if __name__ == "__main__":
    unittest.main()
