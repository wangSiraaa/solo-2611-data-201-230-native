#!/usr/bin/env python3
"""Run every fixture scenario and cross-check the collector independently.

For each mode this script:

1. starts the local synthetic Modbus device with a fresh request log,
2. runs ``plan`` and ``collect`` through the public CLI,
3. stops the device and parses its request log,
4. verifies the *actual* merged data intervals against the plan,
5. re-decodes each point's raw registers with plain ``struct`` (no collector
   decoding code) from hand-picked bytes, and
6. checks the scenario-specific quality expectations.

Exit status is non-zero if any cross-check fails.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modbus_collector.planner import build_plan  # noqa: E402  (planning only)
from modbus_collector.points import load_point_table  # noqa: E402

FIXTURES = ROOT / "fixtures"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_simulator(mode: str, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()
    process = subprocess.Popen(
        [
            sys.executable,
            str(FIXTURES / "mock_modbus.py"),
            "--mode",
            mode,
            "--port",
            str(port),
            "--log",
            str(log_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            out, err = process.communicate()
            raise RuntimeError(
                f"simulator {mode} exited early:\n{out.decode()}\n{err.decode()}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.05)
    process.kill()
    raise RuntimeError(f"simulator {mode} did not start on port {port}")


def _stop_simulator(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "modbus_collector", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# -- request-log parsing ----------------------------------------------------
def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _group_rounds(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split log entries into whole rounds between version-register fences."""
    rounds: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        current.append(entry)
        if entry["start"] == 100 and len(current) > 1:
            rounds.append(current)
            current = []
    if current:
        rounds.append(current)
    return rounds


# -- independent decoding ---------------------------------------------------
_STRUCT_FORMAT = {
    "u16": ">H",
    "i16": ">h",
    "u32": ">I",
    "i32": ">i",
    "float32": ">f",
}


def independent_decode(point: dict[str, Any], registers: list[int]) -> dict[str, Any]:
    """Re-order bytes exactly as the fixture README describes, then struct."""
    words = list(registers)
    if point["width"] == 2 and point["word_order"] == "little":
        words = [words[1], words[0]]

    raw_parts: list[bytes] = []
    for word in words:
        packed = struct.pack(">H", word)
        raw_parts.append(packed if point["byte_order"] == "big" else packed[::-1])
    raw = b"".join(raw_parts)
    value = struct.unpack(_STRUCT_FORMAT[point["type"]], raw)[0]
    if point["type"] == "float32" and not math.isfinite(value):
        return {"raw_bytes": raw, "kind": "nonfinite", "value": None}
    scale = Decimal(str(point["scale"]))
    if scale == 1:
        scaled: Any = value
    else:
        scaled = float(Decimal(value) * scale) if isinstance(value, int) else value * float(scale)
    return {
        "raw_bytes": raw,
        "kind": "float" if point["type"] == "float32" else "integer",
        "value": scaled,
    }


def _values_match(actual: Any, expected: Any) -> bool:
    if isinstance(actual, float) or isinstance(expected, float):
        return (
            isinstance(actual, (int, float))
            and isinstance(expected, (int, float))
            and math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-9)
        )
    return actual == expected


# -- scenario expectations --------------------------------------------------
EXPECTED = {
    "normal": {"quality": "coherent", "complete": True, "rounds": 1},
    "change-once": {"quality": "coherent", "complete": True, "rounds": 2},
    "changing": {"quality": "incoherent", "complete": False, "rounds": 3},
    "exception": {"quality": "degraded", "complete": False, "rounds": 1},
    "wrong-tid": {"quality": "degraded", "complete": False, "rounds": 1},
    "nonfinite": {"quality": "coherent", "complete": True, "rounds": 1},
}


