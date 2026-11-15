"""Unit/integration tests for zarr_patch (run: python -m unittest -v).

These build small isolated Zarr v2 arrays in temp directories so they can
exercise paths the fixed fixtures cannot:

* an unaffected *existing* chunk that must be byte-copied without decoding;
* an edge chunk whose buffer contains non-fill padding beyond the shape;
* a missing chunk materialized from fill and then written once.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import numcodecs
import numpy as np
import zarr

from zarr_patch import apply_plan, compile_plan
from zarr_patch.errors import PatchValidationError
from zarr_patch.instrument import instrument_codec
from zarr_patch.verification import oracle_apply, verify_with_zarr


class ZarrPatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zarr-patch-test-")
        self.src = os.path.join(self.tmp, "source.zarr")
        self.out = os.path.join(self.tmp, "out.zarr")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_source(self, data, chunks=(3, 4), fill_value=-9, omit_chunks=()):
        arr = zarr.open_array(
            self.src,
            mode="w",
            shape=data.shape,
            chunks=chunks,
            dtype="<i4",
            fill_value=fill_value,
            order="C",
            compressor=numcodecs.Zlib(level=1),
        )
        arr[:] = data
        for key in omit_chunks:
            os.remove(os.path.join(self.src, key))
        return arr

    def assertRejected(self, patches):
        before = sorted(os.listdir(self.src))
        with self.assertRaises(PatchValidationError):
            plan = compile_plan(self.src, patches)
            apply_plan(plan, self.out)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".staging"))
        self.assertEqual(sorted(os.listdir(self.src)), before)


class ByteCopyAndMissingTests(ZarrPatchTestBase):
    def test_unaffected_existing_chunk_is_byte_copied(self):
        data = np.arange(7 * 9, dtype="<i4").reshape(7, 9)
        self.make_source(data)
        # Only touch chunk (0,0); every other existing chunk must be copied.
        patches = [
            {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": 12345},
        ]
        plan = compile_plan(self.src, patches)
        self.assertEqual(sorted(plan.chunk_plans), ["0.0"])

        with instrument_codec(self.src) as log:
            report = apply_plan(plan, self.out)

        touched = {r.key: r for r in report.touched}
        self.assertEqual(list(touched), ["0.0"])
        self.assertTrue(touched["0.0"].source_existed)
        self.assertEqual(touched["0.0"].decodes, 1)
        self.assertEqual(touched["0.0"].encodes, 1)

        grid = {f"{i}.{j}" for i in range(3) for j in range(3)}
        expected_copies = sorted(grid - {"0.0"})
        self.assertEqual(sorted(report.byte_copied_chunks), expected_copies)
        self.assertEqual(report.byte_copy_count, len(expected_copies))
        # Decodes and compressed-byte copies are counted separately.
        self.assertEqual(report.total_decodes, 1)
        self.assertEqual(log.decode_calls, 1)
        self.assertEqual(log.encode_calls, 1)

        for key in expected_copies:
            with open(os.path.join(self.src, key), "rb") as a, open(
                os.path.join(self.out, key), "rb"
            ) as b:
                self.assertEqual(a.read(), b.read(), f"{key} not byte-identical")

        expected = oracle_apply(self.src, patches)
        verify_with_zarr(self.out, expected, self.src)

    def test_missing_chunk_built_from_fill_and_written_once(self):
        data = np.arange(7 * 9, dtype="<i4").reshape(7, 9)
        self.make_source(data, omit_chunks=("0.1",))
        patches = [
            {"op": "rect", "rows": [0, 2], "cols": [4, 6], "value": 700},
        ]
        plan = compile_plan(self.src, patches)
        self.assertIn("0.1", plan.chunk_plans)
        report = apply_plan(plan, self.out)
        rec = next(r for r in report.touched if r.key == "0.1")
        self.assertFalse(rec.source_existed)
        self.assertEqual(rec.decodes, 0)
        self.assertEqual(rec.encodes, 1)
        # A previously missing, now touched chunk exists with real bytes.
        self.assertTrue(os.path.isfile(os.path.join(self.out, "0.1")))

        expected = oracle_apply(self.src, patches)
        verify_with_zarr(self.out, expected, self.src)

    def test_missing_untouched_chunk_stays_absent(self):
        data = np.full((7, 9), -9, dtype="<i4")
        self.make_source(data, omit_chunks=("1.2", "2.0", "2.1"))
        patches = [
            {"op": "points", "coords": [[0, 0]], "values": [1]},
        ]
        plan = compile_plan(self.src, patches)
        apply_plan(plan, self.out)
        for key in ("1.2", "2.0", "2.1"):
            self.assertFalse(os.path.exists(os.path.join(self.out, key)))
        expected = oracle_apply(self.src, patches)
        verify_with_zarr(self.out, expected, self.src)

    def test_all_noop_batch_writes_only_metadata(self):
        data = np.arange(7 * 9, dtype="<i4").reshape(7, 9)
        self.make_source(data)
        patches = [
            {"op": "rect", "rows": [0, 0], "cols": [0, 9], "value": 77},
            {"op": "points", "coords": [], "values": []},
        ]
        plan = compile_plan(self.src, patches)
        self.assertEqual(plan.noop_operations, [0, 1])
        self.assertEqual(plan.chunk_plans, {})
        report = apply_plan(plan, self.out)
        # No chunk is ever decoded/encoded; every existing chunk is copied.
        self.assertEqual(report.total_decodes, 0)
        self.assertEqual(report.total_encodes, 0)
        source_chunks = sorted(n for n in os.listdir(self.src) if not n.startswith("."))
        self.assertEqual(sorted(report.byte_copied_chunks), source_chunks)
        expected = oracle_apply(self.src, patches)
        verify_with_zarr(self.out, expected, self.src)

    def test_zattrs_copied_alongside_metadata(self):
        data = np.zeros((3, 3), dtype="<i4")
        self.make_source(data)
        with open(os.path.join(self.src, ".zattrs"), "w", encoding="utf-8") as fh:
            json.dump({"note": "preserve me"}, fh)
        patches = [{"op": "points", "coords": [[0, 0]], "values": [1]}]
        apply_plan(compile_plan(self.src, patches), self.out)
        self.assertTrue(os.path.isfile(os.path.join(self.out, ".zattrs")))
        with open(os.path.join(self.out, ".zattrs"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"note": "preserve me"})


class EdgeChunkTests(ZarrPatchTestBase):
    def test_edge_padding_not_treated_as_data(self):
        # 4x5 array with 3x4 chunks: chunk (1,1) covers rows 3..5 / cols 4..7
        # but only one real cell (3,4) is in shape.
        data = np.arange(20, dtype="<i4").reshape(4, 5)
        self.make_source(data, chunks=(3, 4))
        patches = [
            {"op": "points", "coords": [[3, 4]], "values": [555]},
        ]
        plan = compile_plan(self.src, patches)
        self.assertEqual(sorted(plan.chunk_plans), ["1.1"])
        report = apply_plan(plan, self.out)

        # The stored edge chunk is a full 3x4 buffer.
        z = zarr.open_array(self.out, mode="r")
        codec = numcodecs.get_codec(z.compressor.get_config())
        with open(os.path.join(self.out, "1.1"), "rb") as fh:
            chunk = np.frombuffer(codec.decode(fh.read()), dtype=z.dtype).reshape(z.chunks)
        self.assertEqual(chunk.shape, (3, 4))
        self.assertEqual(int(chunk[0, 0]), 555)  # the single in-shape cell

        expected = oracle_apply(self.src, patches)
        info = verify_with_zarr(self.out, expected, self.src)
        self.assertTrue(info["array_equals_oracle"])
        self.assertEqual(tuple(z.shape), (4, 5))
        self.assertEqual(report.touched[0].affected_cells, 1)

    def test_padding_sentinels_survive_but_remain_invisible(self):
        # Manually write an edge chunk whose out-of-shape padding carries
        # arbitrary sentinel values; the engine must neither expose nor alter
        # them through the array API, and in-shape cells must patch correctly.
        data = np.zeros((4, 4), dtype="<i4")
        arr = self.make_source(data, chunks=(3, 3))
        codec = numcodecs.Zlib(level=1)
        buf = np.full((3, 3), 123456, dtype="<i4")
        buf[:1, :1] = arr[3:, 3:]  # only (0,0) local maps to global (3,3)
        with open(os.path.join(self.src, "1.1"), "wb") as fh:
            fh.write(codec.encode(np.ascontiguousarray(buf)))
        patches = [{"op": "points", "coords": [[3, 3]], "values": [-100]}]
        plan = compile_plan(self.src, patches)
        apply_plan(plan, self.out)
        z = zarr.open_array(self.out, mode="r")
        self.assertEqual(int(z[3, 3]), -100)
        self.assertEqual(z[:].shape, (4, 4))
        expected = oracle_apply(self.src, patches)
        np.testing.assert_array_equal(z[:], expected)


class PriorityAndPlanTests(ZarrPatchTestBase):
    def test_operation_priority_and_repeated_points(self):
        data = np.zeros((5, 5), dtype="<i4")
        self.make_source(data, chunks=(2, 2), fill_value=0)
        patches = [
            {"op": "rect", "rows": [0, 3], "cols": [0, 3], "value": 1},
            {
                "op": "points",
                "coords": [[0, 0], [0, 0], [2, 2]],
                "values": [2, 3, 4],
            },
            {"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": 5},
            {"op": "rect", "rows": [4, 4], "cols": [0, 5], "value": 9},  # empty no-op
        ]
        plan = compile_plan(self.src, patches)
        self.assertEqual(plan.noop_operations, [3])
        report = apply_plan(plan, self.out)
        # Every touched chunk: one decode (all exist), one encode.
        for rec in report.touched:
            self.assertEqual(rec.decodes, 1)
            self.assertEqual(rec.encodes, 1)
        expected = oracle_apply(self.src, patches)
        z = zarr.open_array(self.out, mode="r")
        np.testing.assert_array_equal(z[:], expected)
        self.assertEqual(int(z[0, 0]), 5)   # last op wins over point value 3
        self.assertEqual(int(z[2, 2]), 4)
        self.assertEqual(int(z[1, 1]), 1)

    def test_rect_with_2d_values_crosses_chunks(self):
        data = np.zeros((4, 4), dtype="<i4")
        self.make_source(data, chunks=(2, 2))
        values = [[10, 11, 12], [13, 14, 15], [16, 17, 18]]
        patches = [{"op": "rect", "rows": [1, 4], "cols": [1, 4], "values": values}]
        plan = compile_plan(self.src, patches)
        self.assertEqual(len(plan.chunk_plans), 4)
        apply_plan(plan, self.out)
        expected = oracle_apply(self.src, patches)
        verify_with_zarr(self.out, expected, self.src)

    def test_plan_json_roundtrip_contents(self):
        data = np.zeros((3, 3), dtype="<i4")
        self.make_source(data, chunks=(2, 2))
        patches = [{"op": "points", "coords": [[0, 0], [0, 0]], "values": [1, 2]}]
        plan = compile_plan(self.src, patches)
        payload = json.loads(plan.to_json())
        self.assertEqual(payload["shape"], [3, 3])
        self.assertEqual(payload["chunks"], [2, 2])
        self.assertEqual(payload["touched_chunks"], ["0.0"])
        chunk0 = payload["chunk_plans"][0]
        self.assertEqual(chunk0["update_count"], 2)
        self.assertEqual(chunk0["affected_cell_count"], 1)
        self.assertEqual(chunk0["final_cells"], [[0, 0, 2]])
        # Update sequence retains both writes in order.
        self.assertEqual([u[2] for u in chunk0["updates"]], [1, 2])


class ValidationTests(ZarrPatchTestBase):
    def test_extra_validation_cases(self):
        cases = [
            [{"op": "rect", "rows": [0, 3], "cols": [0, 3], "value": 2_147_483_648}],
            [{"op": "rect", "rows": [0, 1], "cols": [0, 1], "values": [[True]]}],
            [{"op": "points", "coords": [[0, -1]], "values": [1]}],
            [{"op": "rect", "rows": [0, 1], "cols": [0, 1], "value": 1.0}],
            [{"op": "rect", "rows": [0, 1], "cols": [0, 1]}],
            [{"op": "points", "coords": "notalist", "values": []}],
        ]
        data = np.zeros((3, 3), dtype="<i4")
        self.make_source(data)
        for patches in cases:
            with self.subTest(patches=patches):
                self.assertRejected(patches)

    def test_non_zarr_source_rejected(self):
        other = os.path.join(self.tmp, "plain")
        os.makedirs(other)
        with self.assertRaises(PatchValidationError):
            compile_plan(other, [])


if __name__ == "__main__":
    unittest.main()
