"""事务可见性、LSO 闸门、epoch 隔离与给定 fixtures 端到端测试。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from kafka_reader.analyze import (
    REASON_ABORTED,
    REASON_COMMITTED,
    REASON_CONTROL,
    REASON_LSO_TAIL,
    REASON_NON_TRANSACTIONAL,
    REASON_PENDING,
    Metadata,
    analyze,
    load_metadata,
)
from kafka_reader.batch import (
    COMPRESSION_GZIP,
    CONTROL_KEY_ABORT,
    CONTROL_KEY_COMMIT,
    parse_fragment,
)

try:
    from .fixture_builder import Rec, build_batch, control_record
except ImportError:  # 直接以 tests/ 为起点 discover 时
    from fixture_builder import Rec, build_batch, control_record

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def meta(lso=6, hwm=8, partition=0):
    return Metadata(
        topic="t",
        partition=partition,
        high_watermark=hwm,
        last_stable_offset=lso,
    )


class TransactionSemanticsTest(unittest.TestCase):
    def _entries(self, blob, md):
        fragment = parse_fragment(blob)
        self.assertTrue(fragment.ok, fragment.error)
        result = analyze(fragment.batches, md)
        return {e.offset: e for e in result.entries}, result

    def test_commit_abort_and_interleaving(self):
        # producer A: offsets 0..1 数据 -> ABORT @4
        # producer B: offset 2 数据 -> COMMIT @5
        # 交错排列：A0, B2, A1, ABORT(A), COMMIT(B)
        blob = b"".join(
            [
                build_batch(
                    base_offset=0,
                    records=[Rec(None, b"a0", ())],
                    base_timestamp=10,
                    producer_id=100,
                    producer_epoch=0,
                    transactional=True,
                ),
                build_batch(
                    base_offset=2,
                    records=[Rec(None, b"b0", ())],
                    base_timestamp=11,
                    producer_id=200,
                    producer_epoch=0,
                    transactional=True,
                ),
                build_batch(
                    base_offset=3,
                    records=[Rec(None, b"a1", ())],
                    base_timestamp=12,
                    producer_id=100,
                    producer_epoch=0,
                    transactional=True,
                ),
                build_batch(
                    base_offset=4,
                    records=[control_record(CONTROL_KEY_ABORT, 1)],
                    base_timestamp=13,
                    producer_id=100,
                    producer_epoch=0,
                    transactional=True,
                    control=True,
                ),
                build_batch(
                    base_offset=5,
                    records=[control_record(CONTROL_KEY_COMMIT, 1)],
                    base_timestamp=14,
                    producer_id=200,
                    producer_epoch=0,
                    transactional=True,
                    control=True,
                ),
            ]
        )
        entries, result = self._entries(blob, meta(lso=6))
        self.assertEqual(entries[0].reason, REASON_ABORTED)
        self.assertEqual(entries[3].reason, REASON_ABORTED)
        self.assertEqual(entries[2].reason, REASON_COMMITTED)
        self.assertTrue(entries[2].visible)
        self.assertFalse(entries[0].visible)
        self.assertEqual(entries[4].reason, REASON_CONTROL)
        self.assertFalse(entries[4].visible)
        states = {(t.producer_id, t.state) for t in result.transactions}
        self.assertIn((100, "aborted"), states)
        self.assertIn((200, "committed"), states)

    def test_pending_transaction_excluded(self):
        blob = b"".join(
            [
                build_batch(
                    base_offset=0,
                    records=[Rec(None, b"open", ())],
                    base_timestamp=1,
                    producer_id=5,
                    producer_epoch=0,
                    transactional=True,
                )
            ]
        )
        entries, _ = self._entries(blob, meta(lso=6))
        self.assertEqual(entries[0].reason, REASON_PENDING)
        self.assertFalse(entries[0].visible)

    def test_lso_blocks_even_complete_non_transactional(self):
        # offset 0 非事务且完整；LSO=1 时仍不可提前导出
        blob = build_batch(
            base_offset=0,
            records=[Rec(None, b"x", ())],
            base_timestamp=1,
        )
        entries, _ = self._entries(blob, meta(lso=0))
        self.assertEqual(entries[0].reason, REASON_LSO_TAIL)
        self.assertFalse(entries[0].visible)
        self.assertTrue(entries[0].beyond_lso)

        # LSO 放到 offset 之后即可见
        entries2, _ = self._entries(blob, meta(lso=1))
        self.assertEqual(entries2[0].reason, REASON_NON_TRANSACTIONAL)
        self.assertTrue(entries2[0].visible)

    def test_old_epoch_marker_does_not_release_new_epoch(self):
        # epoch 0 已提交结束；epoch 1 重新开始事务且未决。
        # 再来一个 epoch 0 的 COMMIT（材料异常/旧标记），绝不能提交 epoch 1。
        blob = b"".join(
            [
                build_batch(
                    base_offset=0,
                    records=[Rec(None, b"old", ())],
                    base_timestamp=1,
                    producer_id=7,
                    producer_epoch=0,
                    transactional=True,
                ),
                build_batch(
                    base_offset=1,
                    records=[control_record(CONTROL_KEY_COMMIT, 1)],
                    base_timestamp=2,
                    producer_id=7,
                    producer_epoch=0,
                    transactional=True,
                    control=True,
                ),
                build_batch(
                    base_offset=2,
                    records=[Rec(None, b"new", ())],
                    base_timestamp=3,
                    producer_id=7,
                    producer_epoch=1,
                    transactional=True,
                ),
                build_batch(
                    base_offset=3,
                    records=[control_record(CONTROL_KEY_COMMIT, 1)],
                    base_timestamp=4,
                    producer_id=7,
                    producer_epoch=0,  # 旧 epoch 标记
                    transactional=True,
                    control=True,
                ),
            ]
        )
        entries, result = self._entries(blob, meta(lso=6))
        self.assertTrue(entries[0].visible)  # epoch0 正常提交
        self.assertEqual(entries[2].reason, REASON_PENDING)  # epoch1 仍未决
        self.assertFalse(entries[2].visible)
        self.assertEqual(len(result.dangling_markers), 1)
        self.assertEqual(result.dangling_markers[0].offset, 3)

    def test_null_key_distinct_from_empty_and_tombstone_kept(self):
        blob = build_batch(
            base_offset=0,
            records=[
                Rec(key=None, value=b"v0", headers=(), offset_delta=0),
                Rec(key=b"", value=b"", headers=(), offset_delta=1),
                Rec(key=b"k", value=None, headers=(), offset_delta=2),
            ],
            base_timestamp=1,
        )
        entries, _ = self._entries(blob, meta(lso=3))
        self.assertIsNone(entries[0].key)
        self.assertEqual(entries[1].key, b"")
        self.assertEqual(entries[1].value, b"")
        # tombstone 不被删除、不按 key 去重
        self.assertIsNone(entries[2].value)
        self.assertTrue(all(e.visible for e in entries.values()))

    def test_offsets_not_renumbered(self):
        # 批 baseOffset 从 100 开始，导出必须保留原始 offset
        blob = build_batch(
            base_offset=100,
            records=[
                Rec(None, b"a", (), offset_delta=0),
                Rec(None, b"b", (), offset_delta=1),
            ],
            base_timestamp=1,
        )
        entries, _ = self._entries(blob, meta(lso=102))
        self.assertEqual(sorted(entries), [100, 101])


class GzipTransactionTest(unittest.TestCase):
    def test_gzip_transactional_batch_aborted(self):
        blob = b"".join(
            [
                build_batch(
                    base_offset=0,
                    records=[
                        Rec(None, b"gz-a", (), offset_delta=0),
                        Rec(None, b"gz-b", (), offset_delta=1),
                    ],
                    base_timestamp=1,
                    producer_id=3,
                    producer_epoch=0,
                    transactional=True,
                    compression=COMPRESSION_GZIP,
                ),
                build_batch(
                    base_offset=2,
                    records=[control_record(CONTROL_KEY_ABORT, 1)],
                    base_timestamp=2,
                    producer_id=3,
                    producer_epoch=0,
                    transactional=True,
                    control=True,
                ),
            ]
        )
        fragment = parse_fragment(blob)
        self.assertTrue(fragment.ok)
        entries = analyze(fragment.batches, meta(lso=3)).entries
        self.assertTrue(all(not e.visible for e in entries))
        self.assertEqual(
            [e.reason for e in entries],
            [REASON_ABORTED, REASON_ABORTED, REASON_CONTROL],
        )


class ProvidedFixtureTest(unittest.TestCase):
    """用题目给定材料独立核对完整场景。"""

    def setUp(self):
        self.metadata = load_metadata(FIXTURES / "metadata.json")
        self.data = (FIXTURES / "partition.log").read_bytes()

    def test_good_fixture_visibility(self):
        fragment = parse_fragment(self.data)
        self.assertTrue(fragment.ok)
        self.assertEqual(len(fragment.batches), 7)
        result = analyze(fragment.batches, self.metadata)

        visible = result.visible_entries
        self.assertEqual([e.offset for e in visible], [2, 4])
        self.assertEqual(visible[0].value, b"ordinary")
        # 重复 header key 与顺序
        self.assertEqual(
            [h.key for h in visible[0].headers], [b"x", b"x"]
        )
        self.assertEqual(
            [h.value for h in visible[0].headers], [b"1", b"2"]
        )
        # null value tombstone 保留
        self.assertEqual(visible[1].key, b"delete-key")
        self.assertIsNone(visible[1].value)

        by_offset = {e.offset: e for e in result.entries}
        self.assertEqual(by_offset[0].reason, REASON_ABORTED)
        self.assertEqual(by_offset[1].reason, REASON_ABORTED)
        self.assertEqual(by_offset[3].reason, REASON_CONTROL)
        self.assertEqual(by_offset[5].reason, REASON_CONTROL)
        self.assertEqual(by_offset[6].reason, REASON_PENDING)
        self.assertTrue(by_offset[6].beyond_lso)
        self.assertEqual(by_offset[7].reason, REASON_LSO_TAIL)

        # 原始 offset 连续且不重排
        self.assertEqual(
            [e.offset for e in result.entries], list(range(8))
        )

    def test_bad_crc_fixture(self):
        fragment = parse_fragment((FIXTURES / "bad-crc.log").read_bytes())
        self.assertFalse(fragment.ok)
        self.assertEqual(fragment.error.kind, "crc_mismatch")
        self.assertEqual(fragment.error.batch_index, 0)
        self.assertEqual(fragment.error.file_offset, 0)

    def test_truncated_fixture(self):
        fragment = parse_fragment((FIXTURES / "truncated.log").read_bytes())
        self.assertFalse(fragment.ok)
        self.assertEqual(fragment.error.kind, "truncated")
        self.assertEqual(fragment.error.batch_index, 6)
        self.assertEqual(fragment.error.file_offset, 497)
        # 已确认前缀保留
        self.assertEqual(len(fragment.batches), 6)


class CliTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "kafka_reader", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_inspect_json_and_export(self):
        proc = self._run(
            "inspect",
            str(FIXTURES / "partition.log"),
            str(FIXTURES / "metadata.json"),
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["parse"]["ok"])
        self.assertEqual(payload["visible_count"], 2)

        out = ROOT / "output" / "test_export.jsonl"
        try:
            proc = self._run(
                "export",
                str(FIXTURES / "partition.log"),
                str(FIXTURES / "metadata.json"),
                str(out),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual([r["offset"] for r in rows], [2, 4])
            self.assertEqual(rows[0]["headers"][0], {"key": "x", "value": "1"})
            self.assertIsNone(rows[1]["value"])
        finally:
            if out.exists():
                out.unlink()
            out.parent.rmdir() if out.parent.exists() and not any(out.parent.iterdir()) else None

    def test_export_refuses_on_corruption(self):
        out = ROOT / "output" / "should_not_exist.jsonl"
        try:
            proc = self._run(
                "export",
                str(FIXTURES / "bad-crc.log"),
                str(FIXTURES / "metadata.json"),
                str(out),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(out.exists())
        finally:
            if out.exists():
                out.unlink()


if __name__ == "__main__":
    unittest.main()
