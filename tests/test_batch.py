"""批解析、两种 record 布局、gzip、控制批与损坏检测测试。"""

import gzip
import struct
import unittest

from kafka_reader.batch import (
    ATTR_TRANSACTIONAL,
    COMPRESSION_GZIP,
    CONTROL_KEY_ABORT,
    CONTROL_KEY_COMMIT,
    HEADER_SIZE,
    parse_batch,
    parse_fragment,
)
from kafka_reader.codec import crc32c
from kafka_reader.errors import (
    CompressionError,
    CrcMismatch,
    RecordCountError,
    TruncatedBatch,
)

try:
    from .fixture_builder import Rec, build_batch, control_record
except ImportError:
    from fixture_builder import Rec, build_batch, control_record


class RecordLayoutTest(unittest.TestCase):
    def _sample_records(self):
        return [
            Rec(
                key=None,
                value=b"a",
                headers=((b"h", b"1"), (b"h", b"2")),  # 重复 header key
                offset_delta=0,
            ),
            Rec(key=b"", value=b"", headers=(), offset_delta=1),  # 空字节非 null
            Rec(key=b"k", value=None, headers=(), offset_delta=2),  # tombstone
        ]

    def test_standard_layout_roundtrip(self):
        data = build_batch(
            base_offset=10,
            records=self._sample_records(),
            base_timestamp=1000,
            variant=False,
        )
        batch = parse_batch(data, 0, 0)
        self.assertEqual(batch.layout, "standard")
        self.assertEqual(batch.record_count, 3)
        self.assertEqual([r.offset_delta for r in batch.records], [0, 1, 2])
        self.assertIsNone(batch.records[0].key)
        self.assertEqual(batch.records[1].key, b"")
        self.assertEqual(batch.records[1].value, b"")
        self.assertIsNone(batch.records[2].value)  # tombstone 保留
        # 重复 header key 与顺序
        self.assertEqual(
            [h.key for h in batch.records[0].headers], [b"h", b"h"]
        )
        self.assertEqual(
            [h.value for h in batch.records[0].headers], [b"1", b"2"]
        )
        # offset / timestamp 由 delta 恢复
        self.assertEqual(
            batch.absolute_offset(batch.records[2], 2), 12
        )
        self.assertEqual(
            batch.absolute_timestamp(batch.records[0]), 1000
        )

    def test_variant_layout_roundtrip(self):
        data = build_batch(
            base_offset=0,
            records=self._sample_records(),
            base_timestamp=500,
            variant=True,
        )
        batch = parse_batch(data, 0, 0)
        self.assertEqual(batch.layout, "variant")
        self.assertEqual(batch.records[0].record_attributes, 0)
        self.assertEqual(batch.records[2].value, None)
        self.assertEqual(batch.absolute_offset(batch.records[1], 1), 1)

    def test_gzip_standard_and_variant(self):
        for variant in (False, True):
            with self.subTest(variant=variant):
                data = build_batch(
                    base_offset=0,
                    records=[Rec(None, b"gz" * 50, ())],
                    base_timestamp=1,
                    compression=COMPRESSION_GZIP,
                    variant=variant,
                )
                batch = parse_batch(data, 0, 0)
                self.assertEqual(batch.compression, COMPRESSION_GZIP)
                self.assertEqual(batch.records[0].value, b"gz" * 50)

    def test_gzip_body_corruption_detected(self):
        data = bytearray(
            build_batch(
                base_offset=0,
                records=[Rec(None, b"x" * 20, ())],
                base_timestamp=1,
                compression=COMPRESSION_GZIP,
            )
        )
        # 破坏 gzip 载荷尾部并重算 CRC，使 CRC 通过而解压失败
        data[-1] ^= 0xFF
        crc = crc32c(bytes(data[21:]))
        data[17:21] = struct.pack(">I", crc)
        with self.assertRaises(CompressionError):
            parse_batch(bytes(data), 0, 0)


class ControlBatchTest(unittest.TestCase):
    def test_commit_and_abort_markers(self):
        for marker_type, commit in (
            (CONTROL_KEY_ABORT, False),
            (CONTROL_KEY_COMMIT, True),
        ):
            data = build_batch(
                base_offset=5,
                records=[control_record(marker_type, 7)],
                base_timestamp=1,
                transactional=True,
                control=True,
                producer_id=99,
                producer_epoch=3,
            )
            batch = parse_batch(data, 0, 0)
            self.assertTrue(batch.is_control)
            self.assertEqual(batch.records[0].key[:2], b"\x00\x00")
            self.assertEqual(batch.control_marker.is_commit, commit)
            self.assertEqual(batch.control_marker.coordinator_epoch, 7)

    def test_control_batch_with_two_records_rejected(self):
        data = build_batch(
            base_offset=0,
            records=[
                control_record(CONTROL_KEY_COMMIT, 1),
                control_record(CONTROL_KEY_ABORT, 1),
            ],
            base_timestamp=1,
            control=True,
            transactional=True,
            last_offset_delta=1,
        )
        with self.assertRaises(RecordCountError):
            parse_batch(data, 0, 0)


class CorruptionTest(unittest.TestCase):
    def _good_batch(self):
        return build_batch(
            base_offset=0,
            records=[Rec(None, b"hello", ())],
            base_timestamp=1,
        )

    def test_crc_mismatch(self):
        data = bytearray(self._good_batch())
        data[30] ^= 0x01  # 改 CRC 覆盖区内字节但不动 CRC 字段
        with self.assertRaises(CrcMismatch):
            parse_batch(bytes(data), 0, 0)

    def test_truncated_header(self):
        data = self._good_batch()
        with self.assertRaises(TruncatedBatch):
            parse_batch(data[:8], 0, 0)

    def test_truncated_body(self):
        data = self._good_batch()
        with self.assertRaises(TruncatedBatch):
            parse_batch(data[:-4], 0, 0)

    def test_declared_length_too_small(self):
        data = bytearray(self._good_batch())
        struct.pack_into(">I", data, 8, 10)  # 小于最小 batchLength
        from kafka_reader.errors import BatchCorrupt

        with self.assertRaises(BatchCorrupt):
            parse_batch(bytes(data), 0, 0)

    def test_record_count_mismatch(self):
        data = bytearray(self._good_batch())
        # 把 recordCount 改成 9，CRC 重算，触发 count 校验
        struct.pack_into(">i", data, 57, 9)
        crc = crc32c(bytes(data[21:]))
        struct.pack_into(">I", data, 17, crc)
        with self.assertRaises(RecordCountError):
            parse_batch(bytes(data), 0, 0)

    def test_unsupported_compression(self):
        data = bytearray(self._good_batch())
        attrs = struct.unpack_from(">H", data, 21)[0]
        struct.pack_into(">H", data, 21, attrs | 0x0002)  # snappy=2
        crc = crc32c(bytes(data[21:]))
        struct.pack_into(">I", data, 17, crc)
        with self.assertRaises(CompressionError):
            parse_batch(bytes(data), 0, 0)

    def test_fragment_keeps_prefix_and_locates_error(self):
        good = self._good_batch()
        bad = bytearray(
            build_batch(
                base_offset=1,
                records=[Rec(None, b"world", ())],
                base_timestamp=2,
            )
        )
        bad[25] ^= 0x01
        result = parse_fragment(good + bytes(bad))
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.batch_index, 1)
        self.assertEqual(result.error.file_offset, len(good))
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(result.bytes_consumed, len(good))


if __name__ == "__main__":
    unittest.main()
