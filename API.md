# BufferPool API

`buffer_pool.py` implements a single-threaded, fixed-size page cache over
`page_io.PageIO` (128-byte pages, 8 pages in `fixtures/pages.bin`).

It provides **only** page buffering: no B+tree, SQL, WAL, concurrency, background
flushing, crash recovery, or power-loss durability promises. `PageIO.write()`
does `flush()` on the process file object but that is not a durability guarantee.

```python
from page_io import PageIO
from buffer_pool import BufferPool

pool = BufferPool(PageIO("pages.bin"), capacity=2)
```

## Concepts

| Term | Meaning |
| --- | --- |
| **frame** | One of `capacity` in-memory slots. Holds at most one page. |
| **handle** | Opaque per-fetch object. It pins a frame; it is not a mutable buffer. |
| **pin count** | Number of live handles for a resident page. Only `0`-pin frames may be evicted. |
| **dirty** | Page has in-memory changes not yet written back. |
| **fetch sequence** | Global counter bumped on every *successful* fetch. Used for LRU. |
| **generation** | Bumped when a frame changes page identity; invalidates old handles. |

A `PageHandle` exposes `page_id`, `frame_index`, `from_cache`, `valid` and
`read()/write()/release()`, but **no** direct access to the page bytes. Callers
can store the object long-term, but after `release()` or after its frame is
reused, every read/write/release through it raises `InvalidHandleError`. A
mutable `bytearray` is never handed out.

## Public API

### `BufferPool(page_io, capacity, *, page_count=None)`

* `page_io` – object with `page_size`, `read(page_id)`, `write(page_id, data)`
  (`PageIO` qualifies).
* `capacity` – integer ≥ 1, number of frames.
* `page_count` – optional; derived from `len(page_io.path) / page_size` when
  omitted. The file size must be a multiple of the page size.

Holds `io`, `capacity`, `page_size`, `page_count`, `closed`.

### `fetch(page_id) -> PageHandle`

Pins `page_id`.

* **Hit** (page already resident): no disk read; pin count +1; updates the page's
  last-fetch sequence; returns a new handle with `from_cache=True`.
* **Miss**: picks a victim:
  1. a completely free frame if one exists;
  2. otherwise the unpinned frame with the smallest last-fetch sequence (ties
     broken by frame index).
* A dirty victim is written back **before** its identity changes. If the write
  fails, the frame keeps its page id, dirty flag and bytes, and the fetch raises
  `PageIOError`; the new page is never loaded. The retained page can be fetched
  again (a hit) and the write-back retried by a later eviction/flush.
* If every frame is pinned, raises `AllPinnedError` immediately. Nothing is
  grown or evicted.
* Invalid `page_id` → `PageOutOfRangeError` before any disk I/O.
* Read failure from the backend → `PageIOError`; the previous victim state is
  kept (if it was dirty, its bytes were already safely written back).

### `read(handle, offset, length) -> bytes`

Copies `length` bytes out of the pinned page and returns an immutable `bytes`.
`0 <= offset`, `0 <= length`, `offset + length <= page_size`; otherwise
`PageRangeError`. `length=0` is allowed. Does **not** change LRU recency. Does
**not** accept released, evicted, or foreign handles (`InvalidHandleError`).

### `write(handle, offset, data)`

Copies a bytes-like `data` into the pinned page and marks the page dirty.
Range rules are as for `read` applied to `len(data)`. A zero-length write
changes no bytes and does not mark the page dirty. Same handle validity rules as
`read()`. Changes are immediately visible through every other handle for the
same page.

### `release(handle)`

Decrements the frame's pin count once and permanently invalidates that handle.
A second release, or any later read/write/release, raises `InvalidHandleError`.
Releasing a different live handle for the same page only drops that handle's
pin.

### `flush_all() -> FlushReport`

Walks resident frames. Dirty unpinned pages are written back; `dirty` is cleared
only after a successful write. Dirty pinned pages are skipped and reported. A
write failure is recorded, not raised.

`FlushReport` fields:

* `flushed: tuple[int, ...]` – pages written successfully;
* `skipped_pinned: tuple[int, ...]` – dirty pages still pinned;
* `failed: tuple[dict, ...]` – `{page_id, frame, error_type, error}`;
* `ok: bool` – `not failed`;

