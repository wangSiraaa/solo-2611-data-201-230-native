"""A small, single-threaded fixed-page buffer pool.

This module deliberately does *not* provide:

* concurrency / locking (the API is single-threaded),
* background flushing,
* crash recovery or power-loss durability guarantees,
* WAL / SQL / B+tree features.

It only manages a fixed number of page frames, opaque pin handles and
write-back of dirty frames.  ``page_io.PageIO`` remains the disk backend.

The public API is documented in ``API.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BufferPoolError(Exception):
    """Base exception for all buffer-pool errors.

    ``code`` is a stable machine-readable string used by the demo and tests.
    ``details`` contains extra structured context.
    """

    code = "buffer_pool_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PageOutOfRangeError(BufferPoolError):
    """The requested page id is outside the data file."""

    code = "page_out_of_range"


class PageRangeError(BufferPoolError):
    """An offset/length range is outside a 128-byte page."""

    code = "page_range_error"


class InvalidHandleError(BufferPoolError):
    """The handle is unknown, already released, evicted, or belongs elsewhere."""

    code = "invalid_handle"


class AllPinnedError(BufferPoolError):
    """Every frame is pinned, so a new page cannot be brought in."""

    code = "all_pinned"


class PoolClosedError(BufferPoolError):
    """An operation was attempted after the pool was closed."""

    code = "pool_closed"


class CloseWithPinsError(BufferPoolError):
    """close() refuses to run while any handle is still pinned."""

    code = "pins_held"


class CloseFlushError(BufferPoolError):
    """close() could not write back one or more dirty pages, so it stayed open."""

    code = "close_flush_failed"


class PageIOError(BufferPoolError):
    """The disk backend failed during a read or write-back."""

    code = "io_error"

    def __init__(
        self,
        operation: str,
        page_id: int,
        cause: BaseException,
        *,
        requested_page_id: int | None = None,
    ):
        details = {
            "operation": operation,
            "page_id": page_id,
            "requested_page_id": requested_page_id,
            "cause_type": type(cause).__name__,
            "cause_message": str(cause),
        }
        super().__init__(
            f"page I/O {operation} failed for page {page_id}: {cause}", details=details
        )
        self.operation = operation
        self.page_id = page_id
        self.cause = cause


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlushReport:
    """Result of ``BufferPool.flush_all()``.

    A page appears in at most one of the three tuples:

    * ``flushed``       -- was dirty, unpinned, and write-back succeeded;
    * ``skipped_pinned`` -- was dirty but still pinned;
    * ``failed``        -- write-back was attempted and raised.
    """

    flushed: tuple[int, ...] = ()
    skipped_pinned: tuple[int, ...] = ()
    failed: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "flushed": list(self.flushed),
            "skipped_pinned": list(self.skipped_pinned),
            "failed": [dict(item) for item in self.failed],
            "ok": self.ok,
        }


@dataclass(frozen=True)
class CloseReport:
    """Result of a successful ``BufferPool.close()``."""

    closed: bool = True
    already_closed: bool = False
    flushed: tuple[int, ...] = ()
    skipped_pinned: tuple[int, ...] = ()
    failed: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed": self.closed,
            "already_closed": self.already_closed,
            "flushed": list(self.flushed),
            "skipped_pinned": list(self.skipped_pinned),
            "failed": [dict(item) for item in self.failed],
        }


# ---------------------------------------------------------------------------
# Internal frame
# ---------------------------------------------------------------------------


class _Frame:
    __slots__ = (
        "index",
        "page_id",
        "data",
        "pin_count",
        "dirty",
        "last_fetch_seq",
        "generation",
        "handles",
    )

    def __init__(self, index: int):
        self.index = index
        self.page_id: int | None = None
        self.data: bytearray | None = None
        self.pin_count = 0
        self.dirty = False
        # Global sequence number of the last *successful fetch* of this page.
        self.last_fetch_seq = 0
        # Incremented whenever the frame loses its page identity.  A handle
        # from an older generation can never address the new occupant.
        self.generation = 0
        # Live handle objects for this frame (normally empty at eviction time,
        # because only pin_count == 0 frames are evictable).
        self.handles: set["PageHandle"] = set()


# ---------------------------------------------------------------------------
# Opaque handle
# ---------------------------------------------------------------------------


class PageHandle:
    """An opaque, single-frame pin handle returned by :meth:`BufferPool.fetch`.

    The handle intentionally exposes **no** mutable page buffer.  All reads
    and writes go through the pool and are checked against:

    * the handle's validity flag (release invalidates it exactly once),
    * the frame generation (an evicted/reused frame invalidates old handles),
    * the frame's page id.

    A caller may hold the object, but cannot keep a mutable view of the page
    that bypasses pinning or invalidation checks.
    """

    __slots__ = (
        "_pool",
        "_frame_index",
        "_page_id",
        "_generation",
        "_valid",
        "_from_cache",
        "_invalid_reason",
    )

    def __init__(
        self,
        pool: "BufferPool",
        frame_index: int,
        page_id: int,
        generation: int,
        from_cache: bool,
    ):
        self._pool = pool
        self._frame_index = frame_index
        self._page_id = page_id
        self._generation = generation
        self._valid = True
        self._from_cache = from_cache
        self._invalid_reason: str | None = None

    @property
    def page_id(self) -> int:
        return self._page_id

    @property
    def frame_index(self) -> int:
        """Diagnostic frame number (not a mutable page buffer)."""

        return self._frame_index

    @property
    def from_cache(self) -> bool:
        """True if this handle was created by a cache hit (no disk read)."""

        return self._from_cache

    @property
    def valid(self) -> bool:
        """True only while this exact handle still pins its original page."""

        pool = self._pool
        if not self._valid or pool._closed:
            return False
        frame = pool._frames[self._frame_index]
        return (
            frame.generation == self._generation
            and frame.page_id == self._page_id
            and frame.pin_count > 0
            and self in frame.handles
        )

    # Convenience delegations; the pool methods enforce all checks.
    def read(self, offset: int, length: int) -> bytes:
        return self._pool.read(self, offset, length)

    def write(self, offset: int, data: bytes | bytearray | memoryview) -> None:
        self._pool.write(self, offset, data)

    def release(self) -> None:
        self._pool.release(self)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        state = "valid" if self.valid else f"invalid:{self._invalid_reason}"
        return (
            f"PageHandle(page_id={self._page_id}, frame={self._frame_index}, "
            f"{state}, from_cache={self._from_cache})"
        )


# ---------------------------------------------------------------------------
# Buffer pool
# ---------------------------------------------------------------------------


class BufferPool:
    """A bounded pool of pinned page frames over a ``PageIO`` backend.

    Parameters
    ----------
    page_io:
        Disk backend.  It must provide ``page_size``, ``read(page_id)`` and
        ``write(page_id, data)``; ``page_io.PageIO`` is the supplied backend.
    capacity:
        Number of page frames (must be >= 1).
    page_count:
        Optional number of pages in the file.  If omitted, it is derived from
        ``page_io.path`` and ``page_size`` (the file size must be a multiple of
        the page size).
    """

    def __init__(
        self,
        page_io: Any,
        capacity: int,
        *,
        page_count: int | None = None,
    ):
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError("capacity must be an integer")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")

        self.io = page_io
        self.capacity = capacity
        self.page_size = int(getattr(page_io, "page_size", 0))
        if self.page_size <= 0:
            raise ValueError("page_io.page_size must be a positive integer")

        if page_count is None:
            path = getattr(page_io, "path", None)
            if path is None:
                raise ValueError(
                    "page_count is required when page_io has no .path attribute"
                )
            size = Path(path).stat().st_size
            if size % self.page_size != 0:
                raise ValueError("data file size is not a multiple of page_size")
            page_count = size // self.page_size
        if isinstance(page_count, bool) or not isinstance(page_count, int):
            raise ValueError("page_count must be an integer")
        if page_count < 0:
            raise ValueError("page_count must be >= 0")

        self.page_count = page_count
        self._frames: list[_Frame] = [_Frame(i) for i in range(capacity)]
        self._page_map: dict[int, _Frame] = {}
        self._fetch_seq = 0
        self._closed = False
        self.events: list[dict[str, Any]] = []
        self.stats: dict[str, int] = {
            "fetch_calls": 0,
            "fetch_hits": 0,
            "fetch_misses": 0,
            "fetch_loads": 0,
            "all_pinned_errors": 0,
            "page_out_of_range_errors": 0,
            "invalid_handle_errors": 0,
            "range_errors": 0,
            "evictions": 0,
            "dirty_evictions": 0,
            "writebacks": 0,
            "writeback_failures": 0,
            "read_io_errors": 0,
            "reads": 0,
            "writes": 0,
            "pins": 0,
            "releases": 0,
            "flush_calls": 0,
            "flush_skips_pinned": 0,
            "flush_writes": 0,
            "close_refusals": 0,
            "close_flush_failures": 0,
        }

    # -- helpers ----------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise PoolClosedError("buffer pool is closed")

    def _log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"event_seq": len(self.events) + 1, "event": event}
        record.update(fields)
        self.events.append(record)
        return record

    def _next_fetch_seq(self) -> int:
        self._fetch_seq += 1
        return self._fetch_seq

    def _validate_page_id(self, page_id: Any) -> int:
        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise PageOutOfRangeError(
                f"page id must be an integer, got {type(page_id).__name__}",
                details={"page_id": repr(page_id), "page_count": self.page_count},
            )
        if page_id < 0 or page_id >= self.page_count:
            raise PageOutOfRangeError(
                f"page id {page_id} outside [0, {self.page_count - 1}]",
                details={"page_id": page_id, "page_count": self.page_count},
            )
        return page_id

    def _validate_range(self, offset: Any, length: Any) -> tuple[int, int]:
        if (
            isinstance(offset, bool)
            or isinstance(length, bool)
            or not isinstance(offset, int)
            or not isinstance(length, int)
        ):
            raise PageRangeError(
                "offset and length must be integers",
                details={
                    "offset": repr(offset),
                    "length": repr(length),
                    "page_size": self.page_size,
                },
            )
        if (
            offset < 0
            or length < 0
            or offset > self.page_size
            or offset + length > self.page_size
        ):
            raise PageRangeError(
                f"range offset={offset}, length={length} outside page size "
                f"{self.page_size}",
                details={
                    "offset": offset,
                    "length": length,
                    "page_size": self.page_size,
                },
            )
        return offset, length

    def _new_handle(self, frame: _Frame, *, from_cache: bool) -> PageHandle:
        assert frame.page_id is not None
        handle = PageHandle(
            self,
            frame.index,
            frame.page_id,
            frame.generation,
            from_cache,
        )
        frame.handles.add(handle)
        return handle

    def _invalidate_handle(self, handle: PageHandle, reason: str) -> None:
        if handle._valid:
            handle._valid = False
            handle._invalid_reason = reason

    def _reset_frame(self, frame: _Frame, reason: str) -> None:
        """Drop a frame's page identity and invalidate any (stale) handles."""

        for handle in list(frame.handles):
            self._invalidate_handle(handle, reason)
        frame.handles.clear()
        frame.page_id = None
        frame.data = None
        frame.pin_count = 0
        frame.dirty = False
        frame.last_fetch_seq = 0
        frame.generation += 1

    def _require_handle(self, handle: Any) -> _Frame:
        if not isinstance(handle, PageHandle):
            self.stats["invalid_handle_errors"] += 1
            raise InvalidHandleError(
                "not a PageHandle from this pool",
                details={"handle_type": type(handle).__name__},
            )
        if handle._pool is not self:
            self.stats["invalid_handle_errors"] += 1
            raise InvalidHandleError(
                "handle belongs to a different buffer pool",
                details={"page_id": handle._page_id},
            )
        if not handle._valid:
            self.stats["invalid_handle_errors"] += 1
            raise InvalidHandleError(
                f"handle is no longer valid ({handle._invalid_reason})",
                details={
                    "page_id": handle._page_id,
                    "reason": handle._invalid_reason,
                    "frame": handle._frame_index,
                },
            )

        frame = self._frames[handle._frame_index]
        if (
            frame.generation != handle._generation
            or frame.page_id != handle._page_id
            or frame.pin_count <= 0
            or handle not in frame.handles
        ):
            self._invalidate_handle(handle, "frame_reused")
            self.stats["invalid_handle_errors"] += 1
            raise InvalidHandleError(
                "handle's frame no longer holds the page",
                details={
                    "page_id": handle._page_id,
                    "frame": handle._frame_index,
                    "reason": "frame_reused",
                },
            )
        return frame

    def _select_victim(self) -> _Frame | None:
        # A completely free frame is always preferable.
        for frame in self._frames:
            if frame.page_id is None and frame.pin_count == 0:
                return frame
        candidates = [frame for frame in self._frames if frame.pin_count == 0]
        if not candidates:
            return None
        # Oldest last successful fetch; frame index breaks a theoretical tie.
        return min(candidates, key=lambda f: (f.last_fetch_seq, f.index))

    def _page_pinned(self, frame: _Frame) -> bool:
        return frame.pin_count > 0

    # -- public API -------------------------------------------------------

    def fetch(self, page_id: int) -> PageHandle:
        """Pin ``page_id`` and return a new independent handle.

        A resident page is a cache hit: the existing frame is reused and its
        pin count increases.  A miss loads the page into a free frame or the
        least-recently-fetched unpinned frame.  If every frame is pinned,
        :class:`AllPinnedError` is raised immediately and nothing is evicted.
        """

        self._ensure_open()
        self.stats["fetch_calls"] += 1
        try:
            page_id = self._validate_page_id(page_id)
        except PageOutOfRangeError:
            self.stats["page_out_of_range_errors"] += 1
            raise

        frame = self._page_map.get(page_id)
        if frame is not None:
            fetch_seq = self._next_fetch_seq()
            frame.last_fetch_seq = fetch_seq
            frame.pin_count += 1
            self.stats["fetch_hits"] += 1
            self.stats["pins"] += 1
            handle = self._new_handle(frame, from_cache=True)
            self._log(
                "fetch_hit",
                page_id=page_id,
                frame=frame.index,
                pin=frame.pin_count,
                dirty=frame.dirty,
                fetch_seq=fetch_seq,
            )
            return handle

        self.stats["fetch_misses"] += 1
        victim = self._select_victim()
        if victim is None:
            self.stats["all_pinned_errors"] += 1
            pinned_pages = sorted(
                f.page_id for f in self._frames if f.pin_count > 0 and f.page_id is not None
            )
            error = AllPinnedError(
                f"cannot fetch page {page_id}: all {self.capacity} frames are pinned",
                details={
                    "requested_page_id": page_id,
                    "capacity": self.capacity,
                    "pinned_pages": pinned_pages,
                },
            )
            self._log(
                "fetch_error",
                code=error.code,
                page_id=page_id,
                pinned_pages=pinned_pages,
                capacity=self.capacity,
            )
            raise error

        old_page_id = victim.page_id
        was_dirty = victim.dirty

        # Write back first.  If this fails, the frame keeps its page id,
        # dirty flag and bytes, and the new fetch fails.
        if old_page_id is not None and was_dirty:
            snapshot = bytes(victim.data or b"")
            try:
                self.io.write(old_page_id, snapshot)
            except (OSError, ValueError) as exc:
                self.stats["writeback_failures"] += 1
                error = PageIOError(
                    "write_back",
                    old_page_id,
                    exc,
                    requested_page_id=page_id,
                )
                self._log(
                    "writeback_failed",
                    reason="eviction",
                    page_id=old_page_id,
                    requested_page_id=page_id,
                    frame=victim.index,
                    dirty=victim.dirty,
                    pin=victim.pin_count,
                    data_hex=snapshot.hex(),
                    error=str(exc),
                )
                self._log(
                    "fetch_error",
                    code=error.code,
                    page_id=page_id,
                    requested_page_id=page_id,
                    retained_page_id=old_page_id,
                    retained_dirty=victim.dirty,
                    frame=victim.index,
                )
                raise error
            victim.dirty = False
            self.stats["writebacks"] += 1
            self._log(
                "writeback",
                reason="eviction",
                page_id=old_page_id,
                frame=victim.index,
                dirty_before=True,
                data_hex=snapshot.hex(),
            )

        # Read the incoming page before dropping the old frame identity.  If
        # the read fails, the (possibly just-cleaned) old page stays resident.
        try:
            data = self.io.read(page_id)
        except (OSError, ValueError) as exc:
            self.stats["read_io_errors"] += 1
            error = PageIOError(
                "read",
                page_id,
                exc,
                requested_page_id=page_id,
            )
            self._log(
                "fetch_error",
                code=error.code,
                page_id=page_id,
                retained_page_id=old_page_id,
                retained_dirty=victim.dirty,
                frame=victim.index,
            )
            raise error
        if not isinstance(data, (bytes, bytearray)) or len(data) != self.page_size:
            cause = ValueError(
                f"backend returned {len(data) if hasattr(data, '__len__') else '?'} "
                f"bytes, expected {self.page_size}"
            )
            self.stats["read_io_errors"] += 1
            error = PageIOError("read", page_id, cause, requested_page_id=page_id)
            self._log(
                "fetch_error",
                code=error.code,
                page_id=page_id,
                retained_page_id=old_page_id,
                frame=victim.index,
            )
            raise error

        if old_page_id is not None:
            self._page_map.pop(old_page_id, None)
            self.stats["evictions"] += 1
            if was_dirty:
                self.stats["dirty_evictions"] += 1
            self._reset_frame(victim, reason="evicted")
            self._log(
                "evict",
                page_id=old_page_id,
                frame=victim.index,
                was_dirty=was_dirty,
                wrote_back=was_dirty,
            )
        else:
            self._reset_frame(victim, reason="free_frame_reused")

        fetch_seq = self._next_fetch_seq()
        victim.page_id = page_id
        victim.data = bytearray(data)
        victim.pin_count = 1
        victim.dirty = False
        victim.last_fetch_seq = fetch_seq
        self._page_map[page_id] = victim

        self.stats["fetch_loads"] += 1
        self.stats["pins"] += 1
        handle = self._new_handle(victim, from_cache=False)
        self._log(
            "fetch_load",
            page_id=page_id,
            frame=victim.index,
            pin=victim.pin_count,
            fetch_seq=fetch_seq,
            evicted_page_id=old_page_id,
        )
        return handle

    def read(self, handle: PageHandle, offset: int, length: int) -> bytes:
        """Copy ``length`` bytes at ``offset`` out of the pinned page."""

        self._ensure_open()
        frame = self._require_handle(handle)
        try:
            offset, length = self._validate_range(offset, length)
        except PageRangeError:
            self.stats["range_errors"] += 1
            raise
        assert frame.data is not None
        value = bytes(frame.data[offset : offset + length])
        self.stats["reads"] += 1
        self._log(
            "read",
            page_id=frame.page_id,
            frame=frame.index,
            offset=offset,
            length=length,
            pin=frame.pin_count,
            dirty=frame.dirty,
        )
        return value

    def write(
        self,
        handle: PageHandle,
        offset: int,
        data: bytes | bytearray | memoryview,
    ) -> None:
        """Copy ``data`` into the pinned page at ``offset`` and mark it dirty."""

        self._ensure_open()
        frame = self._require_handle(handle)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        payload = bytes(data)
        try:
            offset, length = self._validate_range(offset, len(payload))
        except PageRangeError:
            self.stats["range_errors"] += 1
            raise
        assert frame.data is not None
        frame.data[offset : offset + length] = payload
        # A zero-length write changes nothing, so it does not dirty the page.
        if length:
            frame.dirty = True
        self.stats["writes"] += 1
        self._log(
            "write",
            page_id=frame.page_id,
            frame=frame.index,
            offset=offset,
            length=length,
            hex=payload.hex(),
            pin=frame.pin_count,
            dirty=frame.dirty,
        )

    def release(self, handle: PageHandle) -> None:
        """Release exactly one pin.  A second release raises ``invalid_handle``."""

        self._ensure_open()
        frame = self._require_handle(handle)
        frame.pin_count -= 1
        frame.handles.discard(handle)
        self._invalidate_handle(handle, "released")
        self.stats["releases"] += 1
        self._log(
            "release",
            page_id=frame.page_id,
            frame=frame.index,
            pin=frame.pin_count,
            dirty=frame.dirty,
        )

    def flush_all(self) -> FlushReport:
        """Write back dirty unpinned pages; report pinned skips and failures.

        Dirty pages with ``pin_count > 0`` are skipped and reported.  A page's
        dirty flag is cleared only after its write-back succeeds.  This method
        does not raise for individual write failures; inspect the report.
        """

        self._ensure_open()
        self.stats["flush_calls"] += 1
        flushed: list[int] = []
        skipped: list[int] = []
        failed: list[dict[str, Any]] = []

        for frame in self._frames:
            if frame.page_id is None or not frame.dirty:
                continue
            page_id = frame.page_id
            if self._page_pinned(frame):
                skipped.append(page_id)
                self.stats["flush_skips_pinned"] += 1
                self._log(
                    "flush_skip_pinned",
                    page_id=page_id,
                    frame=frame.index,
                    pin=frame.pin_count,
                )
                continue
            snapshot = bytes(frame.data or b"")
            try:
                self.io.write(page_id, snapshot)
            except (OSError, ValueError) as exc:
                self.stats["writeback_failures"] += 1
                failure = {
                    "page_id": page_id,
                    "frame": frame.index,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                failed.append(failure)
                self._log(
                    "flush_writeback_failed",
                    page_id=page_id,
                    frame=frame.index,
                    pin=frame.pin_count,
                    data_hex=snapshot.hex(),
                    error=str(exc),
                )
                continue
            frame.dirty = False
            flushed.append(page_id)
            self.stats["writebacks"] += 1
            self.stats["flush_writes"] += 1
            self._log(
                "flush_writeback",
                page_id=page_id,
                frame=frame.index,
                pin=frame.pin_count,
                data_hex=snapshot.hex(),
            )

        report = FlushReport(
            flushed=tuple(flushed),
            skipped_pinned=tuple(skipped),
            failed=tuple(failed),
        )
        self._log(
            "flush_complete",
            flushed=list(flushed),
            skipped_pinned=list(skipped),
            failed=[dict(item) for item in failed],
        )
        return report

    def close(self) -> CloseReport:
        """Close the pool after flushing dirty pages.

        * Refuses with :class:`CloseWithPinsError` if any pin is outstanding.
        * Flushes all dirty pages (none can be pinned at this point).  If a
          write-back fails, raises :class:`CloseFlushError` and stays open so
          the caller can retry.
        * Calling ``close()`` twice is a safe no-op.
        """

        if self._closed:
            self._log("close", already_closed=True)
            return CloseReport(already_closed=True)

        pinned_pages = sorted(
            f.page_id for f in self._frames if f.page_id is not None and f.pin_count > 0
        )
        if pinned_pages:
            self.stats["close_refusals"] += 1
            self._log(
                "close_refused",
                code=CloseWithPinsError.code,
                pinned_pages=pinned_pages,
            )
            raise CloseWithPinsError(
                f"cannot close: {len(pinned_pages)} page(s) still pinned",
                details={"pinned_pages": pinned_pages},
            )

        report = self.flush_all()
        if report.failed:
            self.stats["close_flush_failures"] += 1
            self._log(
                "close_flush_failed",
                code=CloseFlushError.code,
                failed=[dict(item) for item in report.failed],
            )
            raise CloseFlushError(
                "cannot close: dirty page write-back failed; pool left open",
                details={"failed": [dict(item) for item in report.failed]},
            )

        for frame in self._frames:
            self._reset_frame(frame, reason="pool_closed")
        self._page_map.clear()
        self._closed = True
        self._log(
            "close",
            already_closed=False,
            flushed=list(report.flushed),
        )
        return CloseReport(
            flushed=report.flushed,
            skipped_pinned=report.skipped_pinned,
            failed=report.failed,
        )

    # -- diagnostics ------------------------------------------------------

    def frames_state(self, *, include_data: bool = False) -> list[dict[str, Any]]:
        """Return a read-only diagnostic snapshot; never a mutable handle."""

        snapshot = []
        for frame in self._frames:
            entry: dict[str, Any] = {
                "frame": frame.index,
                "page_id": frame.page_id,
                "pin": frame.pin_count,
                "dirty": frame.dirty,
                "last_fetch_seq": frame.last_fetch_seq,
                "generation": frame.generation,
                "handles": len(frame.handles),
            }
            if include_data and frame.data is not None:
                entry["data_hex"] = bytes(frame.data).hex()
            snapshot.append(entry)
        return snapshot

    def stats_snapshot(self) -> dict[str, int]:
        return dict(self.stats)

    def resident_pages(self) -> list[int]:
        return sorted(self._page_map)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        state = "closed" if self._closed else "open"
        return (
            f"BufferPool(capacity={self.capacity}, pages={self.page_count}, "
            f"{state}, resident={self.resident_pages()})"
        )
