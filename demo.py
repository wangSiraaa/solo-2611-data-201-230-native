"""Run fixtures/scenarios.json against the buffer pool and verify behaviour.

Usage (from the repository root):

    python3 demo.py                  # all fixture scenarios + built-in edge cases
    python3 demo.py --only lru       # one scenario (repeatable)
    python3 demo.py --verbose        # include every operation record
    python3 demo.py --json           # machine-readable summary
    python3 demo.py --keep           # keep per-scenario working files

For every scenario the fixture is copied to a fresh temporary file.  The run
records pool events and PageIO events, then a verifier reads the resulting file
directly (not through the pool) and checks the bytes page by page.

Exit status is 0 only if every recorded expectation and every independent check
passes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buffer_pool import (
    AllPinnedError,
    BufferPool,
    BufferPoolError,
    CloseWithPinsError,
    InvalidHandleError,
    PageIOError,
)
from page_io import PageIO

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "fixtures"
PAGES_BIN = FIXTURE_DIR / "pages.bin"
SCENARIOS_JSON = FIXTURE_DIR / "scenarios.json"
PAGE_SIZE = 128


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class OpRecord:
    op: str
    request: dict
    status: str = ""  # "ok" | "expected_error" | "error"
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "request": self.request,
            "status": self.status,
            "result": self.result,
        }


@dataclass
class ScenarioRun:
    scenario_id: str
    capacity: int
    source: str
    work_file: str
    records: list[OpRecord] = field(default_factory=list)
    io_events: list[dict] = field(default_factory=list)
    pool_events: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    final_file_hex: str = ""

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]

    @property
    def ok(self) -> bool:
        return not self.failures

    def add_check(self, name: str, ok: bool, **info: Any) -> None:
        entry = {"name": name, "ok": bool(ok)}
        entry.update(info)
        self.checks.append(entry)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

EXPECTED_CODES = {
    "all_pinned": "all_pinned",
    "io_error": "io_error",
    "invalid_handle": "invalid_handle",
    "page_out_of_range": "page_out_of_range",
    "page_range_error": "page_range_error",
    "pins_held": "pins_held",
    "pool_closed": "pool_closed",
    "close_flush_failed": "close_flush_failed",
}


def classify_error(exc: BufferPoolError) -> tuple[str, dict]:
    result: dict = {
        "code": exc.code,
        "message": exc.message,
        "details": exc.details,
    }
    if isinstance(exc, PageIOError):
        result.update(
            {
                "operation": exc.operation,
                "page_id": exc.page_id,
                "cause_type": type(exc.cause).__name__,
                "cause_message": str(exc.cause),
            }
        )
        if exc.details.get("requested_page_id") is not None:
            result["requested_page_id"] = exc.details["requested_page_id"]
        return "io_error", result
    if isinstance(exc, AllPinnedError):
        result["requested_page_id"] = exc.details.get("requested_page_id")
        result["pinned_pages"] = exc.details.get("pinned_pages", [])
        return "all_pinned", result
    if isinstance(exc, CloseWithPinsError):
        result["pinned_pages"] = exc.details.get("pinned_pages", [])
        return "pins_held", result
    return exc.code, result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ScenarioRunner:
    def __init__(self, scenario: dict, work_dir: Path, *, verify: bool = True):
        self.scenario = scenario
        self.scenario_id = str(scenario.get("id", "scenario"))
        self.capacity = int(scenario["capacity"])
        self.pages_bin = Path(scenario.get("pages_bin", PAGES_BIN))
        if not self.pages_bin.is_absolute():
            self.pages_bin = (ROOT / self.pages_bin).resolve()
        self.work_dir = work_dir
        self.verify = verify
        self.initial_bytes = self.pages_bin.read_bytes()
        if len(self.initial_bytes) % PAGE_SIZE:
            raise ValueError("fixture size is not a multiple of the page size")
        self.page_count = len(self.initial_bytes) // PAGE_SIZE
        self.work_file = work_dir / f"{self.scenario_id}.bin"
        self.pool: BufferPool | None = None
        self.io: PageIO | None = None
        self.handles: dict[str, Any] = {}
        self.run = ScenarioRun(
            scenario_id=self.scenario_id,
            capacity=self.capacity,
            source=str(self.pages_bin),
            work_file=str(self.work_file),
        )
        self.expected_image: dict[int, bytearray] = {}

    # -- setup ------------------------------------------------------------

    def setup(self) -> None:
        shutil.copyfile(self.pages_bin, self.work_file)
        self.io = PageIO(self.work_file)
        self.pool = BufferPool(self.io, self.capacity, page_count=self.page_count)
        for page_id in range(self.page_count):
            start = page_id * PAGE_SIZE
            self.expected_image[page_id] = bytearray(
                self.initial_bytes[start : start + PAGE_SIZE]
            )

    # -- main loop --------------------------------------------------------

    def run_all(self) -> ScenarioRun:
        self.setup()
        for op in self.scenario.get("ops", []):
            self.run_one(op)
        self.finalize()
        return self.run

    def run_one(self, op: dict) -> None:
        assert self.pool is not None and self.io is not None
        kind = op.get("op", "")
        expect = op.get("expect")
        record = OpRecord(
            op=kind, request={k: v for k, v in op.items() if k != "expect"}
        )
        io_before = len(self.io.events)
        pool_before = len(self.pool.events)
        try:
            result = self._dispatch(kind, op)
            if expect:
                record.status = "error"
                record.result = {
                    "error": {
                        "code": "unexpected_success",
                        "message": f"operation succeeded but expected {expect!r}",
                        "expected": expect,
                    }
                }
            else:
                record.status = "ok"
                record.result = result
        except BufferPoolError as exc:
            family, detail = classify_error(exc)
            detail["page_io_events"] = self.io.events[io_before:]
            detail["expected"] = expect
            if expect and family == EXPECTED_CODES.get(expect):
                record.status = "expected_error"
            else:
                record.status = "error"
            record.result = {"error": detail}
        except Exception as exc:  # noqa: BLE001 - record anything unexpected
            record.status = "error"
            record.result = {
                "error": {
                    "code": "unexpected_exception",
                    "message": f"{type(exc).__name__}: {exc}",
                    "expected": expect,
                }
            }

        record.result["pool_events"] = self.pool.events[pool_before:]
        record.result["io_events"] = self.io.events[io_before:]
        self.run.records.append(record)

    def _handle(self, name: str):
        if name not in self.handles:
            raise InvalidHandleError(f"no handle named {name!r} in this scenario")
        return self.handles[name]

    def _dispatch(self, kind: str, op: dict) -> dict:
        assert self.pool is not None and self.io is not None

        if kind == "fetch":
            handle = self.pool.fetch(op["page"])
            alias = op.get("as")
            result = {
                "page_id": handle.page_id,
                "frame": handle.frame_index,
                "from_cache": handle.from_cache,
                "resident_after": self.pool.resident_pages(),
            }
            if alias:
                self.handles[alias] = handle
                result["handle"] = alias
            else:
                result["note"] = "unnamed handle released immediately"
                self.pool.release(handle)
            return result

        if kind == "read":
            handle = self._handle(op["handle"])
            payload = self.pool.read(handle, op["offset"], op.get("length", 1))
            return {"bytes_hex": payload.hex(), "length": len(payload)}

        if kind == "write":
            handle = self._handle(op["handle"])
            payload = bytes.fromhex(op["hex"])
            self.pool.write(handle, op["offset"], payload)
            page_id = handle.page_id
            self.expected_image[page_id][
                op["offset"] : op["offset"] + len(payload)
            ] = payload
            return {
                "page_id": page_id,
                "offset": op["offset"],
                "length": len(payload),
                "hex": payload.hex(),
            }

        if kind == "release":
            handle = self._handle(op["handle"])
            page_id = handle.page_id
            self.pool.release(handle)
            return {"handle": op["handle"], "page_id": page_id}

        if kind == "fail_next_write":
            count = int(op.get("count", 1))
            if count < 1:
                raise ValueError("fail_next_write count must be >= 1")
            self.io.fail_writes += count
            return {"pending_failures": self.io.fail_writes}

        if kind == "flush_all":
            return {"report": self.pool.flush_all().to_dict()}

        if kind == "close":
            return {"report": self.pool.close().to_dict()}

        if kind == "state":
            return {
                "state": self.pool.frames_state(),
                "stats": self.pool.stats_snapshot(),
            }

        raise ValueError(f"unknown operation {kind!r}")

    # -- finalisation -----------------------------------------------------

    def finalize(self) -> None:
        assert self.pool is not None and self.io is not None
        # Release anything the scenario left pinned so verification sees a
        # stable file.  Individual op records already captured the real effects.
        for handle in list(self.handles.values()):
            if getattr(handle, "valid", False):
                try:
                    self.pool.release(handle)
                except BufferPoolError:
                    pass
        if not self.pool.closed:
            self.pool.flush_all()

        self.run.io_events = list(self.io.events)
        self.run.pool_events = list(self.pool.events)
        self.run.stats = self.pool.stats_snapshot()
        self.run.final_file_hex = self.work_file.read_bytes().hex()

        if self.verify:
            self._verify_file()
            self._generic_checks()

    def _verify_file(self) -> None:
        final = self.work_file.read_bytes()
        run = self.run
        run.add_check(
            "file.size_preserved",
            len(final) == self.page_count * PAGE_SIZE,
            expected=len(self.initial_bytes),
            actual=len(final),
        )

        for page_id in range(self.page_count):
            start = page_id * PAGE_SIZE
            expected = bytes(self.expected_image[page_id])
            actual = final[start : start + PAGE_SIZE]
            if expected == actual:
                run.add_check(f"page[{page_id}].bytes_match_expected", True)
                continue
            diff = next(
                (i for i in range(PAGE_SIZE) if expected[i] != actual[i]), 0
            )
            run.add_check(
                f"page[{page_id}].bytes_match_expected",
                False,
                first_diff_offset=diff,
                expected_byte=expected[diff],
                actual_byte=actual[diff],
                expected_hex=expected.hex(),
                actual_hex=actual.hex(),
            )

        wrote_pages = {e["page_id"] for e in self.run.io_events if e["op"] == "write"}
        modified_pages = {
            e.get("page_id")
            for e in self.run.pool_events
            if e.get("event") == "write" and e.get("length", 0) > 0
        }
        for page_id in range(self.page_count):
            if page_id in wrote_pages and page_id not in modified_pages:
                run.add_check(
                    f"page[{page_id}].unmodified_page_not_written",
                    False,
                    note="page was written to disk without any pool write",
                )
            elif page_id not in modified_pages:
                run.add_check(
                    f"page[{page_id}].unmodified_page_not_written", True
                )
            start = page_id * PAGE_SIZE
            initial = self.initial_bytes[start : start + PAGE_SIZE]
            final_page = final[start : start + PAGE_SIZE]
            if initial != final_page:
                run.add_check(
                    f"page[{page_id}].changed_page_was_written",
                    page_id in wrote_pages,
                    changed=True,
                )

        # Every on-disk write must be a pool-issued write-back event.
        writeback_pages = {
            e.get("page_id")
            for e in self.run.pool_events
            if e.get("event") in {"writeback", "flush_writeback"}
        }
        unexpected = sorted(wrote_pages - writeback_pages)
        run.add_check(
            "disk.writes_are_pool_writebacks",
            not unexpected,
            unexpected_pages=unexpected,
        )

    def _generic_checks(self) -> None:
        run = self.run
        reads = [e["page_id"] for e in run.io_events if e["op"] == "read"]
        evicted = {
            e["page_id"]
            for e in run.pool_events
            if e.get("event") == "evict"
        }
        repeated = sorted(
            {p for p in reads if reads.count(p) > 1 and p not in evicted}
        )
        run.add_check(
            "cache.no_reread_without_eviction",
            not repeated,
            read_pages=reads,
            evicted_pages=sorted(evicted),
            repeated_pages=repeated,
        )
        hits = sum(
            1 for e in run.pool_events if e.get("event") == "fetch_hit"
        )
        loads = sum(
            1 for e in run.pool_events if e.get("event") == "fetch_load"
        )
        run.add_check(
            "cache.every_load_has_one_disk_read",
            loads == len(reads),
            loads=loads,
            reads=len(reads),
            hits=hits,
        )


# ---------------------------------------------------------------------------
# Built-in edge cases required by the brief
# ---------------------------------------------------------------------------


def builtin_scenarios() -> list[dict]:
    return [
        {
            "id": "bounds_and_release",
            "capacity": 2,
            "ops": [
                {"op": "fetch", "page": 8, "expect": "page_out_of_range"},
                {"op": "fetch", "page": -1, "expect": "page_out_of_range"},
                {"op": "fetch", "page": 0, "as": "h"},
                {
                    "op": "read", "handle": "h", "offset": 127, "length": 2,
                    "expect": "page_range_error",
                },
                {
                    "op": "read", "handle": "h", "offset": -1, "length": 1,
                    "expect": "page_range_error",
                },
                {
                    "op": "write", "handle": "h", "offset": 127, "hex": "aabb",
                    "expect": "page_range_error",
                },
                {"op": "release", "handle": "h"},
                {"op": "release", "handle": "h", "expect": "invalid_handle"},
                {
                    "op": "read", "handle": "h", "offset": 0, "length": 1,
                    "expect": "invalid_handle",
                },
                {
                    "op": "write", "handle": "h", "offset": 0, "hex": "00",
                    "expect": "invalid_handle",
                },
            ],
        },
        {
            "id": "close_with_pins",
            "capacity": 2,
            "ops": [
                {"op": "fetch", "page": 5, "as": "p"},
                {"op": "fetch", "page": 6, "as": "q"},
                {"op": "close", "expect": "pins_held"},
                {"op": "release", "handle": "p"},
                {"op": "release", "handle": "q"},
                {"op": "close"},
            ],
        },
        {
            "id": "flush_reports_pinned_dirty",
            "capacity": 2,
            "ops": [
                {"op": "fetch", "page": 0, "as": "a"},
                {"op": "write", "handle": "a", "offset": 0, "hex": "deadbeef"},
                {"op": "fetch", "page": 1, "as": "b"},
                {"op": "write", "handle": "b", "offset": 0, "hex": "cafe"},
                {"op": "release", "handle": "b"},
                {"op": "flush_all"},
                {"op": "release", "handle": "a"},
                {"op": "close"},
            ],
        },
        {
            "id": "dirty_eviction_writeback",
            "capacity": 1,
            "ops": [
                {"op": "fetch", "page": 2, "as": "a"},
                {"op": "write", "handle": "a", "offset": 10, "hex": "0102"},
                {"op": "release", "handle": "a"},
                {"op": "fetch", "page": 7, "as": "b"},
                {"op": "release", "handle": "b"},
                {"op": "fetch", "page": 2, "as": "c"},
                {"op": "read", "handle": "c", "offset": 10, "length": 2},
            ],
        },
        {
            "id": "writeback_failure_retry",
            "capacity": 1,
            "ops": [
                {"op": "fetch", "page": 4, "as": "a"},
                {"op": "write", "handle": "a", "offset": 5, "hex": "abcd"},
                {"op": "release", "handle": "a"},
                {"op": "fail_next_write"},
                {"op": "fetch", "page": 0, "as": "b", "expect": "io_error"},
                {"op": "state"},
                {"op": "fetch", "page": 4, "as": "c"},
                {"op": "read", "handle": "c", "offset": 5, "length": 2},
                {"op": "release", "handle": "c"},
                {"op": "fetch", "page": 0, "as": "b"},
                {"op": "release", "handle": "b"},
                {"op": "close"},
            ],
        },
        {
            "id": "stale_handle_after_eviction",
            "capacity": 1,
            "ops": [
                {"op": "fetch", "page": 0, "as": "a"},
                {"op": "write", "handle": "a", "offset": 0, "hex": "ee"},
                {"op": "release", "handle": "a"},
                {"op": "fetch", "page": 1, "as": "b"},
                {
                    "op": "read", "handle": "a", "offset": 0, "length": 1,
                    "expect": "invalid_handle",
                },
                {
                    "op": "write", "handle": "a", "offset": 0, "hex": "ff",
                    "expect": "invalid_handle",
                },
                {
                    "op": "release", "handle": "a",
                    "expect": "invalid_handle",
                },
                {"op": "read", "handle": "b", "offset": 0, "length": 1},
                {"op": "release", "handle": "b"},
            ],
        },
        {
            "id": "shared_visibility_no_double_read",
            "capacity": 1,
            "ops": [
                {"op": "fetch", "page": 3, "as": "a"},
                {"op": "fetch", "page": 3, "as": "b"},
                {"op": "write", "handle": "a", "offset": 100, "hex": "11223344"},
                {"op": "read", "handle": "b", "offset": 100, "length": 4},
                {"op": "release", "handle": "a"},
                {"op": "read", "handle": "b", "offset": 100, "length": 4},
                {"op": "release", "handle": "b"},
            ],
        },
        {
            "id": "lru_hit_updates_sequence",
            "capacity": 2,
            "ops": [
                {"op": "fetch", "page": 0, "as": "a"},
                {"op": "release", "handle": "a"},
                {"op": "fetch", "page": 1, "as": "b"},
                {"op": "release", "handle": "b"},
                {"op": "fetch", "page": 0, "as": "c"},
                {"op": "write", "handle": "c", "offset": 0, "hex": "42"},
                {"op": "release", "handle": "c"},
                {"op": "state"},
                {"op": "fetch", "page": 2, "as": "d"},
                {"op": "state"},
                {"op": "release", "handle": "d"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Scenario-specific independent checks
# ---------------------------------------------------------------------------


def last_read_hex(run: ScenarioRun, page_id: int, offset: int, length: int):
    assert run.records
    for record in run.records:
        if (
            record.op == "read"
            and record.status == "ok"
            and record.request.get("offset") == offset
            and record.request.get("length") == length
        ):
            return record.result.get("bytes_hex")
    return None


def expected_result_checks(run: ScenarioRun) -> None:
    sid = run.scenario_id
    reads = [e["page_id"] for e in run.io_events if e["op"] == "read"]
    writes = [e["page_id"] for e in run.io_events if e["op"] == "write"]
    final = bytes.fromhex(run.final_file_hex)
    page = lambda p: final[p * PAGE_SIZE : (p + 1) * PAGE_SIZE]  # noqa: E731

    if sid == "shared_pin":
        run.add_check("shared_pin.page0_read_once", reads.count(0) == 1, reads=reads)
        run.add_check(
            "shared_pin.page2_loaded_only_after_unpin",
            reads.count(2) == 1,
            reads=reads,
        )
        run.add_check(
            "shared_pin.write_flushed_on_final_flush",
            page(0)[1:3].hex() == "aabb",
            bytes_1_to_3=page(0)[1:3].hex(),
        )
        fetch_records = [r for r in run.records if r.op == "fetch"]
        run.add_check(
            "shared_pin.second_fetch_was_hit",
            len(fetch_records) > 1 and fetch_records[1].result.get("from_cache") is True,
        )

    elif sid == "write_fail":
        run.add_check(
            "write_fail.data_retained_in_memory",
            last_read_hex(run, 3, 0, 1) == "ff",
        )
        run.add_check("write_fail.retry_wrote_page3", 3 in writes, writes=writes)
        run.add_check("write_fail.page3_on_disk", page(3)[0] == 0xFF)
        errors = [r for r in run.records if r.status == "error"]
        run.add_check("write_fail.page4_loaded_once_after_retry", reads.count(4) == 1,
                      reads=reads)
        run.add_check("write_fail.no_unexpected_errors", not errors,
                      errors=[r.to_dict() for r in errors])

    elif sid == "lru":
        events = [e.get("event") for e in run.pool_events]
        run.add_check(
            "lru.page0_hit_after_page1",
            events.count("fetch_hit") == 1,
            events=events,
        )
        run.add_check("lru.no_disk_writes", not writes, writes=writes)
        flush_records = [r for r in run.records if r.op == "flush_all"]
        report = flush_records[0].result.get("report") if flush_records else None
        run.add_check(
            "lru.flush_reports_nothing_dirty",
            report is not None and report.get("flushed") == [],
            report=report,
        )

    elif sid == "lru_hit_updates_sequence":
        states = [
            r.result["state"] for r in run.records if r.op == "state"
        ]
        run.add_check("lru_seq.two_states", len(states) == 2)
        if len(states) == 2:
            seq = {
                f["page_id"]: f["last_fetch_seq"]
                for f in states[0]
                if f["page_id"] is not None
            }
            run.add_check(
                "lru_seq.hit_makes_page0_newer_than_page1",
                seq.get(0, 0) > seq.get(1, 0),
                seq=seq,
            )
            second = {
                f["page_id"]: f for f in states[1] if f["page_id"] is not None
            }
        run.add_check(
            "lru_seq.evicted_page1_not_page0",
            1 not in second and 0 in second and 2 in second,
            resident=sorted(second),
        )

    elif sid == "bounds_and_release":
        run.add_check(
            "bounds.only_page0_read", reads == [0], reads=reads,
        )
        run.add_check("bounds.no_disk_writes", not writes, writes=writes)
        run.add_check(
            "bounds.all_ops_expected",
            all(r.status in {"ok", "expected_error"} for r in run.records),
            ops=[(r.op, r.status) for r in run.records],
        )

    elif sid == "close_with_pins":
        closes = [r for r in run.records if r.op == "close"]
        run.add_check(
            "close.pins_refused_first",
            len(closes) == 2 and closes[0].status == "expected_error",
        )
        run.add_check(
            "close.succeeds_after_release",
            len(closes) == 2 and closes[1].status == "ok",
        )

    elif sid == "flush_reports_pinned_dirty":
        flushes = [r for r in run.records if r.op == "flush_all"]
        report = flushes[0].result.get("report") if flushes else {}
        run.add_check(
            "flush.pinned_page_skipped",
            report.get("skipped_pinned") == [0],
            report=report,
        )
        run.add_check(
            "flush.unpinned_dirty_page_flushed",
            report.get("flushed") == [1],
            report=report,
        )
        run.add_check(
            "flush.only_page1_written_at_that_point",
            report.get("failed") == [],
            report=report,
        )

    elif sid == "dirty_eviction_writeback":
        run.add_check(
            "dirty_eviction.modified_bytes_reloaded_from_disk",
            last_read_hex(run, 2, 10, 2) == "0102",
        )
        run.add_check("dirty_eviction.page2_written", 2 in writes, writes=writes)
        run.add_check("dirty_eviction.page7_not_written", 7 not in writes)

    elif sid == "writeback_failure_retry":
        run.add_check(
            "retry.page4_data_survived_failure",
            last_read_hex(run, 4, 5, 2) == "abcd",
        )
        failed = [
            e for e in run.io_events if e["op"] == "write_failed" and e["page_id"] == 4
        ]
        run.add_check("retry.failed_writeback_observed", len(failed) == 1,
                      failed_events=failed)
        state_records = [r for r in run.records if r.op == "state"]
        if state_records:
            frames = state_records[0].result["state"]
            retained = [f for f in frames if f["page_id"] == 4]
            run.add_check(
                "retry.page4_still_resident_dirty_unpinned",
                bool(retained)
                and retained[0]["dirty"]
                and retained[0]["pin"] == 0,
                frames=retained,
            )
        fetch_errors = [
            r for r in run.records if r.op == "fetch" and r.status == "expected_error"
        ]
        run.add_check("retry.single_fetch_error", len(fetch_errors) == 1)
        run.add_check("retry.page4_on_disk", page(4)[5:7].hex() == "abcd")

    elif sid == "stale_handle_after_eviction":
        run.add_check(
            "stale.new_occupant_untouched",
            last_read_hex(run, 1, 0, 1) == "01",
        )
        run.add_check("stale.old_page_write_reached_disk", page(0)[0] == 0xEE)
        errors = [r for r in run.records if r.status == "error"]
        run.add_check("stale.no_unexpected_errors", not errors,
                      errors=[r.to_dict() for r in errors])

    elif sid == "shared_visibility_no_double_read":
        run.add_check(
            "shared_visibility.both_reads_see_new_bytes",
            all(
                rec.result.get("bytes_hex") == "11223344"
                for rec in run.records
                if rec.op == "read" and rec.status == "ok"
            ),
            reads=[
                rec.result.get("bytes_hex")
                for rec in run.records
                if rec.op == "read"
            ],
        )
        run.add_check(
            "shared_visibility.single_disk_read",
            reads.count(3) == 1,
            reads=reads,
        )
        run.add_check(
            "shared_visibility.mutations_visible_to_peers_before_release",
            len(
                [
                    rec
                    for rec in run.records
                    if rec.op == "read" and rec.status == "ok"
                ]
            )
            == 2,
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_run(run: ScenarioRun, *, verbose: bool) -> str:
    lines = [
        f"== scenario {run.scenario_id!r} (capacity={run.capacity}) -> "
        f"{'OK' if run.ok else 'FAILED'}"
    ]
    for index, record in enumerate(run.records, 1):
        if verbose or record.status == "error":
            lines.append(
                f"  {index:>2}. {record.op:<16} {record.status:<14} "
                f"{json.dumps(record.result, ensure_ascii=False)}"
            )
    checks = run.checks if run.ok else run.failures
    if verbose or not run.ok:
        label = "checks" if run.ok else "FAILED checks"
        lines.append(f"  {label}: {len(checks)}")
        for entry in checks:
            mark = "PASS" if entry["ok"] else "FAIL"
            extras = {k: v for k, v in entry.items() if k not in {"name", "ok"}}
            suffix = f"  {json.dumps(extras, ensure_ascii=False)}" if extras else ""
            lines.append(f"    [{mark}] {entry['name']}{suffix}")
    else:
        lines.append(f"  {len(run.checks)} checks passed")
    lines.append("  io events: " + json.dumps(run.io_events, ensure_ascii=False))
    lines.append("  stats: " + json.dumps(run.stats, ensure_ascii=False))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def write_report(run: ScenarioRun, report_dir: Path) -> Path:
    """Persist one scenario's operations, events and checks as JSON."""

    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / f"{run.scenario_id}.json"
    payload = {
        "scenario": run.scenario_id,
        "ok": run.ok,
        "capacity": run.capacity,
        "source": run.source,
        "work_file": run.work_file,
        "records": [rec.to_dict() for rec in run.records],
        "io_events": run.io_events,
        "pool_events": run.pool_events,
        "stats": run.stats,
        "checks": run.checks,
        "final_file_hex": run.final_file_hex,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", action="append", default=None,
        help="run only the named scenario id (repeatable)",
    )
    parser.add_argument("--verbose", action="store_true", help="show every operation")
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    parser.add_argument(
        "--keep", action="store_true", help="keep temporary working files",
    )
    parser.add_argument(
        "--report-dir",
        default="out/demo-reports",
        help="directory for per-scenario JSON reports (default: out/demo-reports)",
    )
    args = parser.parse_args(argv)

    scenarios = json.loads(SCENARIOS_JSON.read_text(encoding="utf-8"))
    scenarios = scenarios + builtin_scenarios()
    if args.only:
        wanted = set(args.only)
        scenarios = [s for s in scenarios if s["id"] in wanted]
        missing = wanted - {s["id"] for s in scenarios}
        if missing:
            print(f"unknown scenario id(s): {sorted(missing)}", file=sys.stderr)
            return 2

    temp_root = Path(tempfile.mkdtemp(prefix="bufferpool-demo-"))
    report_dir = (ROOT / args.report_dir).resolve() if not Path(args.report_dir).is_absolute() else Path(args.report_dir)
    runs: list[ScenarioRun] = []
    try:
        for scenario in scenarios:
            run = ScenarioRunner(scenario, temp_root).run_all()
            expected_result_checks(run)
            runs.append(run)
            write_report(run, report_dir)

        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "scenario": r.scenario_id,
                            "ok": r.ok,
                            "checks": r.checks,
                            "records": [rec.to_dict() for rec in r.records],
                            "io_events": r.io_events,
                            "pool_events": r.pool_events,
                            "stats": r.stats,
                            "final_file_hex": r.final_file_hex,
                        }
                        for r in runs
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for run in runs:
                print(render_run(run, verbose=args.verbose))
                print()

        failed = [r for r in runs if not r.ok]
        total_checks = sum(len(r.checks) for r in runs)
        summary_stream = sys.stderr if args.json else sys.stdout
        print(
            f"{len(runs) - len(failed)}/{len(runs)} scenarios passed, "
            f"{total_checks} checks; reports in {report_dir}",
            file=summary_stream,
        )
        if failed:
            print(
                "failed scenarios: " + ", ".join(r.scenario_id for r in failed),
                file=summary_stream,
            )
        if args.keep:
            print(f"work files kept in {temp_root}", file=summary_stream)
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
