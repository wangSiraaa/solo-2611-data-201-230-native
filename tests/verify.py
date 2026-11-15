#!/usr/bin/env python3
"""Independent verification for aggregate.py.

Checks the tool against:
  * a Python Counter/set oracle, and
  * an independent SQLite query over the same rows (used only for checking;
    the tool itself never hands GROUP BY to a database).

Also exercises the required edge cases, error paths, and determinism.
Run:  python3 tests/verify.py
"""
from __future__ import annotations

import json
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "aggregate.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cents_of(amount: str | None) -> int | None:
    if amount is None:
        return None
    negative = amount.startswith("-")
    body = amount[1:] if negative else amount
    if "." in body:
        whole, frac = body.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = body, "00"
    value = int(whole) * 100 + int(frac)
    return -value if negative else value


def fmt(value: int | None) -> str | None:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def gsort(group: str | None) -> tuple[int, bytes]:
    return (0, b"") if group is None else (1, group.encode("utf-8"))


def oracle(rows: list[dict]) -> list[dict]:
    state: dict = {}
    for row in rows:
        st = state.setdefault(row["group"], [0, 0, False, set(), set()])
        st[0] += 1
        value = cents_of(row["amount"])
        if value is not None:
            st[1] += value
            st[2] = True
        if row["user"] is not None:
            st[3].add(row["user"])
        if row["user"] is not None and row["product"] is not None:
            st[4].add((row["user"], row["product"]))
    out = []
    for group in sorted(state, key=gsort):
        count, total, has_total, users, pairs = state[group]
        out.append(
            {
                "group": group,
                "count": count,
                "sum": fmt(total) if has_total else None,
                "user_distinct": len(users),
                "pair_distinct": len(pairs),
            }
        )
    return out


