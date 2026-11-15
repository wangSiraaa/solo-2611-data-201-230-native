"""Step-by-step BufferPool walkthrough.

Run from the repository root:

    python3 example.py

The script works on a temporary copy of fixtures/pages.bin and prints the state
after each operation.  It never touches the original fixture.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from buffer_pool import AllPinnedError, BufferPool, InvalidHandleError
from page_io import PageIO

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 128


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def show(pool: BufferPool, note: str = "") -> None:
    state = pool.frames_state()
    cells = []
    for frame in state:
        page = "-" if frame["page_id"] is None else frame["page_id"]
        flags = f"pin={frame['pin']} dirty={int(frame['dirty'])}"
        cells.append(f"frame {frame['frame']}: page {page} {flags}")
    print(f"  {note}")
    for cell in cells:
        print(f"    {cell}")
    print(f"    resident={pool.resident_pages()} seq={pool._fetch_seq}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bufferpool-example-"))
    path = tmp / "pages.bin"
    shutil.copyfile(ROOT / "fixtures" / "pages.bin", path)
    print(f"working copy: {path}")

    try:
        banner("1. Create a 2-frame pool")
        io = PageIO(path)
        pool = BufferPool(io, capacity=2)
        print(f"  page_count={pool.page_count}, capacity={pool.capacity}")

        banner("2. fetch(page 0) twice -> one resident frame, two pins")
        a = pool.fetch(0)
        b = pool.fetch(0)
        print(f"  a: page={a.page_id} frame={a.frame_index} from_cache={a.from_cache}")
        print(f"  b: page={b.page_id} frame={b.frame_index} from_cache={b.from_cache}")
        show(pool, "page 0 is pinned twice")
        print(f"  disk reads so far: {[e for e in io.events if e['op'] == 'read']}")

        banner("3. Write through handle a; handle b sees it immediately")
        pool.write(a, 1, b"\xaa\xbb")
        print(f"  read via a: {pool.read(a, 0, 4).hex()}")
        print(f"  read via b: {pool.read(b, 0, 4).hex()}")
        show(pool, "page 0 is dirty")

        banner("4. release(a): one pin left on page 0")
        pool.release(a)
        show(pool, "page 0 pinned once")

        banner("5. Fetch page 1, then try page 2 while both frames are pinned")
        c = pool.fetch(1)
        show(pool, "pages 0 and 1 resident and pinned")
        try:
            pool.fetch(2)
        except AllPinnedError as exc:
            print(f"  all_pinned: {exc.message}")
            print(f"  pinned pages: {exc.details['pinned_pages']}")
        print("  nothing was evicted or loaded")

        banner("6. release(b); fetch(page 2) now evicts LRU page 0")
        pool.release(b)
        d = pool.fetch(2)
        show(pool, "page 0 evicted (dirty write-back), page 1 and 2 resident")
        writes = [e for e in io.events if e["op"] == "write"]
        print(f"  disk writes: {writes}")
        page0_on_disk = path.read_bytes()[:4]
        print(f"  page 0 first 4 bytes on disk: {page0_on_disk.hex()} (the aa bb landed)")

        banner("7. The released handle a stays invalid even though page 0 was reloaded later")
        try:
            pool.read(a, 0, 1)
        except InvalidHandleError as exc:
            print(f"  invalid_handle: {exc.message}")
        try:
            pool.release(a)
        except InvalidHandleError as exc:
            print(f"  second release refused: {exc.message}")

        banner("8. Failed write-back keeps memory and dirty state")
        pool.release(c)
        pool.release(d)
        e = pool.fetch(3)
        pool.write(e, 0, b"\xff")
        pool.release(e)
        # Pin page 2 so the dirty page 3 is the only unpinned candidate and the
        # next fetch *must* try to evict it.
        pinned_2 = pool.fetch(2)
        show(pool, "page 3 dirty and unpinned; page 2 pinned")

        io.fail_writes = 1
        before = len(io.events)
        try:
            pool.fetch(4)
        except Exception as exc:  # noqa: BLE001 - intentional demonstration
            print(f"  fetch(page 4) failed: {type(exc).__name__}: {exc}")
        show(pool, "page 3 retained, still dirty; page 4 never loaded")
        print(f"  disk events for the attempt: {io.events[before:]}")

        z = pool.fetch(3)  # cache hit on the retained dirty page
        print(f"  refetch page 3: from_cache={z.from_cache}, byte0={pool.read(z, 0, 1).hex()}")
        pool.release(z)

        banner("9. Retry the eviction; write-back now succeeds")
        y = pool.fetch(4)  # page 3 is again the only unpinned frame
        print(f"  page 4 loaded, byte0={pool.read(y, 0, 1).hex()}")
        print(f"  page 3 byte0 on disk: {path.read_bytes()[3 * PAGE_SIZE]:#04x}")
        pool.release(y)
        pool.release(pinned_2)
        show(pool, "page 4 resident, page 3 gone")

        banner("10. flush_all only flushes unpinned dirty pages")
        p = pool.fetch(5)
        pool.write(p, 0, b"\x11")
        q = pool.fetch(6)
        pool.write(q, 0, b"\x22")
        pool.release(q)
        report = pool.flush_all()
        print(f"  flushed={report.flushed} skipped_pinned={report.skipped_pinned}")
        pool.release(p)
        report = pool.flush_all()
        print(f"  after release: flushed={report.flushed}")

        banner("11. close() refuses while pinned, then succeeds")
        r = pool.fetch(7)
        pool.write(r, 0, b"\x33")
        try:
            pool.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  close refused: {type(exc).__name__}: {exc}")
        print(f"  pool.closed={pool.closed}")
        pool.release(r)
        closing = pool.close()
        print(f"  closed: flushed={closing.flushed}, pool.closed={pool.closed}")
        print(f"  page 7 byte0 on disk: {path.read_bytes()[7 * PAGE_SIZE]:#04x}")

        banner("12. Independent check of the final file")
        data = path.read_bytes()
        print(f"  file size: {len(data)} bytes")
        for page_id in range(8):
            page = data[page_id * PAGE_SIZE : (page_id + 1) * PAGE_SIZE]
            print(f"  page {page_id}: first 4 bytes {page[:4].hex()}")
        print()
        print("All steps completed successfully.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