def run_scenario(
    mode: str,
    points_path: Path,
    out_root: Path,
    retries: int,
) -> dict[str, Any]:
    scenario_dir = out_root / mode
    scenario_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    log_path = scenario_dir / "requests.jsonl"
    collect_path = scenario_dir / "collect.json"
    plan_path = scenario_dir / "plan.json"

    process = _start_simulator(mode, port, log_path)
    try:
        plan_run = _run_cli(
            ["plan", "--points", str(points_path), "--output", str(plan_path)]
        )
        if plan_run.returncode != 0:
            raise RuntimeError(f"plan failed: {plan_run.stderr}")
        collect_run = _run_cli(
            [
                "collect",
                "--points",
                str(points_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--retries",
                str(retries),
                "--output",
                str(collect_path),
            ]
        )
    finally:
        _stop_simulator(process)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = json.loads(collect_path.read_text(encoding="utf-8")) if collect_path.exists() else {}
    entries = _read_log(log_path)
    rounds = _group_rounds(entries)

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    data_segments = [
        (request["start"], request["count"])
        for request in plan["requests_per_round"]
        if request["role"] == "data"
    ]
    check(
        "plan_has_no_duplicate_segments",
        len(data_segments) == len(set(data_segments)),
        data_segments,
    )
    check(
        "plan_avoids_forbidden_6_8",
        all(not (start < 8 and 6 < start + count) for start, count in data_segments),
        data_segments,
    )
    check(
        "plan_respects_max_registers",
        all(1 <= count <= plan["max_registers"] for _, count in data_segments),
        data_segments,
    )
    for start, count in data_segments:
        inside = [
            p
            for p in plan["points"]
            if p["address"] < start + count and start < p["address"] + p["width"]
        ]
        check(
            f"request_{start}_contains_no_partial_point",
            all(
                start <= p["address"] and p["address"] + p["width"] <= start + count
                for p in inside
            ),
            {"segment": [start, count], "intersecting_points": [p["id"] for p in inside]},
        )

    # Actual wire intervals per round must equal the planned ones exactly.
    actual_rounds = []
    for index, round_entries in enumerate(rounds):
        wire = [
            (entry["start"], entry["count"])
            for entry in round_entries
            if entry["start"] != 100
        ]
        actual_rounds.append(wire)
        check(
            f"round_{index}_wire_segments_match_plan",
            wire == data_segments,
            {"wire": wire, "planned": data_segments},
        )
        check(
            f"round_{index}_fences_present",
            round_entries[0]["start"] == 100 and round_entries[-1]["start"] == 100,
            [entry["start"] for entry in round_entries],
        )
        transaction_ids = [entry["transaction_id"] for entry in round_entries]
        check(
            f"round_{index}_transaction_ids_sequential",
            transaction_ids
            == list(range(transaction_ids[0], transaction_ids[0] + len(transaction_ids))),
            transaction_ids,
        )

    check("rounds_executed", len(rounds) == EXPECTED[mode]["rounds"], len(rounds))

    # Independent point decoding from the collector's raw registers.
    point_defs = {point["id"]: point for point in plan["points"]}
    independent: dict[str, Any] = {}
    for record in result.get("points", []):
        definition = point_defs[record["id"]]
        if record.get("raw_registers") is None:
            independent[record["id"]] = {"status": "no_raw_registers"}
            continue
        expected = independent_decode(definition, record["raw_registers"])
        independent[record["id"]] = {
            "status": "decoded",
            "raw_bytes_hex": expected["raw_bytes"].hex(),
            "expected_value": expected["value"],
            "collector_value": record.get("value"),
        }
        check(
            f"point_{record['id']}_raw_bytes_match_struct",
            record.get("raw_bytes_hex") == expected["raw_bytes"].hex(),
            {
                "collector": record.get("raw_bytes_hex"),
                "independent": expected["raw_bytes"].hex(),
            },
        )
        if expected["kind"] == "nonfinite":
            check(
                f"point_{record['id']}_nonfinite_not_a_number",
                record.get("value") is None and record.get("valid") is False,
                {
                    "value": record.get("value"),
                    "valid": record.get("valid"),
                    "raw_bytes": expected["raw_bytes"].hex(),
                },
            )
        else:
            check(
                f"point_{record['id']}_value_matches_struct",
                _values_match(record.get("value"), expected["value"]),
                {
                    "collector": record.get("value"),
                    "independent": expected["value"],
                },
            )

    quality = result.get("quality", {}).get("name")
    check(
        "round_quality_matches_expectation",
        quality == EXPECTED[mode]["quality"],
        {"actual": quality, "expected": EXPECTED[mode]["quality"]},
    )
    check(
        "complete_snapshot_matches_expectation",
        result.get("complete_snapshot") == EXPECTED[mode]["complete"],
        {
            "actual": result.get("complete_snapshot"),
            "expected": EXPECTED[mode]["complete"],
        },
    )
    check(
        "every_point_carries_its_round_fence",
        all(
            record.get("version_fence", {}).get("round") == result.get("selected_round")
            for record in result.get("points", [])
        ),
        [
            (record["id"], record.get("version_fence"))
            for record in result.get("points", [])
        ],
    )

    records = {record["id"]: record for record in result.get("points", [])}
    if mode == "exception":
        check(
            "exception_marks_only_address_8_region",
            records["temperature"]["quality"] == "exception"
            and records["temperature"]["error"]["exception_code"] == 2
            and records["signed"]["valid"]
            and records["swapped"]["valid"],
            {
                "temperature": records["temperature"].get("error"),
                "signed_quality": records["signed"].get("quality"),
                "swapped_quality": records["swapped"].get("quality"),
            },
        )
        check(
            "exception_keeps_unrelated_data",
            records["temperature"]["raw_registers"] is None
            and records["temperature"]["value"] is None,
            records["temperature"].get("raw_registers"),
        )
    if mode == "wrong-tid":
        kinds = [m["kind"] for m in result.get("response_mismatches", [])]
        check("wrong_tid_detected_as_transaction_id", kinds == ["transaction_id"], kinds)
        first_exchange = result["requests"][0]["exchange"]
        check(
            "wrong_tid_value_not_attributed",
            first_exchange is not None
            and first_exchange["registers"] is None
            and not first_exchange["ok"],
            first_exchange,
        )
        # The rejected frame carried transaction id 3; the collector must not
        # let the *next* legitimate response with id 3 be mistaken for it.
        log_entries = entries
        first_data_request_tid = log_entries[1]["transaction_id"]
        check(
            "wrong_tid_not_reused_as_later_request",
            first_data_request_tid == 2,
            log_entries[:3],
        )
        check(
            "wrong_tid_affected_points_marked",
            records["signed"]["quality"].startswith("response_")
            and records["counter"]["quality"].startswith("response_")
            and records["alias-low"]["quality"].startswith("response_"),
            {
                "signed": records["signed"]["quality"],
                "counter": records["counter"]["quality"],
                "alias-low": records["alias-low"]["quality"],
            },
        )
        check(
            "wrong_tid_unrelated_data_survives",
            records["swapped"]["valid"] and records["temperature"]["valid"],
            {
                "swapped": records["swapped"]["value"],
                "temperature": records["temperature"]["value"],
            },
        )
    if mode == "change-once":
        round_versions = [
            (round_data["version_before"], round_data["version_after"])
            for round_data in result.get("rounds", [])
        ]
        check(
            "change_once_first_round_incoherent_second_coherent",
            len(round_versions) == 2
            and round_versions[0][0] != round_versions[0][1]
            and round_versions[1][0] == round_versions[1][1]
            and result["rounds"][1]["coherent"],
            round_versions,
        )
    if mode == "changing":
        check(
            "changing_never_claims_complete_snapshot",
            all(not r["quality"]["complete_snapshot"] for r in result.get("rounds", [])),
            [r["quality"] for r in result.get("rounds", [])],
        )
        check(
            "changing_keeps_observations",
            all(record["raw_registers"] is not None for record in result.get("points", [])),
            {k: v.get("raw_registers") for k, v in records.items()},
        )

    summary = {
        "mode": mode,
        "port": port,
        "collect_exit_code": collect_run.returncode,
        "quality": quality,
        "complete_snapshot": result.get("complete_snapshot"),
        "rounds_executed": len(rounds),
        "planned_data_segments": data_segments,
        "actual_round_segments": actual_rounds,
        "version_fence": result.get("version_fence"),
        "checks": checks,
        "passed": all(item["ok"] for item in checks),
        "independent_decode": independent,
        "files": {
            "plan": str(plan_path.relative_to(ROOT)),
            "collect": str(collect_path.relative_to(ROOT)),
            "requests": str(log_path.relative_to(ROOT)),
        },
    }
    (scenario_dir / "verification.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", default=str(FIXTURES / "points.json"))
    parser.add_argument("--out", default=str(ROOT / "out" / "scenarios"))
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--modes",
        nargs="*",
        default=["normal", "change-once", "changing", "exception", "wrong-tid", "nonfinite"],
    )
    args = parser.parse_args(argv)

    points_path = Path(args.points)
    out_root = Path(args.out)
    summaries = []
    for mode in args.modes:
        print(f"=== {mode} ===", flush=True)
        summary = run_scenario(mode, points_path, out_root, args.retries)
        summaries.append(summary)
        print(
            f"quality={summary['quality']} complete={summary['complete_snapshot']} "
            f"rounds={summary['rounds_executed']} "
            f"{'PASS' if summary['passed'] else 'FAIL'}",
            flush=True,
        )
        for item in summary["checks"]:
            if not item["ok"]:
                print(f"  FAILED: {item['check']} -> {item['detail']}", flush=True)

    report = {
        "all_passed": all(summary["passed"] for summary in summaries),
        "scenarios": summaries,
    }
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nall_passed={report['all_passed']}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
