"""Unit tests for buffer_pool.py and the scenario demo.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buffer_pool import (  # noqa: E402
    AllPinnedError,
    BufferPool,
    CloseFlushError,
    CloseWithPinsError,
    InvalidHandleError,
    PageIOError,
    PageOutOfRangeError,
    PageRangeError,
    PoolClosedError,
)
from page_io import PageIO  # noqa: E402

PAGE_SIZE = 128
FIXTURE = ROOT / "fixtures" / "pages.bin"


class PoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-tests-"))
        self.path = self.tmp / "pages.bin"
        shutil.copyfile(FIXTURE, self.path)
        self.io = PageIO(self.path)
        self.pool = BufferPool(self.io, capacity=2)

    def tearDown(self) -> None:
        try:
            for handle in self.pool_handles:
                if handle.valid:
                    self.pool.release(handle)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def pool_handles(self):
        return getattr(self, "_handles", [])

    def fetch(self, page_id: int):
        handle = self.pool.fetch(page_id)
        if not hasattr(self, "_handles"):
            self._handles = []
        self._handles.append(handle)
        return handle

    def file_bytes(self, page_id: int) -> bytes:
        raw = self.path.read_bytes()
        return raw[page_id * PAGE_SIZE : (page_id + 1) * PAGE_SIZE]


class TestConstruction(PoolTestCase):
    def test_page_count_from_file(self):
        self.assertEqual(self.pool.page_count, 8)
        self.assertEqual(self.pool.page_size, PAGE_SIZE)
        self.assertEqual(self.pool.capacity, 2)

    def test_bad_capacity(self):
        with self.assertRaises(ValueError):
            BufferPool(self.io, 0)
        with self.assertRaises(ValueError):
            BufferPool(self.io, -1)
        with self.assertRaises(ValueError):
            BufferPool(self.io, True)

    def test_explicit_page_count(self):
        pool = BufferPool(self.io, 2, page_count=8)
        self.assertEqual(pool.page_count, 8)

    def test_no_path_requires_page_count(self):
        class NoPath:
            page_size = PAGE_SIZE

            def read(self, page_id):  # pragma: no cover
                raise AssertionError

            def write(self, page_id, data):  # pragma: no cover
                raise AssertionError

        with self.assertRaises(ValueError):
            BufferPool(NoPath(), 1)


class TestFetchAndHandles(PoolTestCase):
    def test_fetch_returns_distinct_independent_handles(self):
        a = self.fetch(0)
        b = self.fetch(0)
        self.assertIsNot(a, b)
        self.assertEqual(a.page_id, 0)
        self.assertEqual(b.page_id, 0)
        self.assertEqual(a.frame_index, b.frame_index)
        self.assertEqual(self.pool.frames_state()[a.frame_index]["pin"], 2)
        self.assertTrue(a.valid)
        self.assertTrue(b.valid)

    def test_second_fetch_is_cache_hit_without_disk_read(self):
        self.fetch(0)
        self.fetch(0)
        reads = [e for e in self.io.events if e["op"] == "read"]
        self.assertEqual(reads, [{"op": "read", "page_id": 0}])
        self.assertEqual(self.pool.stats["fetch_hits"], 1)

    def test_handle_exposes_no_mutable_page_buffer(self):
        handle = self.fetch(0)
        self.assertFalse(hasattr(handle, "data"))
        self.assertFalse(hasattr(handle, "bytearray"))
        payload = self.pool.read(handle, 0, 4)
        self.assertIsInstance(payload, bytes)
        payload += b""
        # Snapshot in diagnostics is hex text, not a live buffer.
        state = self.pool.frames_state(include_data=True)[0]
        self.assertIsInstance(state["data_hex"], str)

    def test_writes_through_one_handle_visible_through_another(self):
        a = self.fetch(0)
        b = self.fetch(0)
        self.pool.write(a, 4, b"\xde\xad")
        self.assertEqual(self.pool.read(b, 4, 2), b"\xde\xad")
        self.assertEqual(self.pool.read(a, 4, 2), b"\xde\xad")

    def test_release_once_then_invalid(self):
        h = self.fetch(0)
        self.pool.release(h)
        with self.assertRaises(InvalidHandleError):
            self.pool.release(h)
        with self.assertRaises(InvalidHandleError):
            self.pool.read(h, 0, 1)
        with self.assertRaises(InvalidHandleError):
            self.pool.write(h, 0, b"\x00")
        self.assertFalse(h.valid)

    def test_release_one_of_two_pins_keeps_page_pinned(self):
        a = self.fetch(0)
        b = self.fetch(0)
        self.pool.release(a)
        self.assertEqual(self.pool.frames_state()[0]["pin"], 1)
        self.assertEqual(self.pool.read(b, 0, 1), b"\x00")
        self.pool.release(b)
        self.assertEqual(self.pool.frames_state()[0]["pin"], 0)

    def test_foreign_handle_rejected(self):
        other = BufferPool(PageIO(self.path), 2)
        h = other.fetch(0)
        with self.assertRaises(InvalidHandleError):
            self.pool.read(h, 0, 1)
        other.release(h)

    def test_non_handle_rejected(self):
        with self.assertRaises(InvalidHandleError):
            self.pool.read(bytearray(PAGE_SIZE), 0, 1)


class TestBounds(PoolTestCase):
    def test_page_id_bounds(self):
        for page_id in (-1, 8, 100):
            with self.assertRaises(PageOutOfRangeError):
                self.pool.fetch(page_id)
        self.assertEqual(self.io.events, [])
        with self.assertRaises(PageOutOfRangeError):
            self.pool.fetch("0")
        with self.assertRaises(PageOutOfRangeError):
            self.pool.fetch(True)

    def test_read_ranges(self):
        h = self.fetch(0)
        self.assertEqual(len(self.pool.read(h, 0, 128)), 128)
        self.assertEqual(self.pool.read(h, 128, 0), b"")
        for offset, length in (
            (127, 2),
            (128, 1),
            (-1, 1),
            (0, 129),
            (0, -1),
            (129, 0),
        ):
            with self.assertRaises(PageRangeError):
                self.pool.read(h, offset, length)

    def test_write_ranges(self):
        h = self.fetch(0)
        with self.assertRaises(PageRangeError):
            self.pool.write(h, 127, b"\xaa\xbb")
        with self.assertRaises(PageRangeError):
            self.pool.write(h, -1, b"\xaa")
        with self.assertRaises(PageRangeError):
            self.pool.write(h, 0, b"\xaa" * 129)
        # Nothing was marked dirty by the rejected writes.
        self.assertFalse(self.pool.frames_state()[0]["dirty"])

    def test_zero_length_write_does_not_dirty(self):
        h = self.fetch(0)
        self.pool.write(h, 10, b"")
        self.assertFalse(self.pool.frames_state()[0]["dirty"])


class TestEviction(PoolTestCase):
    def test_all_pinned_raises_without_eviction_or_growth(self):
        a = self.fetch(0)
        b = self.fetch(1)
        with self.assertRaises(AllPinnedError) as ctx:
            self.pool.fetch(2)
        self.assertEqual(ctx.exception.details["pinned_pages"], [0, 1])
        self.assertEqual(self.pool.capacity, 2)
        self.assertEqual(self.pool.resident_pages(), [0, 1])
        self.assertNotIn("evict", [e["event"] for e in self.pool.events])
        # Existing pins are untouched.
        self.assertTrue(a.valid and b.valid)
        self.assertEqual(self.pool.read(a, 0, 1), b"\x00")

    def test_hit_updates_lru_sequence_but_read_does_not(self):
        a = self.fetch(0)
        self.pool.release(a)
        b = self.fetch(1)
        self.pool.release(b)
        c = self.fetch(0)  # hit -> page 0 becomes most recently fetched
        self.pool.read(c, 0, 1)  # must not change recency
        state = self.pool.frames_state()
        seqs = {f["page_id"]: f["last_fetch_seq"] for f in state if f["page_id"] is not None}
        self.assertGreater(seqs[0], seqs[1])
        self.pool.release(c)
        d = self.fetch(2)
        self.assertEqual(self.pool.resident_pages(), [0, 2])
        self.assertEqual(self.pool.stats["evictions"], 1)

    def test_read_does_not_make_a_page_newer(self):
        a = self.fetch(0)
        self.pool.release(a)
        b = self.fetch(1)
        self.pool.release(b)
        # read page 0 directly via a new pin re-fetches (hit) anyway; instead
        # compare a true no-fetch read: hold page 0 pinned to read it, then
        # release and evict the other page.
        c = self.fetch(0)  # this hit legitimately updates the sequence
        seq_after_fetch = self._seq_of(0)
        self.pool.read(c, 0, 1)
        self.assertEqual(self._seq_of(0), seq_after_fetch)

    def _seq_of(self, page_id):
        for f in self.pool.frames_state():
            if f["page_id"] == page_id:
                return f["last_fetch_seq"]
        raise AssertionError("page not resident")

    def test_dirty_frame_written_before_replacement(self):
        a = self.fetch(2)
        self.pool.write(a, 0, b"\x42")
        self.pool.release(a)
        filler = self.fetch(0)  # fills the second free frame
        self.pool.release(filler)
        b = self.fetch(3)  # capacity is now full, page 2 is LRU and evicted
        writes = [e for e in self.io.events if e["op"] == "write"]
        self.assertEqual(writes, [{"op": "write", "page_id": 2}])
        self.assertEqual(self.file_bytes(2)[0], 0x42)
        self.assertEqual(self.pool.read(b, 0, 1), b"\x03")

    def test_clean_frame_not_written_on_eviction(self):
        a = self.fetch(0)
        self.pool.release(a)
        b = self.fetch(1)
        self.pool.release(b)
        c = self.fetch(2)  # evicts clean page 0
        self.assertFalse([e for e in self.io.events if e["op"] == "write"])
        self.assertEqual(self.pool.resident_pages(), [1, 2])

    def test_old_handle_cannot_write_new_occupant(self):
        # Capacity 1 forces the reuse of the same frame.
        self.pool = BufferPool(self.io, capacity=1)
        a = self.fetch(0)
        self.pool.write(a, 0, b"\xee")
        self.pool.release(a)
        b = self.fetch(1)
        self.assertTrue(b.frame_index == a.frame_index)
        with self.assertRaises(InvalidHandleError):
            self.pool.write(a, 0, b"\xff")
        self.assertEqual(self.pool.read(b, 0, 1), b"\x01")
        self.assertEqual(self.file_bytes(0)[0], 0xEE)


class TestWriteBackFailure(PoolTestCase):
    def _fail(self, count=1):
        self.io.fail_writes += count

    def test_failed_dirty_eviction_preserves_frame_and_data(self):
        self.pool = BufferPool(self.io, capacity=1)
        a = self.fetch(4)
        self.pool.write(a, 7, b"\xab\xcd")
        self.pool.release(a)
        self._fail()
        with self.assertRaises(PageIOError) as ctx:
            self.pool.fetch(5)
        self.assertEqual(ctx.exception.operation, "write_back")
        self.assertEqual(ctx.exception.page_id, 4)
        self.assertEqual(ctx.exception.details["requested_page_id"], 5)

        state = self.pool.frames_state()[0]
        self.assertEqual(state["page_id"], 4)
        self.assertTrue(state["dirty"])
        self.assertEqual(state["pin"], 0)
        self.assertEqual(self.pool.resident_pages(), [4])
        self.assertEqual(
            [e["op"] for e in self.io.events],
            ["read", "write_failed"],
        )
        # File itself still holds the original page 4 bytes.
        self.assertEqual(self.file_bytes(4), bytes([4]) * PAGE_SIZE)

    def test_refetch_old_page_sees_in_memory_write_and_retry_succeeds(self):
        self.pool = BufferPool(self.io, capacity=1)
        a = self.fetch(4)
        self.pool.write(a, 7, b"\xab\xcd")
        self.pool.release(a)
        self._fail()
        with self.assertRaises(PageIOError):
            self.pool.fetch(5)

        z = self.fetch(4)  # hit the retained dirty frame, no disk read
        self.assertTrue(z.from_cache)
        self.assertEqual(self.pool.read(z, 7, 2), b"\xab\xcd")
        self.pool.release(z)

        y = self.fetch(5)  # retry write-back during eviction now succeeds
        self.assertEqual(self.file_bytes(4)[7:9], b"\xab\xcd")
        self.assertEqual(self.pool.resident_pages(), [5])
        self.assertEqual(self.pool.read(y, 0, 1), b"\x05")

    def test_flush_all_reports_failure_and_keeps_dirty(self):
        self.pool = BufferPool(self.io, capacity=1)
        h = self.fetch(6)
        self.pool.write(h, 0, b"\x99")
        self.pool.release(h)
        self._fail()
        report = self.pool.flush_all()
        self.assertEqual(report.flushed, ())
        self.assertEqual(report.skipped_pinned, ())
        self.assertEqual(report.failed[0]["page_id"], 6)
        self.assertFalse(report.ok)
        frame = self.pool.frames_state()[0]
        self.assertTrue(frame["dirty"])
        h = self.fetch(6)  # cache hit on the retained dirty frame
        self.assertEqual(self.pool.read(h, 0, 1), b"\x99")
        self.pool.release(h)
        report = self.pool.flush_all()  # retry succeeds
        self.assertEqual(report.flushed, (6,))
        self.assertEqual(self.file_bytes(6)[0], 0x99)
        self.assertFalse(self.pool.frames_state()[0]["dirty"])


class TestFlushAndClose(PoolTestCase):
    def test_flush_skips_pinned_dirty_and_reports_it(self):
        a = self.fetch(0)
        self.pool.write(a, 0, b"\xde\xad")
        b = self.fetch(1)
        self.pool.write(b, 0, b"\xca\xfe")
        self.pool.release(b)
        report = self.pool.flush_all()
        self.assertEqual(report.flushed, (1,))
        self.assertEqual(report.skipped_pinned, (0,))
        self.assertEqual(self.file_bytes(1)[0:2], b"\xca\xfe")
        self.assertEqual(self.file_bytes(0)[0:2], bytes([0, 0]))
        # Pinned page stays dirty until released and flushed.
        self.assertIn(0, [f["page_id"] for f in self.pool.frames_state() if f["dirty"]])
        self.pool.release(a)
        report = self.pool.flush_all()
        self.assertEqual(report.flushed, (0,))
        self.assertEqual(self.file_bytes(0)[0:2], b"\xde\xad")

    def test_flush_clears_dirty_only_after_write(self):
        h = self.fetch(0)
        self.pool.write(h, 0, b"\x11")
        self.pool.release(h)
        report = self.pool.flush_all()
        self.assertTrue(report.ok)
        self.assertFalse(self.pool.frames_state()[0]["dirty"])

    def test_unmodified_pages_never_written(self):
        for page_id in range(8):
            h = self.fetch(page_id)
            self.pool.release(h)
        self.pool.flush_all()
        report = self.pool.close()
        self.assertEqual(report.flushed, ())
        self.assertFalse([e for e in self.io.events if e["op"] == "write"])

    def test_close_refuses_with_pins(self):
        h = self.fetch(0)
        with self.assertRaises(CloseWithPinsError) as ctx:
            self.pool.close()
        self.assertEqual(ctx.exception.details["pinned_pages"], [0])
        self.assertFalse(self.pool.closed)
        self.pool.release(h)
        self.pool.close()
        self.assertTrue(self.pool.closed)

    def test_close_flushes_dirty(self):
        h = self.fetch(0)
        self.pool.write(h, 3, b"\x77")
        self.pool.release(h)
        report = self.pool.close()
        self.assertEqual(report.flushed, (0,))
        self.assertEqual(self.file_bytes(0)[3], 0x77)
        self.assertTrue(self.pool.closed)

    def test_close_idempotent(self):
        self.pool.close()
        second = self.pool.close()
        self.assertTrue(second.already_closed)
        self.assertTrue(self.pool.closed)

    def test_operations_after_close_raise(self):
        self.pool.close()
        with self.assertRaises(PoolClosedError):
            self.pool.fetch(0)
        with self.assertRaises(PoolClosedError):
            self.pool.flush_all()

    def test_close_flush_failure_leaves_pool_open_for_retry(self):
        h = self.fetch(0)
        self.pool.write(h, 0, b"\x55")
        self.pool.release(h)
        self.io.fail_writes += 1
        with self.assertRaises(CloseFlushError):
            self.pool.close()
        self.assertFalse(self.pool.closed)
        self.assertEqual(self.pool.frames_state()[0]["page_id"], 0)
        self.assertTrue(self.pool.frames_state()[0]["dirty"])
        report = self.pool.close()  # retry succeeds
        self.assertEqual(report.flushed, (0,))
        self.assertEqual(self.file_bytes(0)[0], 0x55)


class TestDemoScenarios(unittest.TestCase):
    def test_demo_runs_clean(self):
        import contextlib
        import io

        import demo

        buffer = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            rc = demo.main(["--json"])
        self.assertEqual(rc, 0, buffer.getvalue())
        summary = json.loads(buffer.getvalue())
        self.assertTrue(all(item["ok"] for item in summary))

    def test_all_fixture_expectations_are_meaningful(self):
        scenarios = json.loads((ROOT / "fixtures" / "scenarios.json").read_text())
        self.assertEqual({s["id"] for s in scenarios}, {"shared_pin", "write_fail", "lru"})
        for scenario in scenarios:
            self.assertGreaterEqual(scenario["capacity"], 1)
            for op in scenario["ops"]:
                self.assertIn("op", op)


if __name__ == "__main__":
    unittest.main()
