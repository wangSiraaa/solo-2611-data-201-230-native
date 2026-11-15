#!/usr/bin/env python3
"""
run_scenarios.py -- run the four fixture stages against one mirror save
directory, then verify everything independently.

Stages (one mock server at a time on the same port, same save dir):
  initial   UIDVALIDITY 900, UIDs 11/12/13            -> snapshot #1
  shift     UID 11 expunged after SEARCH, 12 reflagged -> snapshot #2
  reset     UIDVALIDITY 901, UID 11 reused (new mail)  -> new generation
  truncated literal cut mid-connection                 -> old snapshot intact
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT / "run"
SAVE = RUN / "mirror"
LOGS = RUN / "logs"
EXPORT = RUN / "export"
PORT = 11430

sys.path.insert(0, str(ROOT))
import verify_mirror  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label}  {detail}")


def start_server(stage):
    log = LOGS / f"{stage}.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "fixtures" / "mock_imap.py"),
         "--stage", stage, "--port", str(PORT), "--log", str(log)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"mock server for stage {stage} died at startup")
    ready = json.loads(line)
    assert ready.get("ready") and ready["stage"] == stage, ready
    return proc


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(0.2)


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, str(ROOT / "imap_mirror.py"), *argv,
         "--save-dir", str(SAVE), "--port", str(PORT)],
        capture_output=True, text=True, timeout=60)


def sync():
    r = run_cli("sync")
    report = json.loads(r.stdout)
    return r.returncode, report


def main():
    subprocess.run(["pkill", "-f", "mock_imap.py"], capture_output=True)
    time.sleep(0.3)
    if RUN.exists():
        shutil.rmtree(RUN)
    LOGS.mkdir(parents=True)

    # ---------------------------------------------------------- initial
    print("== stage: initial ==")
    proc = start_server("initial")
    code, rep = sync()
    stop_server(proc)
    check("initial: sync exit 0", code == 0, f"exit={code}")
    check("initial: published gen 900 snapshot #1",
          rep["published"] == {"generation": 900, "snapshot_id": 1},
          f"published={rep['published']}")
    check("initial: searched [11,12,13] twice",
          rep["search_first"] == [11, 12, 13] and rep["search_recheck"] == [11, 12, 13],
          f"{rep['search_first']} / {rep['search_recheck']}")
    check("initial: fetched all three UIDs",
          sorted(f["uid"] for f in rep["fetched"]) == [11, 12, 13])
    check("initial: nothing vanished/unconfirmed",
          rep["vanished"] == [] and rep["unconfirmed"] == [])

    # ---------------------------------------------------------- shift
    print("== stage: shift (expunge after SEARCH, sequence numbers move) ==")
    proc = start_server("shift")
    code, rep = sync()
    stop_server(proc)
    check("shift: sync exit 0", code == 0, f"exit={code}")
    check("shift: published gen 900 snapshot #2",
          rep["published"] == {"generation": 900, "snapshot_id": 2},
          f"published={rep['published']}")
    check("shift: first search saw 11, recheck sees only [12,13]",
          rep["search_first"] == [11, 12, 13] and rep["search_recheck"] == [12, 13],
          f"{rep['search_first']} / {rep['search_recheck']}")
    check("shift: UID 11 recorded vanished (archived original kept)",
          any(v["uid"] == 11 and v["archived"] for v in rep["vanished"]),
          f"vanished={rep['vanished']}")
    check("shift: UID 12 flags change reported (order-insensitive)",
          any(f["uid"] == 12 and f["old"] == ["\\Seen"]
              and sorted(f["new"]) == ["\\Flagged", "\\Seen"]
              for f in rep["flags_changes"]),
          f"flags_changes={rep['flags_changes']}")
    check("shift: fetched UIDs bound by explicit UID, not position",
          sorted(f["uid"] for f in rep["fetched"]) == [12, 13],
          f"fetched={rep['fetched']}")
    check("shift: EXPUNGE noticed transiently (seq only, not identity)",
          rep["expunged_seq_transient"] == [1], f"{rep['expunged_seq_transient']}")

    # ---------------------------------------------------------- reset
    print("== stage: reset (UIDVALIDITY 900 -> 901, UID 11 reused) ==")
    proc = start_server("reset")
    code, rep = sync()
    stop_server(proc)
    check("reset: sync exit 0", code == 0, f"exit={code}")
    check("reset: published NEW generation 901 snapshot #1",
          rep["published"] == {"generation": 901, "snapshot_id": 1},
          f"published={rep['published']}")
    check("reset: uidvalidity reported as 901", rep["identity"]["uidvalidity"] == 901)

    # ---------------------------------------------------------- truncated
    print("== stage: truncated (literal cut mid-connection) ==")
    before = (SAVE / "gen-900" / "current.json").read_bytes()
    proc = start_server("truncated")
    code, rep = sync()
    stop_server(proc)
    check("truncated: sync exit 3 (failed, nothing published)", code == 3, f"exit={code}")
    check("truncated: error recorded, published is null",
          rep["error"] is not None and rep["published"] is None,
          f"error={rep['error']}")
    check("truncated: limited retries exhausted (3 attempts)",
          len(rep["attempts"]) == 3, f"attempts={rep['attempts']}")
    after = (SAVE / "gen-900" / "current.json").read_bytes()
    check("truncated: previous snapshot byte-identical afterwards", before == after)

    # ---------------------------------------------------------- status / export
    print("== status & export entry points ==")
    r = run_cli("status", "--json")
    status = json.loads(r.stdout)
    check("status: current generation is 901", status["current_generation"] == 901)
    check("status: both generations present",
          sorted(status["generations"]) == ["900", "901"])
    check("status: last error from truncated sync is visible",
          status["last_error"] is not None and "uidvalidity" in status["last_error"])
    check("status: gen-900 shows UID 11 vanished",
          "11" in status["generations"]["900"]["vanished"])
    r = run_cli("status")
    print(r.stdout)

    r = run_cli("export", "--out", str(EXPORT / "gen-901"))
    check("export gen-901 exit 0", r.returncode == 0, r.stderr)
    r = run_cli("export", "--out", str(EXPORT / "gen-900"), "--generation", "900")
    check("export gen-900 exit 0", r.returncode == 0, r.stderr)

    # ---------------------------------------------------------- independent verify
    print("\n== independent verification (server logs + raw fixture bytes) ==")
    failures.extend(verify_mirror.verify_all(SAVE, LOGS, EXPORT))

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURES"))
    for f in failures:
        print(" -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