### `close() -> CloseReport`

* Any outstanding pin → raises `CloseWithPinsError` and stays open.
* Otherwise flush dirty pages (equivalent to `flush_all`; none can be pinned).
  Any write-back failure raises `CloseFlushError` and leaves the pool **open**
  with dirty data intact, so the caller can retry.
* On success marks the pool closed, invalidates all handles, and returns a
  `CloseReport` (`flushed`, `skipped_pinned`, `failed`, `closed`,
  `already_closed`).
* Calling `close()` again returns `already_closed=True` without error.
* Every other operation after close raises `PoolClosedError`.

### Diagnostics

* `stats` / `stats_snapshot()` – counters: fetch hits/loads/errors, evictions,
  writebacks, writeback failures, writes, pins, flush skips, close refusals, …
* `events` – ordered event log (fetch_hit, fetch_load, evict, writeback,
  writeback_failed, write, read, release, flush_skip_pinned, flush_writeback,
  flush_writeback_failed, close_refused, close, …). Events include a hex
  snapshot of bytes written at write-back time. The log is plain dicts, not
  page buffers.
* `frames_state(include_data=False)` – per-frame `{frame, page_id, pin, dirty,
  last_fetch_seq, generation, handles}`. `include_data=True` adds a hex **string**.
* `resident_pages()` – sorted list of resident page ids.

## Errors and codes

All pool errors subclass `BufferPoolError` and carry `.code`, `.message`,
`.details`, plus `.to_dict()`.

| Exception | `code` | When |
| --- | --- | --- |
| `PageOutOfRangeError` | `page_out_of_range` | `page_id` not in `[0, page_count)` |
| `PageRangeError` | `page_range_error` | offset/length outside the page |
| `InvalidHandleError` | `invalid_handle` | released, evicted/reused, foreign, or not a handle |
| `AllPinnedError` | `all_pinned` | every frame pinned; `details.pinned_pages` |
| `PoolClosedError` | `pool_closed` | operation after `close()` |
| `CloseWithPinsError` | `pins_held` | `close()` with pins; `details.pinned_pages` |
| `CloseFlushError` | `close_flush_failed` | dirty write-back failed during `close()`; pool stays open |
| `PageIOError` | `io_error` | backend read/write failure; `.operation`, `.page_id`, `.cause`, `details` with `requested_page_id` |

`PageIOError` wraps `OSError`/`ValueError` from the backend. A failed eviction
write-back reports `.page_id` = retained page, `details["requested_page_id"]`
= page the fetch asked for.

## Behavioural guarantees (and non-goals)

* Only `pin == 0` frames are evicted; LRU is by the last successful fetch
  sequence (hits update it; reads/writes do not).
* All-pinned fetch never silently grows the pool or evicts a borrowed page.
  `capacity` never changes.
* Dirty eviction writes back first, then changes identity. Failures preserve
  page identity, dirty state and all in-memory bytes; the new page is not loaded.
* Handles are per-fetch and generation-checked; an invalidated handle can never
  write a later occupant of its old frame.
* `flush_all` flushes only unpinned dirty pages, reports pinned ones, and clears
  dirty only on success.
* `close` refuses while pinned and flushes dirty pages before closing.
* Handles for the same page share one buffer; writes are immediately visible.
* **Not provided:** locking/thread safety, background writing, redo/undo logs,
  crash recovery, or os-level durability guarantees. Use one pool per file
  per thread.

## Demo, tests, example

```bash
python3 demo.py            # fixtures/scenarios.json + built-in edge cases
python3 demo.py --verbose  # per-operation output
python3 example.py         # narrated step-by-step walkthrough
python3 -m unittest discover -s tests -v
```

Each scenario's operation records, pool events, backend events, stats and check
results are also written to `out/demo-reports/<scenario>.json` (override with
`--report-dir`).

Each demo scenario copies the fixture to a fresh temporary file, records pool
events and `PageIO` events, then independently reads the resulting file byte by
byte and compares it with an expected image maintained from the scenario's writes. It also checks that pages are read at most once without an intervening eviction and that every disk write is a pool write-back. The verifier is not allowed to trust pool state for byte correctness.
