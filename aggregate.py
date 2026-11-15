#!/usr/bin/env python3
"""Bounded-memory exact multi-DISTINCT aggregation for JSONL input.

Python 3.11.  See IMPLEMENTATION.md for the counting rules and the
temporary-file lifecycle.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# Fixed-size staging buffer for already-produced spill records.  This is an
# output/I/O buffer, not part of the counted aggregation state.
CHUNK_RECORD_LIMIT = 64

_REQUIRED_FIELDS = {"row_id", "group", "user", "product", "amount"}
_AMOUNT_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]{1,2})?$")


class InputError(Exception):
    """Invalid input, config, or structure that must fail the whole run."""


# ---------------------------------------------------------------------------
# Parsing / exact decimal handling
# ---------------------------------------------------------------------------

def parse_amount_cents(value: Any) -> int | None:
    """Return exact integer cents for a JSON amount, or None.

    Accepted grammar: -?[0-9]+(.[0-9]{1,2})?  (no float conversion).
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _AMOUNT_RE.fullmatch(value):
        raise InputError(f"invalid amount {value!r}: expected decimal string with <= 2 decimals")
    negative = value.startswith("-")
    body = value[1:] if negative else value
    if "." in body:
        whole, frac = body.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = body, "00"
    cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def format_cents(cents: int) -> str:
    """Format integer cents as a two-decimal string without floats."""
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _validate_text(value: Any, field_name: str, line_no: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputError(f"line {line_no}: {field_name} must be a string or null")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InputError(f"line {line_no}: {field_name} is not encodable as UTF-8") from exc
    return value


def validate_config(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, dict):
        raise InputError("config must be a JSON object")
    limit = raw.get("state_entry_limit")
    fan_in = raw.get("merge_fan_in")
    for name, value in (("state_entry_limit", limit), ("merge_fan_in", fan_in)):
        if type(value) is not int:  # bool is intentionally rejected
            raise InputError(f"config field {name} must be an integer")
    if limit < 1:
        raise InputError("state_entry_limit must be >= 1")
    if fan_in < 2:
        raise InputError("merge_fan_in must be >= 2")
    return limit, fan_in


def validate_row(
    raw: Any, line_no: int, seen_row_ids: set[int]
) -> tuple[int, str | None, str | None, str | None, int | None]:
    if not isinstance(raw, dict):
        raise InputError(f"line {line_no}: each JSONL row must be an object")
    keys = set(raw)
    missing = _REQUIRED_FIELDS - keys
    extra = keys - _REQUIRED_FIELDS
    if missing or extra:
        raise InputError(
            f"line {line_no}: bad fields (missing={sorted(missing)}, extra={sorted(extra)})"
        )

    row_id = raw["row_id"]
    if type(row_id) is not int:  # reject bool
        raise InputError(f"line {line_no}: row_id must be an integer")
    if row_id in seen_row_ids:
        raise InputError(f"line {line_no}: duplicate row_id {row_id}")
    # Keep the duplicate-detection set separate from the counted aggregation
    # state.  It is bounded by the input size (<= 10k rows for this task).
    seen_row_ids.add(row_id)

    group = _validate_text(raw["group"], "group", line_no)
    user = _validate_text(raw["user"], "user", line_no)
    product = _validate_text(raw["product"], "product", line_no)
    amount = parse_amount_cents(raw["amount"])
    return row_id, group, user, product, amount


# ---------------------------------------------------------------------------
# Record ordering / serialization
# ---------------------------------------------------------------------------

_SENTINEL = object()


def gkey(group: str | None) -> tuple[int, bytes]:
    """Sort key: NULL group first, then group strings by UTF-8 bytes."""
    if group is None:
        return (0, b"")
    return (1, group.encode("utf-8"))


def record_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    g = rec["g"]
    gk = gkey(g)
    t = rec["t"]
    if t == "A":
        return (gk, 0)
    if t == "U":
        # U and P are deliberately separate sort domains.
        return (gk, 1, rec["u"])
    if t == "P":
        return (gk, 2, rec["u"], rec["p"])
    raise InputError(f"unknown spill record type {t!r}")


def _dump(rec: dict[str, Any]) -> str:
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"


def iter_run(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                yield json.loads(line)


# ---------------------------------------------------------------------------
# In-memory state and spilling
# ---------------------------------------------------------------------------

@dataclass
class GroupState:
    count: int = 0
    sum_cents: int = 0
    has_sum: bool = False
    users: set[str] = field(default_factory=set)
    pairs: set[tuple[str, str]] = field(default_factory=set)


class Aggregator:
    """Hash aggregation whose counted state never exceeds state_entry_limit.

    Counted items:
      * 1 item for each resident group accumulation (GroupState), and
      * 1 item for each resident distinct user key,
      * 1 item for each resident distinct (user, product) pair key.

    Not counted: the current input row, the fixed 64-record output staging
    buffer, row_id duplicate-detection metadata, and ordinary interpreter/I/O
    overhead.
    """

    def __init__(self, limit: int, fan_in: int, temp_dir: Path) -> None:
        self.limit = limit
        self.fan_in = fan_in
        self.temp_dir = temp_dir

        self.groups: dict[str | None, GroupState] = {}
        self.chunk_records: list[dict[str, Any]] = []
        self.run_paths: list[Path] = []

        self.groups_n = 0
        self.user_keys_n = 0
        self.pair_keys_n = 0
        self.resident = 0

        self.max_resident_entries = 0
        self.peak_detail = {"groups": 0, "user_keys": 0, "pair_keys": 0}

        self.merge_rounds = 0
        self.merge_jobs = 0
        self.max_open_runs = 0
        self.final_runs = 0

        self.counters = {
            "input_rows": 0,
            "group_states_created": 0,
            "group_spills": 0,
            "aggregate_partials_written": 0,
            "user_key_items_added_to_memory": 0,
            "pair_key_items_added_to_memory": 0,
            "user_key_records_written": 0,
            "pair_key_records_written": 0,
            "user_key_direct_spills": 0,
            "pair_key_direct_spills": 0,
            "duplicate_user_keys_in_memory": 0,
            "duplicate_pair_keys_in_memory": 0,
            "chunk_flushes": 0,
            "merge_records_read": 0,
            "merge_records_written": 0,
        }

        self._next_run_number = 0
        self._seen_row_ids: set[int] = set()

    # ---- counters -----------------------------------------------------
    def _update_peak(self) -> None:
        if self.resident > self.max_resident_entries:
            self.max_resident_entries = self.resident
            self.peak_detail = {
                "groups": self.groups_n,
                "user_keys": self.user_keys_n,
                "pair_keys": self.pair_keys_n,
            }
        if self.resident != self.groups_n + self.user_keys_n + self.pair_keys_n:
            raise AssertionError("resident counter accounting drift")

    # ---- input --------------------------------------------------------
    def consume(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    raise InputError(f"line {line_no}: blank line is not a valid JSONL row")
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(f"line {line_no}: invalid JSON: {exc.msg}") from exc
                _, group, user, product, amount = validate_row(
                    raw, line_no, self._seen_row_ids
                )
                self._consume_row(group, user, product, amount)
                self.counters["input_rows"] += 1

    def _get_or_create_group(self, group: str | None) -> GroupState:
        state = self.groups.get(group)
        if state is not None:
            return state
        while self.resident >= self.limit:
            self._spill_one_group()
        state = GroupState()
        self.groups[group] = state
        self.groups_n += 1
        self.resident += 1
        self.counters["group_states_created"] += 1
        self._update_peak()
        return state

    def _consume_row(
        self, group: str | None, user: str | None, product: str | None, amount: int | None
    ) -> None:
        state = self._get_or_create_group(group)
        state.count += 1
        if amount is not None:
            state.sum_cents += amount
            state.has_sum = True

        # Distinct user: NULL is ignored.
        if user is not None:
            self._add_user_key(group, user)
        # Distinct pair: only when both columns are non-NULL.  A pair is kept
        # as a two-field tuple/record; no separator concatenation is used.
        if user is not None and product is not None:
            self._add_pair_key(group, user, product)

    def _add_user_key(self, group: str | None, user: str) -> None:
        state = self.groups.get(group)
        if state is None:
            # The group's accumulator was evicted earlier (a later partial will
            # be merged); just spill this key.
            self._append_distinct_direct(group, "U", user, None, "user_key_direct_spills")
            return
        if user in state.users:
            self.counters["duplicate_user_keys_in_memory"] += 1
            return
        if not self._make_room_for_key(group, state, "user_key_direct_spills", "U", user, None):
            return
        state.users.add(user)
        self.user_keys_n += 1
        self.resident += 1
        self.counters["user_key_items_added_to_memory"] += 1
        self._update_peak()

    def _add_pair_key(self, group: str | None, user: str, product: str) -> None:
        state = self.groups.get(group)
        if state is None:
            self._append_distinct_direct(group, "P", user, product, "pair_key_direct_spills")
            return
        pair = (user, product)
        if pair in state.pairs:
            self.counters["duplicate_pair_keys_in_memory"] += 1
            return
        if not self._make_room_for_key(
            group, state, "pair_key_direct_spills", "P", user, product
        ):
            return
        state.pairs.add(pair)
        self.pair_keys_n += 1
        self.resident += 1
        self.counters["pair_key_items_added_to_memory"] += 1
        self._update_peak()

    def _make_room_for_key(
        self,
        group: str | None,
        state: GroupState,
        direct_counter: str,
        rec_type: str,
        user: str,
        product: str | None,
    ) -> bool:
        """Ensure one slot exists for a new distinct key.

        Returns True when the caller should insert the key into ``state``;
        returns False when the key was spilled directly (count/sum stay in
        memory and the spill run will be merged/deduped later).  The group
        accumulator is not evicted just to hold one more distinct key.
        """
        if self.resident < self.limit:
            return True

        # Free this group's own distinct keys first.  This is what keeps a
        # single hot group's distinct sets bounded: they are flushed to disk
        # while its count/sum accumulator stays resident.
        if state.users or state.pairs:
            self._evict_group_keys(group, state)
            return True

        # This group has no resident keys.  Free another group's keys if any.
        for other_group, other in self.groups.items():
            if other_group != group and (other.users or other.pairs):
                self._evict_group_keys(other_group, other)
                return True

        # All resident slots are group accumulators.  Do not spill an
        # accumulator merely to admit one key; write the key to a run instead.
        self._append_distinct_direct(group, rec_type, user, product, direct_counter)
        return False

    def _evict_group_keys(self, group: str | None, state: GroupState) -> None:
        self.chunk_records.extend({"t": "U", "g": group, "u": u} for u in state.users)
        self.counters["user_key_records_written"] += len(state.users)
        self.chunk_records.extend(
            {"t": "P", "g": group, "u": u, "p": p} for u, p in state.pairs
        )
        self.counters["pair_key_records_written"] += len(state.pairs)
        self.user_keys_n -= len(state.users)
        self.pair_keys_n -= len(state.pairs)
        self.resident -= len(state.users) + len(state.pairs)
        state.users.clear()
        state.pairs.clear()
        if len(self.chunk_records) >= CHUNK_RECORD_LIMIT:
            self._flush_chunk()

    def _append_distinct_direct(
        self,
        group: str | None,
        rec_type: str,
        user: str,
        product: str | None,
        counter_name: str,
    ) -> None:
        if rec_type == "U":
            rec = {"t": "U", "g": group, "u": user}
            self.counters["user_key_records_written"] += 1
        else:
            rec = {"t": "P", "g": group, "u": user, "p": product}
            self.counters["pair_key_records_written"] += 1
        self.counters[counter_name] += 1
        self.chunk_records.append(rec)
        if len(self.chunk_records) >= CHUNK_RECORD_LIMIT:
            self._flush_chunk()

    # ---- spilling -----------------------------------------------------
    def _spill_one_group(self) -> None:
        if not self.groups:
            raise AssertionError("cannot spill: no resident group")
        group = next(iter(self.groups))
        self._spill_group(group)

    def _spill_group(self, group: str | None) -> None:
        state = self.groups.pop(group)
        self.chunk_records.append(
            {
                "t": "A",
                "g": group,
                "c": state.count,
                "s": state.sum_cents,
                "h": state.has_sum,
            }
        )
        self.counters["aggregate_partials_written"] += 1
        for user in state.users:
            self.chunk_records.append({"t": "U", "g": group, "u": user})
            self.counters["user_key_records_written"] += 1
        for user, product in state.pairs:
            self.chunk_records.append({"t": "P", "g": group, "u": user, "p": product})
            self.counters["pair_key_records_written"] += 1

        self.groups_n -= 1
        self.user_keys_n -= len(state.users)
        self.pair_keys_n -= len(state.pairs)
        self.resident -= 1 + len(state.users) + len(state.pairs)
        self.counters["group_spills"] += 1
        # Records are batched in the fixed staging buffer; letting it fill
        # avoids one tiny run per group eviction.  Its size is bounded by
        # CHUNK_RECORD_LIMIT (plus one group's keys at the moment of eviction).
        if len(self.chunk_records) >= CHUNK_RECORD_LIMIT:
            self._flush_chunk()

    def _flush_chunk(self) -> Path | None:
        if not self.chunk_records:
            return None
        run_path = self.temp_dir / f"run-{self._next_run_number:06d}.jsonl"
        self._next_run_number += 1
        records = self.chunk_records
        records.sort(key=record_key)
        with run_path.open("w", encoding="utf-8", newline="\n") as stream:
            for rec in records:
                stream.write(_dump(rec))
        self.chunk_records = []
        self.run_paths.append(run_path)
        self.counters["chunk_flushes"] += 1
        return run_path

    def finish_memory(self) -> None:
        """Write all remaining resident state as one sorted final spill run."""
        for group, state in self.groups.items():
            self.chunk_records.append(
                {
                    "t": "A",
                    "g": group,
                    "c": state.count,
                    "s": state.sum_cents,
                    "h": state.has_sum,
                }
            )
            self.counters["aggregate_partials_written"] += 1
            for user in state.users:
                self.chunk_records.append({"t": "U", "g": group, "u": user})
                self.counters["user_key_records_written"] += 1
            for user, product in state.pairs:
                self.chunk_records.append({"t": "P", "g": group, "u": user, "p": product})
                self.counters["pair_key_records_written"] += 1
        # The counted state is being released, not grown.
        self.groups.clear()
        self.groups_n = 0
        self.user_keys_n = 0
        self.pair_keys_n = 0
        self.resident = 0
        self._flush_chunk()

    # ---- merge --------------------------------------------------------
    def merge_all(self) -> Path | None:
        paths = self.run_paths
        while len(paths) > 1:
            new_paths: list[Path] = []
            for start in range(0, len(paths), self.fan_in):
                batch = paths[start : start + self.fan_in]
                if len(batch) == 1:
                    new_paths.append(batch[0])
                    continue
                out_path = self.temp_dir / f"merge-{self._next_run_number:06d}.jsonl"
                self._next_run_number += 1
                self.max_open_runs = max(self.max_open_runs, len(batch))
                self._merge_batch(batch, out_path)
                self.merge_jobs += 1
                for old in batch:
                    old.unlink(missing_ok=True)
                new_paths.append(out_path)
            paths = new_paths
            self.merge_rounds += 1
        self.run_paths = paths
        self.final_runs = len(paths)
        return paths[0] if paths else None

    def _merge_batch(self, inputs: list[Path], out_path: Path) -> None:
        """k-way merge of at most fan_in sorted runs, streaming O(fan_in)."""
        heap: list[tuple[tuple[Any, ...], int, dict[str, Any]]] = []
        iters: list[Iterator[dict[str, Any]]] = []
        for i, path in enumerate(inputs):
            it = iter_run(path)
            iters.append(it)
            try:
                rec = next(it)
            except StopIteration:
                continue
            heapq.heappush(heap, (record_key(rec), i, rec))

        with out_path.open("w", encoding="utf-8", newline="\n") as out:
            for collapsed in _collapse_stream(self._heap_stream(heap, iters)):
                out.write(_dump(collapsed))
                self.counters["merge_records_written"] += 1

    def _heap_stream(
        self,
        heap: list[tuple[tuple[Any, ...], int, dict[str, Any]]],
        iters: list[Iterator[dict[str, Any]]],
    ) -> Iterator[dict[str, Any]]:
        while heap:
            _, idx, rec = heapq.heappop(heap)
            self.counters["merge_records_read"] += 1
            yield rec
            try:
                nxt = next(iters[idx])
            except StopIteration:
                continue
            heapq.heappush(heap, (record_key(nxt), idx, nxt))


def _collapse_stream(records: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Collapse aggregate partials and dedupe distinct records in sorted order.

    Sorting order is (group key, type rank, distinct key ...).  All A
    (aggregate partial) records for a group precede U/P records, so count/sum
    can be summed in O(1) state and distinct keys from any number of runs are
    deduplicated only when adjacent (which sorted order guarantees).
    """
    pending_gkey: tuple[int, bytes] | None = None
    pending_group: str | None = None
    pending_count = 0
    pending_sum = 0
    pending_has = False
    last_sig: Any = _SENTINEL

    for rec in records:
        t = rec["t"]
        group = rec["g"]
        gk = gkey(group)
        if t == "A":
            if pending_gkey is not None and pending_gkey != gk:
                yield {
                    "t": "A",
                    "g": pending_group,
                    "c": pending_count,
                    "s": pending_sum,
                    "h": pending_has,
                }
                pending_gkey = None
            if pending_gkey is None:
                pending_gkey = gk
                pending_group = group
                pending_count = rec["c"]
                pending_sum = rec["s"]
                pending_has = rec["h"]
            else:
                pending_count += rec["c"]
                pending_sum += rec["s"]
                pending_has = pending_has or rec["h"]
            continue

        # A distinct record: emit any accumulated aggregate partial first.
        if pending_gkey is not None:
            yield {
                "t": "A",
                "g": pending_group,
                "c": pending_count,
                "s": pending_sum,
                "h": pending_has,
            }
            pending_gkey = None

        if t == "U":
            sig = (gk, "U", rec["u"])
        elif t == "P":
            sig = (gk, "P", rec["u"], rec["p"])
        else:
            raise InputError(f"unknown record type {t!r}")
        if sig == last_sig:
            continue
        last_sig = sig
        yield rec

    if pending_gkey is not None:
        yield {
            "t": "A",
            "g": pending_group,
            "c": pending_count,
            "s": pending_sum,
            "h": pending_has,
        }


# ---------------------------------------------------------------------------
# Final result scan
# ---------------------------------------------------------------------------

def write_results(final_run: Path | None, out_path: Path) -> int:
    """Stream the final sorted run and write one JSON result per group."""
    group_count = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as out:
        if final_run is None:
            return 0

        current_gkey: Any = _SENTINEL
        current_group: str | None = None
        count = 0
        sum_cents = 0
        has_sum = False
        user_distinct = 0
        pair_distinct = 0
        last_user: Any = _SENTINEL
        last_pair: Any = _SENTINEL

        def emit() -> None:
            nonlocal group_count
            out.write(
                _dump(
                    {
                        "group": current_group,
                        "count": count,
                        "sum": format_cents(sum_cents) if has_sum else None,
                        "user_distinct": user_distinct,
                        "pair_distinct": pair_distinct,
                    }
                )
            )
            group_count += 1

        for rec in _collapse_stream(iter_run(final_run)):
            gk = gkey(rec["g"])
            if current_gkey is _SENTINEL or gk != current_gkey:
                if current_gkey is not _SENTINEL:
                    emit()
                current_gkey = gk
                current_group = rec["g"]
                count = 0
                sum_cents = 0
                has_sum = False
                user_distinct = 0
                pair_distinct = 0
                last_user = _SENTINEL
                last_pair = _SENTINEL

            if rec["t"] == "A":
                count += rec["c"]
                sum_cents += rec["s"]
                has_sum = has_sum or rec["h"]
            elif rec["t"] == "U":
                if last_user is _SENTINEL or rec["u"] != last_user:
                    user_distinct += 1
                    last_user = rec["u"]
            elif rec["t"] == "P":
                pair = (rec["u"], rec["p"])
                if last_pair is _SENTINEL or pair != last_pair:
                    pair_distinct += 1
                    last_pair = pair
            else:
                raise InputError(f"unknown record type {rec['t']!r}")

        if current_gkey is not _SENTINEL:
            emit()
    return group_count


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

def run(input_path: Path, config_path: Path, out_dir: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            config_raw = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read config {config_path}: {exc}") from exc
    limit, fan_in = validate_config(config_raw)

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    report_path = out_dir / "report.json"

    with tempfile.TemporaryDirectory(prefix="aggregate-") as temp_name:
        temp_dir = Path(temp_name)
        agg = Aggregator(limit, fan_in, temp_dir)

        try:
            agg.consume(input_path)
        except OSError as exc:
            raise InputError(f"cannot read input {input_path}: {exc}") from exc

        agg.finish_memory()
        final_run = agg.merge_all()

        # Stream final results into a sibling temp file, then atomically replace.
        tmp_results = out_dir / ".results.jsonl.tmp"
        try:
            group_count = write_results(final_run, tmp_results)
            os.replace(tmp_results, results_path)
        finally:
            tmp_results.unlink(missing_ok=True)

        report = {
            "input_path": str(input_path),
            "config_path": str(config_path),
            "config": {"state_entry_limit": limit, "merge_fan_in": fan_in},
            "input_rows": agg.counters["input_rows"],
            "groups_output": group_count,
            "results_path": str(results_path),
            "spill": {
                "state_entry_counting": {
                    "counted": [
                        "one item per resident group accumulation",
                        "one item per resident distinct user key",
                        "one item per resident distinct (user, product) pair key",
                    ],
                    "not_counted": [
                        "current input row",
                        f"fixed {CHUNK_RECORD_LIMIT}-record output staging buffer",
                        "row_id duplicate-detection set",
                        "interpreter and fixed I/O overhead",
                    ],
                    "resident_entries_end": agg.resident,
                },
                "max_resident_entries": agg.max_resident_entries,
                "peak_resident_detail": agg.peak_detail,
                # Every chunk flush (group spills and the final in-memory
                # flush) happens before merging, so this is the initial run
                # count.  The final in-memory flush is included.
                "initial_runs": agg.counters["chunk_flushes"],
                "runs_written": agg.counters["chunk_flushes"],
                "merge_rounds": agg.merge_rounds,
                "merge_jobs": agg.merge_jobs,
                "max_open_runs_in_merge": agg.max_open_runs,
                "final_runs": agg.final_runs,
                "counters": dict(agg.counters),
            },
            "temp_files": {
                "lifecycle": "spill/merge runs live in a TemporaryDirectory; "
                "the directory is deleted on success and on handled errors",
            },
        }

        tmp_report = out_dir / ".report.json.tmp"
        try:
            tmp_report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_report, report_path)
        finally:
            tmp_report.unlink(missing_ok=True)

        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aggregate",
        description="Exact bounded-memory multi-DISTINCT GROUP BY over JSONL.",
    )
    parser.add_argument("--input", required=True, type=Path, help="input JSONL path")
    parser.add_argument("--config", required=True, type=Path, help="config JSON path")
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="directory for results/report"
    )
    args = parser.parse_args(argv)

    try:
        report = run(args.input, args.config, args.out_dir)
    except InputError as exc:
        print(f"aggregate: error: {exc}", file=sys.stderr)
        return 2
    print(
        f"aggregate: {report['groups_output']} groups, "
        f"{report['spill']['initial_runs']} initial runs, "
        f"{report['spill']['merge_rounds']} merge rounds, "
        f"max resident {report['spill']['max_resident_entries']} -> {args.out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
