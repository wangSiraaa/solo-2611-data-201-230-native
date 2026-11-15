#!/usr/bin/env python3
"""End-to-end harness for the zarr_patch engine.

Checks performed:

1. Valid fixture batch (cross-chunk rects, repeated points, later overrides,
   missing chunks, empty selection):
   - compiled plan contents and update counts;
   - apply report (decode/encode keys, source existence, update counts,
     byte copies kept separate from decodes);
   - dense NumPy oracle replaying operations in original order;
   - reopen via the zarr library: metadata, missing-chunk fill, edge values;
   - byte-for-byte identity of unaffected existing chunks;
   - independent instrumentation of the real numcodecs codec entry points
     proving every touched chunk decodes/encodes exactly once.
2. Invalid batches (out of bounds, int32 overflow, value shape errors,
   booleans as integers, ...): every batch is rejected before any output
   directory appears, and the source stays untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zarr_patch import apply_plan, compile_plan
from zarr_patch.errors import PatchValidationError
from zarr_patch.instrument import instrument_codec
from zarr_patch.verification import (
    load_patches,
    oracle_apply,
    verify_byte_copies,
    verify_with_zarr,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "fixtures", "source.zarr")
PATCHES = os.path.join(ROOT, "fixtures", "patches.json")
INVALID_PATCHES = os.path.join(ROOT, "fixtures", "invalid-patches.json")
OUT_DIR = os.path.join(ROOT, "out")

# Each case must be rejected and must not leave an output behind.
EXTRA_INVALID_CASES = {
    "point_row_out_of_bounds": [
        {"op": "points", "coords": [[7, 0]], "values": [3]}
    ],
    "point_col_out_of_bounds": [
        {"op": "points", "coords": [[0, 9]], "values": [3]}
    ],
    "rect_stop_out_of_bounds": [
        {"op": "rect", "rows": [0, 8], "cols": [0, 1], "value": 1}
    ],
    "negative_index": [
        {"op": "rect", "rows": [-1, 1], "cols": [0, 1], "value": 1}
    ],
    "scalar_overflow_high": [
        {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": 2_147_483_648}
    ],
    "scalar_overflow_low": [
        {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": -2_147_483_649}
    ],
    "point_value_overflow": [
        {"op": "points", "coords": [[0, 0]], "values": [2_147_483_648]}
    ],
    "rect_values_wrong_shape": [
        {
            "op": "rect",
            "rows": [0, 2],
            "cols": [0, 2],
            "values": [[1, 2, 3], [4, 5, 6]],
        }
    ],
    "rect_values_ragged": [
        {"op": "rect", "rows": [0, 2], "cols": [0, 2], "values": [[1], [2, 3]]}
    ],
    "rect_values_not_2d": [
        {"op": "rect", "rows": [0, 1], "cols": [0, 2], "values": [1, 2]}
    ],
    "points_length_mismatch": [
        {"op": "points", "coords": [[0, 0], [1, 1]], "values": [1]}
    ],
    "bool_value_rejected": [
        {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": True}
    ],
    "bool_index_rejected": [
        {"op": "points", "coords": [[True, 0]], "values": [1]}
    ],
    "unknown_op": [
        {"op": "frobnicate", "rows": [0, 1], "cols": [0, 1], "value": 1}
    ],
    "both_value_and_values": [
        {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": 1, "values": [[2]]}
    ],
    "start_after_stop": [
        {"op": "rect", "rows": [2, 1], "cols": [0, 1], "value": 1}
    ],
    "empty_rect_wrong_values_shape": [
        {"op": "rect", "rows": [0, 0], "cols": [0, 9], "values": [[1]]}
    ],
}


def snapshot_source() -> dict[str, bytes]:
    snap = {}
    for name in sorted(os.listdir(SOURCE)):
        path = os.path.join(SOURCE, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                snap[name] = fh.read()
    return snap


def assert_source_untouched(before: dict[str, bytes]) -> None:
    after = snapshot_source()
    assert before == after, "source directory was modified"


def run_valid_case() -> None:
    print("=" * 72)
    print("VALID BATCH")
    print("=" * 72)
    patches = load_patches(PATCHES)
    plan = compile_plan(SOURCE, patches)

    plan_path = os.path.join(OUT_DIR, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        fh.write(plan.to_json())
    print(f"plan written: {plan_path}")
    print(
        f"  operations={plan.operation_count} noop={plan.noop_operations} "
        f"touched_chunks={sorted(plan.chunk_plans)} total_updates={plan.total_update_count}"
    )
    for key in sorted(plan.chunk_plans):
        cp = plan.chunk_plans[key]
        print(
            f"  {key}: updates={cp.update_count} distinct_cells={cp.affected_cell_count}"
        )

    output = os.path.join(OUT_DIR, "result.zarr")
    assert not os.path.exists(output)

    with instrument_codec(SOURCE) as log:
        report = apply_plan(plan, output)

    report_path = os.path.join(OUT_DIR, "apply-report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    print(f"\napply report written: {report_path}")
    for rec in report.touched:
        print(
            f"  {rec.key}: source_existed={rec.source_existed} decodes={rec.decodes} "
            f"encodes={rec.encodes} updates={rec.update_count} cells={rec.affected_cells}"
        )
    print(
        f"  byte-copied (no decode): {report.byte_copied_chunks} "
        f"| missing kept absent: {report.missing_untouched_absent}"
    )
    print(
        f"  totals: decodes={report.total_decodes} encodes={report.total_encodes} "
        f"byte_copies={report.byte_copy_count} updates={report.total_updates}"
    )

    # Independent numcodecs entry-point instrumentation.
    existing_touched = [r.key for r in report.touched if r.source_existed]
    print(f"\nreal numcodecs entry points: decode x{log.decode_calls}, encode x{log.encode_calls}")
    assert log.decode_calls == len(existing_touched), (
        f"expected {len(existing_touched)} real decodes, got {log.decode_calls}"
    )
    assert log.encode_calls == len(report.touched), (
        f"expected {len(report.touched)} real encodes, got {log.encode_calls}"
    )
    for rec in report.touched:
        assert rec.decodes == (1 if rec.source_existed else 0), rec
        assert rec.encodes == 1, rec
        assert rec.decodes <= 1 and rec.encodes <= 1

    # Dense oracle on an independent array.
    expected = oracle_apply(SOURCE, patches)
    verification = verify_with_zarr(output, expected, SOURCE)
    verify_byte_copies(SOURCE, output, report.byte_copied_chunks)
    print("\nzarr reopen verification:")
    for k, v in verification.items():
        print(f"  {k}: {v}")

    # Missing untouched chunks must remain absent on disk.
    on_disk = {n for n in os.listdir(output) if not n.startswith(".")}
    assert set(report.missing_untouched_absent).isdisjoint(on_disk)
    print("\nALL VALID-CASE CHECKS PASSED")


def run_invalid_cases() -> None:
    print("\n" + "=" * 72)
    print("INVALID BATCHES (must be rejected before any chunk is written)")
    print("=" * 72)
    before = snapshot_source()

    fixture_cases = {"fixtures/invalid-patches.json": load_patches(INVALID_PATCHES)}
    all_cases = {**fixture_cases, **EXTRA_INVALID_CASES}

    for name, patches in all_cases.items():
        out = os.path.join(OUT_DIR, f"invalid-{name.replace('/', '_')}.zarr")
        staging = out + ".staging"
        assert not os.path.exists(out) and not os.path.exists(staging)
        try:
            plan = compile_plan(SOURCE, patches)
            apply_plan(plan, out)
        except PatchValidationError as exc:
            assert not os.path.exists(out), f"{name}: output published despite rejection"
            assert not os.path.exists(staging), f"{name}: staging dir left behind"
            print(f"  REJECTED {name:38s} -> {exc}")
        else:
            raise AssertionError(f"{name}: invalid batch was accepted")
        assert_source_untouched(before)

    # Pre-existing output path must also be refused without touching it.
    guarded = os.path.join(OUT_DIR, "guarded.zarr")
    os.makedirs(guarded)
    marker = os.path.join(guarded, "marker")
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("keep me")
    try:
        plan = compile_plan(SOURCE, load_patches(PATCHES))
        apply_plan(plan, guarded)
    except PatchValidationError as exc:
        assert os.path.isfile(marker), "existing output directory was modified"
        print(f"  REJECTED existing-output-path             -> {exc}")
    else:
        raise AssertionError("existing output path was overwritten")
    shutil.rmtree(guarded)
    assert_source_untouched(before)
    print("\nALL INVALID-CASE CHECKS PASSED; SOURCE BYTE-IDENTICAL")


def main() -> int:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR)
    run_valid_case()
    run_invalid_cases()
    print("\nDONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