def sqlite_oracle(rows: list[dict]) -> list[dict]:
    """Same rows through SQLite, including exact integer-cent arithmetic."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE r (row_id INTEGER PRIMARY KEY, grp TEXT, usr TEXT, "
        "prod TEXT, amount TEXT, cents INTEGER)"
    )
    for row in rows:
        con.execute(
            "INSERT INTO r VALUES (?,?,?,?,?,?)",
            (
                row["row_id"],
                row["group"],
                row["user"],
                row["product"],
                row["amount"],
                cents_of(row["amount"]),
            ),
        )
    base = {
        grp: (count, total, users)
        for grp, count, total, users in con.execute(
            "SELECT grp, COUNT(*), SUM(cents), COUNT(DISTINCT usr) "
            "FROM r GROUP BY grp"
        )
    }
    pairs = {
        grp: count
        for grp, count in con.execute(
            "SELECT grp, COUNT(*) FROM "
            "(SELECT DISTINCT grp, usr, prod FROM r "
            " WHERE usr IS NOT NULL AND prod IS NOT NULL) "
            "GROUP BY grp"
        )
    }
    out = []
    for grp, (count, total, users) in sorted(base.items(), key=lambda kv: gsort(kv[0])):
        out.append(
            {
                "group": grp,
                "count": count,
                "sum": fmt(total) if total is not None else None,
                "user_distinct": users,
                "pair_distinct": pairs.get(grp, 0),
            }
        )
    con.close()
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_tool(
    rows: list[dict], limit: int, fan_in: int, tmp: Path
) -> tuple[list[dict], dict]:
    input_path = tmp / "rows.jsonl"
    config_path = tmp / "config.json"
    out_dir = tmp / "out"
    write_jsonl(input_path, rows)
    config_path.write_text(
        json.dumps({"state_entry_limit": limit, "merge_fan_in": fan_in})
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--input",
            str(input_path),
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"tool failed: {proc.returncode}\n{proc.stderr}")
    results = [json.loads(line) for line in (out_dir / "results.jsonl").read_text().splitlines()]
    report = json.loads((out_dir / "report.json").read_text())
    assert report["spill"]["max_resident_entries"] <= limit, report["spill"]
    assert report["spill"]["max_open_runs_in_merge"] <= fan_in, report["spill"]
    assert report["input_rows"] == len(rows)
    return results, report


def expect_failure(rows: list[dict], limit: int, fan_in: int, tmp: Path, label: str) -> None:
    input_path = tmp / "rows.jsonl"
    config_path = tmp / "config.json"
    out_dir = tmp / "out-fail"
    write_jsonl(input_path, rows)
    config_path.write_text(json.dumps({"state_entry_limit": limit, "merge_fan_in": fan_in}))
    proc = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--input",
            str(input_path),
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, (label, proc.returncode, proc.stdout, proc.stderr)
    assert "error:" in proc.stderr, (label, proc.stderr)


def check(rows: list[dict], limit: int, fan_in: int, label: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    try:
        results, report = run_tool(rows, limit, fan_in, tmp)
        expected = oracle(rows)
        assert results == expected, f"{label}: oracle mismatch\n{results}\n{expected}"
        sql_expected = sqlite_oracle(rows)
        assert results == sql_expected, f"{label}: sqlite mismatch\n{results}\n{sql_expected}"
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_tmp(rows: list[dict], limit: int, fan_in: int) -> tuple[list[dict], dict]:
    """Like run_tool but always removes its temporary directory."""
    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    try:
        return run_tool(rows, limit, fan_in, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

def fixture_rows() -> list[dict]:
    return [json.loads(line) for line in (ROOT / "fixtures" / "rows.jsonl").read_text().splitlines()]


def edge_rows() -> list[dict]:
    rows: list[dict] = []
    rid = 0

    def add(group, user, product, amount):
        nonlocal rid
        rows.append(
            {
                "row_id": rid,
                "group": group,
                "user": user,
                "product": product,
                "amount": amount,
            }
        )
        rid += 1

    # NULL group is a group; NULL/empty-string distinctions.
    add(None, None, None, None)
    add(None, "u1", "p1", "0.10")
    add(None, "", "", "-0.10")  # empty string is a real distinct key
    add(None, "u1", "p1", "1")  # duplicate pair, integer amount
    add("", None, None, None)
    add("", "", "p1", "2.5")  # one decimal digit
    add("", "", "p1", "-2.50")
    add("only-null-amount", None, None, None)
    add("only-null-amount", "u", "p", None)
    add("cancel", "u", "p", "5.00")
    add("cancel", "u", "p", "-5.00")
    add("cancel", "u2", None, "-0.00")
    add("neg", "u", "p", "-0.01")
    add("neg", "u", "p2", "-1.99")
    # UTF-8 ordering: ASCII < non-ASCII; ensure byte-order not code-point order
    add("zz", "u", "p", "0.01")
    add("é", "u", "p", "0.02")
    add("日", "u", "p", "0.03")
    return rows


def hot_rows(n: int = 300) -> list[dict]:
    """One hot group with many distinct keys, plus duplicates across spills."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "row_id": i,
                "group": "hot" if i % 7 else "cold",
                "user": f"u{i % 53}",
                "product": f"p{i % 41}",
                "amount": "0.01" if i % 2 else "-0.01",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    fixture = fixture_rows()
    edge = edge_rows()
    hot = hot_rows()

    # 1. Required default run: budget 16, fan-in 3 must spill and merge in
    #    multiple rounds for the provided fixtures.
    report = check(fixture, 16, 3, "fixture/default")
    assert report["spill"]["initial_runs"] > 1
    assert report["spill"]["merge_rounds"] >= 2
    print(
        "PASS fixture/default: runs=%d rounds=%d max_resident=%d"
        % (
            report["spill"]["initial_runs"],
            report["spill"]["merge_rounds"],
            report["spill"]["max_resident_entries"],
        )
    )

    # 2. Same input, different budgets / fan-ins -> identical logical output.
    baseline = oracle(fixture)
    for limit in (1, 2, 3, 5, 8, 16, 32, 64, 1000):
        for fan_in in (2, 3, 4, 8):
            got, rep = check_tmp(fixture, limit, fan_in)
            assert got == baseline, (limit, fan_in)
            assert rep["spill"]["max_resident_entries"] <= limit
    print("PASS budget/fan-in matrix (limit 1..1000, fan-in 2..8)")

    # 3. Input order independence.
    rng = random.Random(20261115)
    for trial in range(6):
        shuffled = fixture[:]
        rng.shuffle(shuffled)
        got, rep = check_tmp(shuffled, 8, 3)
        assert got == baseline, f"order trial {trial}"
        assert rep["spill"]["merge_rounds"] >= 2
    print("PASS input-order independence (6 shuffles, limit 8, fan-in 3)")

    # 4. Edge values: NULL/empty string, all-NULL amounts, cancellation.
    for limit, fan_in in ((1, 2), (2, 3), (16, 3), (1000, 4)):
        check(edge, limit, fan_in, f"edge/limit={limit}")
    print("PASS edge cases: NULL/empty-string, all-NULL amount, cancellation, UTF-8 order")

    # 5. One hot group whose distinct sets far exceed the budget; must spill
    #    keys while keeping the accumulator bounded, and dedupe across runs.
    for limit, fan_in in ((1, 2), (4, 2), (16, 3)):
        rep = check(hot, limit, fan_in, f"hot/limit={limit}")
        assert rep["spill"]["max_resident_entries"] <= limit
        assert rep["spill"]["initial_runs"] > 1
        assert rep["spill"]["merge_rounds"] >= 1
    print("PASS hot group over budget (single group, 53 users / 41 products)")

    # 6. Empty input.
    got, rep = check_tmp([], 16, 3)
    assert got == [], got
    assert rep["groups_output"] == 0
    assert rep["spill"]["initial_runs"] == 0
    print("PASS empty input")

    # 7. Determinism: repeating a run yields byte-identical results.
    tmp = Path(tempfile.mkdtemp())
    try:
        run_tool(fixture, 16, 3, tmp)
        first = (tmp / "out" / "results.jsonl").read_bytes()
        run_tool(fixture, 16, 3, tmp)
        second = (tmp / "out" / "results.jsonl").read_bytes()
        assert first == second
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS deterministic output")

    # 8. Failure modes.
    fail_tmp = Path(tempfile.mkdtemp())
    try:
        good = [
            {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": "1.00"}
        ]
        cases = {
            "duplicate row_id": [
                good[0],
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": "2.00"},
            ],
            "three decimals": [
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": "1.005"}
            ],
            "non-decimal amount": [
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": "abc"}
            ],
            "float amount": [
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": 1.0}
            ],
            "leading plus": [
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": "+1.00"}
            ],
            "missing leading digit": [
                {"row_id": 1, "group": "g", "user": "u", "product": "p", "amount": ".50"}
            ],
            "non-string group": [
                {"row_id": 1, "group": 1, "user": "u", "product": "p", "amount": "1.00"}
            ],
            "non-string user": [
                {"row_id": 1, "group": "g", "user": 1, "product": "p", "amount": "1.00"}
            ],
            "non-string product": [
                {"row_id": 1, "group": "g", "user": "u", "product": [], "amount": "1.00"}
            ],
            "bool row_id": [
                {"row_id": True, "group": "g", "user": "u", "product": "p", "amount": "1.00"}
            ],
            "missing field": [
                {"row_id": 1, "group": "g", "user": "u", "amount": "1.00"}
            ],
            "extra field": [
                {
                    "row_id": 1,
                    "group": "g",
                    "user": "u",
                    "product": "p",
                    "amount": "1.00",
                    "x": 1,
                }
            ],
        }
        for label, rows in cases.items():
            expect_failure(rows, 16, 3, fail_tmp, label)
        for label, limit, fan_in in (
            ("limit zero", 0, 3),
            ("fan-in one", 16, 1),
            ("bool limit", True, 3),
            ("float fan-in", 16, 2.5),
        ):
            expect_failure(good, limit, fan_in, fail_tmp, label)
    finally:
        shutil.rmtree(fail_tmp, ignore_errors=True)
    print("PASS failure modes: bad fields, duplicate row_id, bad config")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
