#!/usr/bin/env python3
"""End-to-end driver: start mock_es and run the sink at two batch sizes.

For every batch size (2 and 4) it spins up a fresh mock server against a copy
of the fixtures, executes ``BulkSink.send`` against ``fixtures/records.jsonl``
and then runs ``verify`` against the server's ``/__inspect`` history. Both runs
must end with identical item-level semantics, and pre-existing newer server
data must survive.

Expected scenario outcomes (see fixtures/scenario.json):
  s1 confirmed (first bulk, then connection drop; proven by /_mget replay)
  s2 confirmed (same dropped prefix)
  s3 confirmed after one 429
  s4 rejected (mapping error, bad amount_cents type left untouched)
  s5 confirmed by /_mget (same version + same fingerprint replay)
  s6 conflict  (same version, different fingerprint)
  s7 superseded (target already holds version 5 > 2)
  s8 confirmed (version 2 strictly above existing version 1)
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import bulk_sink

ROOT = pathlib.Path(__file__).resolve().parent
RECORDS = ROOT / "fixtures" / "records.jsonl"
SCENARIO = ROOT / "fixtures" / "scenario.json"
MOCK = ROOT / "mock_es.py"

EXPECTED = {
    "s1": bulk_sink.CONFIRMED,
    "s2": bulk_sink.CONFIRMED,
    "s3": bulk_sink.CONFIRMED,
    "s4": bulk_sink.REJECTED,
    "s5": bulk_sink.CONFIRMED,
    "s6": bulk_sink.CONFLICT,
    "s7": bulk_sink.SUPERSEDED,
    "s8": bulk_sink.CONFIRMED,
}
# Final effective state the index must have afterwards (id -> version).
EXPECTED_INDEX = {"a": 1, "b": 2, "retry": 1, "newer": 5}


class MockServer:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.ready_path = root / "ready.json"
        self.log_path = root / "mock_es.log"

    def __enter__(self) -> str:
        self.log = open(self.log_path, "wb")
        # A stale ready file from a previous server on the same root would
        # otherwise be read before the new process writes its port.
        self.ready_path.unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK), "--port", "0",
             "--scenario", str(self.root / "scenario.json"),
             "--ready", str(self.ready_path)],
            stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.ready_path.exists():
                return json.loads(self.ready_path.read_text())["base_url"]
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"mock_es exited: {self.log_path.read_text()}")
            time.sleep(0.05)
        raise RuntimeError("mock_es did not become ready")

    def __exit__(self, *exc) -> None:
        assert self.proc is not None
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def _semantic_snapshot(summary: dict, inspect: dict) -> dict:
    """Comparable final semantics across batch sizes: terminal status per
    source record, server-side current index and full write history.

    Per-record dispatch counts are deliberately excluded: a record sitting
    behind a disconnect boundary legitimately loses a transport attempt at
    larger batch sizes even though the server never processed it. The attempt
    budget is asserted separately, and verify cross-checks the counts."""
    items = {it["source_id"]: (it["status"], it["reason"])
             for it in summary["items"]}
    docs = {d["id"]: {"version": d["version"],
                      "fingerprint": d["source"]["ingest_fingerprint"]}
            for d in inspect["docs"]}
    writes = sorted((w["id"], w["version"],
                     w["source"]["ingest_fingerprint"]) for w in inspect["writes"])
    return {"items": items, "docs": docs, "writes": writes}


def run_case(base_root: pathlib.Path, batch_size: int) -> dict:
    work_dir = base_root / f"work-bs{batch_size}"
    server = MockServer(base_root)
    with server as base_url:
        sink = bulk_sink.BulkSink(base_url, work_dir,
                                  batch_size=batch_size, retry_interval=0.05)
        sink.load(RECORDS)
        summary = sink.send()
        report = bulk_sink.verify(base_url, work_dir)
        inspect = _inspect(base_url)

    failures: list[str] = []
    if not report["ok"]:
        failures.extend(f"verify: {e}" for e in report["errors"])

    by_id = {it["source_id"]: it for it in summary["items"]}
    for sid, expected_status in EXPECTED.items():
        actual = by_id[sid]["status"]
        if actual != expected_status:
            failures.append(f"{sid}: expected {expected_status}, got {actual}")
        if by_id[sid]["attempts"] > bulk_sink.MAX_WRITE_ATTEMPTS:
            failures.append(f"{sid}: attempt budget exceeded")
    if summary["counts"].get(bulk_sink.PENDING):
        failures.append("records still pending at end")

    inspect_docs = {d["id"]: d for d in inspect["docs"]}
    for doc_id, version in EXPECTED_INDEX.items():
        d = inspect_docs.get(doc_id)
        if d is None or d["version"] != version:
            failures.append(f"index/{doc_id}: expected current version {version}, "
                            f"got {None if d is None else d['version']}")

    # The pre-existing newer document (version 5) must never be overwritten.
    if inspect_docs["newer"]["source"]["amount_cents"] != 500:
        failures.append("pre-existing newer record was overwritten")

    # s4's original wrong-typed value must have been submitted as-is.
    bad_attempts = [a for a in inspect["attempts"] if a["id"] == "bad"]
    if not bad_attempts or bad_attempts[0]["source"].get("amount_cents") != "wrong":
        failures.append("mapping-failure record was not submitted with original value")
    if len(bad_attempts) != 1:
        failures.append(f"permanent mapping error was retried ({len(bad_attempts)}x)")

    # Attempt-level expectations for the controlled failure injections.
    retry_attempts = [a for a in inspect["attempts"] if a["id"] == "retry"]
    if len(retry_attempts) != 2:
        failures.append(f"retry id expected 2 server attempts, got {len(retry_attempts)}")
    if not inspect["drop_done"]:
        failures.append("scenario's first-bulk disconnect was not exercised")

    snapshot = _semantic_snapshot(summary, inspect)
    return {"batch_size": batch_size, "failures": failures,
            "summary": summary, "verify": report, "inspect": inspect,
            "snapshot": snapshot}


def _inspect(base_url: str) -> dict:
    with urllib.request.urlopen(base_url + "/__inspect", timeout=5) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bulk-sink-demo-") as tmp:
        base_root = pathlib.Path(tmp)
        # Independent copy: fixtures themselves are never rewritten.
        shutil.copy(SCENARIO, base_root / "scenario.json")
        results = [run_case(base_root, bs) for bs in (2, 4)]

        for result in results:
            print(f"\n=== batch_size={result['batch_size']} ===")
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
            print("verify checks:")
            for line in result["verify"]["checks"]:
                print("  -", line)
            print("server attempts:", len(result["inspect"]["attempts"]),
                  "server writes:", len(result["inspect"]["writes"]))

        ok = all(not r["failures"] for r in results)
        if results[0]["snapshot"] != results[1]["snapshot"]:
            ok = False
            print("\nSEMANTIC MISMATCH between batch sizes:")
            print(json.dumps({"bs2": results[0]["snapshot"],
                              "bs4": results[1]["snapshot"]},
                             ensure_ascii=False, indent=2))
        else:
            print("\nSemantics identical across batch_size=2 and batch_size=4.")

        all_failures = [(r["batch_size"], f) for r in results for f in r["failures"]]
        if all_failures:
            print("\nFAILURES:")
            for bs, msg in all_failures:
                print(f"  [bs={bs}] {msg}")
            return 1
        print("\nAll scenario expectations passed.")
        print(f"Artifacts kept under the temporary work roots in {tmp} "
              "(receipts, bulk rounds, mget evidence, status).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
