#!/usr/bin/env python3
"""End-to-end verification against fixtures/mock_broker.py.

Runs the real scripted broker and the real receiver, then cross-checks:
  * the broker's own packet log (broker-log.jsonl) -- never mock internals;
  * the receiver's SQLite protocol events / deliveries / business records.

Modes:
  recovery    two receiver *processes* share one database (process restart)
  inprocess   one receiver process performs both connections itself
  reset       broker Session Present=false again -> new session generation

Usage: python3 run_demo.py [mode ...] [--keep-artifacts]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCENARIO_BROKER = ROOT / "tests" / "scenario_broker.py"
PY = sys.executable


def wait_for_ready(proc: subprocess.Popen, timeout: float = 10.0) -> int:
    """Wait for the fixture's stdout 'ready' line and return its real port.

    A TCP readiness probe would itself be accept()-ed by the scripted broker and
    consume one of its exactly-N sessions, so readiness is read from stdout.
    Binding port 0 lets the kernel assign a free port, avoiding cross-run
    collisions with brokers from a previous (possibly crashed) demo run.
    """
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("broker exited before becoming ready: "
                               + proc.stdout.read())
        if '"ready": true' in line:
            return int(json.loads(line)["port"])
    raise RuntimeError("timed out waiting for broker readiness")


def start_broker_fixture(scenario: str, log_path: Path,
                         connections: int | None = None,
                         skip: int = 0) -> tuple[subprocess.Popen, int]:
    """Start the parameterized fixture broker (tests/scenario_broker.py).

    The broker binds port 0 (kernel-assigned); its real port is returned.
    connections=N limits it to N scripted sessions and skip=M starts at
    scenario session M, which lets one receiver *process* handle exactly one
    connection so a second process demonstrates recovery from SQLite while the
    packet log keeps its original session ordinals.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(SCENARIO_BROKER),
           "--scenario", scenario, "--port", "0",
           "--log", str(log_path)]
    if connections:
        cmd += ["--only", str(connections)]
    if skip:
        cmd += ["--skip", str(skip)]
    proc = subprocess.Popen(
        cmd, cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    actual_port = wait_for_ready(proc)
    return proc, actual_port


def receiver_cmd(db: Path, port: int, once: bool,
                 max_connections: int = 0,
                 crash_before_pubcomp: bool = False) -> list[str]:
    cmd = [PY, "-m", "receiver", "receive", "--db", str(db),
           "--port", str(port), "--wait-broker", "8"]
    if once:
        cmd.append("--once")
    if max_connections:
        cmd += ["--max-connections", str(max_connections)]
    if crash_before_pubcomp:
        cmd.append("--crash-before-pubcomp")
    return cmd


def run_receiver(db: Path, port: int, once: bool,
                 max_connections: int = 0,
                 crash_before_pubcomp: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        receiver_cmd(db, port, once, max_connections, crash_before_pubcomp),
        cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(("  PASS " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def assert_database(db: Path, expected_business: int, expected_deliveries: int,
                    expected_payloads: list[str], failures: list[str],
                    expected_generations: int, open_handshakes: int = 0) -> None:
    out = subprocess.run(
        [PY, "-m", "receiver", "inspect", "--db", str(db), "--json"],
        cwd=ROOT, text=True, capture_output=True, check=True)
    state = json.loads(out.stdout)

    check(state["counts"]["business_records"] == expected_business,
          f"business_records == {expected_business} (got "
          f"{state['counts']['business_records']})", failures)
    check(state["counts"]["deliveries"] == expected_deliveries,
          f"deliveries == {expected_deliveries} (got "
          f"{state['counts']['deliveries']})", failures)
    check(state["counts"]["sessions"] == expected_generations,
          f"session generations == {expected_generations}", failures)
    check(state["counts"]["open_handshakes"] == open_handshakes,
          f"no open handshakes left (got {state['counts']['open_handshakes']})",
          failures)

    payloads = [r["payload"] for r in state["business_records"]]
    check(payloads == expected_payloads,
          f"business payloads in order == {expected_payloads} (got {payloads})",
          failures)
    seqs = [r["business_seq"] for r in state["business_records"]]
    check(len(seqs) == len(set(seqs)), "business_seq values are unique", failures)
    return state


def crosscheck_broker_log(broker_log: Path, failures: list[str],
                          expected_sessions: int) -> None:
    events = read_jsonl(broker_log)
    # type numbers: 3 PUBLISH 5 PUBREC 6 PUBREL 7 PUBCOMP; 1 CONNECT 8 SUBSCRIBE
    by_type = {}
    for e in events:
        if "type" in e:
            by_type.setdefault(e["type"], []).append(e)

    check(len({e["session"] for e in events if "session" in e})
          == expected_sessions,
          f"broker saw {expected_sessions} session connections", failures)
    check(len(by_type.get(5, [])) == 3,
          "broker received 3 PUBREC (1st publish + publish + its DUP retry)",
          failures)
    check(len(by_type.get(7, [])) == 2,
          "broker received 2 PUBCOMP (one per completed delivery)", failures)
    # The broker only logs sent PUBLISH/PUBREL and received PUBREC/PUBCOMP here.
    broker_publishes = [e for e in by_type.get(3, []) if e["direction"] == "sent"]
    check(len(broker_publishes) == 3,
          "broker sent 3 PUBLISH packets (two distinct deliveries, one DUP)",
          failures)
    broker_pubrels = [e for e in by_type.get(6, []) if e["direction"] == "sent"]
    check(len(broker_pubrels) == 3,
          "broker sent 3 PUBREL (one redelivered after disconnect)", failures)

    # Every receiver-side outbound ack must exist on the wire as seen by broker.
    check(all(e["direction"] == "received" for e in by_type.get(5, [])),
          "all PUBREC entries in broker log are receiver->broker", failures)
    check(all(e["direction"] == "received" for e in by_type.get(7, [])),
          "all PUBCOMP entries in broker log are receiver->broker", failures)


def verify_ordering(state: dict, failures: list[str]) -> None:
    ev = state["protocol_events"]
    def idx(predicate):
        return next(i for i, e in enumerate(ev) if predicate(e))

    # PUBREC must never precede the durable PUBLISH save for the same delivery.
    publishes = [e for e in ev if e["packet_type"] == "PUBLISH"]
    pubrecs = [e for e in ev if e["packet_type"] == "PUBREC"]
    check(len(publishes) == 3 and len(pubrecs) == 3,
          "receiver logged 3 PUBLISH and 3 PUBREC events", failures)
    for rec in pubrecs:
        owner = next(e for e in publishes
                     if e["delivery_id"] == rec["delivery_id"])
        check(ev.index(owner) < ev.index(rec),
              f"PUBLISH committed before PUBREC for delivery {rec['delivery_id']}",
              failures)

    # Business record creation must coincide with the first PUBREL, never PUBLISH.
    commits = [e for e in ev if e["event"] == "business_record_committed"]
    check(len(commits) == 2,
          "exactly two business_record_committed events (one per delivery)",
          failures)
    for c in commits:
        # The matching PUBREL is the one for the same delivery; the redelivered
        # PUBREL of delivery 1 must not be matched against delivery 2.
        rel = next(e for e in ev
                   if e["packet_type"] == "PUBREL"
                   and e["delivery_id"] == c["delivery_id"])
        check(ev.index(rel) <= ev.index(c),
              f"business row for delivery {c['delivery_id']} created at PUBREL "
              "time (no PUBLISH-time insert)", failures)

    # The redelivered PUBREL must be logged as duplicate and create no row.
    dup_rel = [e for e in ev if e["event"] == "duplicate_pubrel_ignored"]
    check(len(dup_rel) == 1,
          "redelivered PUBREL recorded once as duplicate_pubrel_ignored",
          failures)
    check(any(e["event"] == "duplicate_publish_ignored" for e in ev),
          "DUP PUBLISH recorded as duplicate_publish_ignored", failures)

    # Each delivery reaches completed exactly once per delivery_id. A
    # redelivered PUBREL may retransmit PUBCOMP (extra completing->completed
    # events for the same delivery) but must not complete a second delivery.
    completed_ids = {e["delivery_id"] for e in ev
                     if e["event"] == "pubcomp_written_completed"}
    delivery_ids = {e["delivery_id"] for e in ev
                    if e["event"] == "new_publish_saved"}
    check(completed_ids == delivery_ids and len(completed_ids) == 2,
          "each of the two deliveries reached completed (per delivery_id, "
          "PUBCOMP retransmissions do not add a completion)", failures)
    completed_events = [e for e in ev
                        if e["event"] == "pubcomp_written_completed"]
    ids = {e["packet_id"] for e in completed_events}
    check(ids == {7}, "both completed deliveries used packet identifier 7",
          failures)


def mode_recovery(work: Path, failures: list[str]) -> None:
    print("\n=== mode: recovery (two receiver processes, one database) ===")
    db = work / "recovery.db"
    broker_log = work / "broker-recovery-1.jsonl"
    broker, port = start_broker_fixture("standard", broker_log, connections=1)
    # Fault injection: this process hard-exits AFTER the durable 'completing'
    # marker commits and BEFORE PUBCOMP is written (exit code 77 by design).
    proc = run_receiver(db, port, once=True, crash_before_pubcomp=True)
    broker.wait(timeout=15)
    print(proc.stdout)

    state1 = json.loads(subprocess.run(
        [PY, "-m", "receiver", "inspect", "--db", str(db), "--json"],
        cwd=ROOT, text=True, capture_output=True, check=True).stdout)
    # The business transaction from PUBREL is durable, but PUBCOMP never left
    # the process; the row is parked at 'completing' awaiting retransmission.
    check(state1["counts"]["business_records"] == 1,
          "after crash: the PUBREL business record is already durable",
          failures)
    stuck = state1["deliveries"][0]
    check(stuck["state"] == "completing" and stuck["packet_id"] == 7,
          "handshake parked at 'completing' (PUBCOMP never written)",
          failures)
    check(state1["counts"]["open_handshakes"] == 1,
          "one open handshake survives the process exit", failures)

    broker_log2 = work / "broker-recovery-2.jsonl"
    # Only scenario session 1 (Session Present=true); a fresh process handles it.
    broker, port2 = start_broker_fixture("standard", broker_log2,
                                         connections=1, skip=1)
    # Second process uses the *same* database file: true process-restart recovery.
    proc = run_receiver(db, port2, once=True)
    broker.wait(timeout=15)
    print(proc.stdout)

    merged = work / "broker-recovery-all.jsonl"
    merged.write_text(broker_log.read_text() + broker_log2.read_text())
    state = assert_database(
        db, expected_business=2, expected_deliveries=2,
        expected_payloads=['{"sensor":"A","value":17}',
                           '{"sensor":"A","value":23}'],
        failures=failures, expected_generations=1)
    check(any(e["event"] == "generation_resumed" for e in state["protocol_events"]),
          "second process resumed generation on Session Present=true", failures)
    verify_ordering(state, failures)
    crosscheck_broker_log(merged, failures, expected_sessions=2)


def mode_inprocess(work: Path, failures: list[str]) -> None:
    print("\n=== mode: inprocess (one receiver process reconnects itself) ===")
    db = work / "inprocess.db"
    broker_log = work / "broker-inprocess.jsonl"
    broker, port = start_broker_fixture("standard", broker_log)
    proc = run_receiver(db, port, once=False,
                        max_connections=2)  # reconnect loop, 2 connections
    broker.wait(timeout=20)
    print(proc.stdout)
    state = assert_database(
        db, expected_business=2, expected_deliveries=2,
        expected_payloads=['{"sensor":"A","value":17}',
                           '{"sensor":"A","value":23}'],
        failures=failures, expected_generations=1)
    verify_ordering(state, failures)
    crosscheck_broker_log(broker_log, failures, expected_sessions=2)


def mode_reset(work: Path, failures: list[str]) -> None:
    print("\n=== mode: reset (Session Present=false -> new generation) ===")
    # First finish the standard scenario so completed business rows exist.
    db = work / "reset.db"
    broker, port = start_broker_fixture("standard",
                                        work / "broker-reset-1.jsonl")
    print(run_receiver(db, port, once=False, max_connections=2).stdout)
    broker.wait(timeout=20)

    # Then the reset scenario starts a brand-new session on the same database.
    broker, port2 = start_broker_fixture(
        "reset", work / "broker-reset-2.jsonl")
    print(run_receiver(db, port2, once=False, max_connections=1).stdout)
    broker.wait(timeout=20)

    state = json.loads(subprocess.run(
        [PY, "-m", "receiver", "inspect", "--db", str(db), "--json"],
        cwd=ROOT, text=True, capture_output=True, check=True).stdout)
    check(state["counts"]["sessions"] == 2,
          "two session generations recorded", failures)
    check(state["counts"]["business_records"] == 3,
          "old completed business rows kept + new delivery added (3 total)",
          failures)
    payloads = [r["payload"] for r in state["business_records"]]
    check(payloads == ['{"sensor":"A","value":17}',
                       '{"sensor":"A","value":23}',
                       '{"sensor":"A","value":29}'],
          "business payload order spans both generations", failures)
    gens = {r["generation"] for r in state["deliveries"]}
    check(gens == {1, 2}, "deliveries belong to two generations", failures)
    check(any(e["event"] == "generation_started"
              and e["generation"] == 2 for e in state["protocol_events"]),
          "second CONNACK false started generation 2", failures)
    check(state["counts"]["open_handshakes"] == 0,
          "no handshakes remain open across generations", failures)

    # Export command spot-check.
    exported = subprocess.run(
        [PY, "-m", "receiver", "export", "--db", str(db)],
        cwd=ROOT, text=True, capture_output=True, check=True).stdout
    rows = [json.loads(line) for line in exported.splitlines()]
    check(len(rows) == 3 and all(r["payload_sha256"] for r in rows),
          "export emits 3 jsonl business rows with payload digests", failures)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("modes", nargs="*",
                        default=["recovery", "inprocess", "reset"])
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args()

    work = ROOT / "out" / "demo"
    if work.exists():
        # Fresh databases each run: assertions count rows for exactly this run.
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for mode in args.modes:
        {"recovery": mode_recovery,
         "inprocess": mode_inprocess,
         "reset": mode_reset}[mode](work, failures)

    print("\n=== export csv smoke test ===")
    subprocess.run([PY, "-m", "receiver", "export",
                    "--db", str(work / "inprocess.db"), "--format", "csv"],
                   cwd=ROOT, check=True)

    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("  all cross-checks passed")
    if not args.keep_artifacts:
        print(f"  artifacts kept under {work} for manual inspection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
