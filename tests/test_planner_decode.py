"""Unit tests for planning and decoding (no network I/O)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modbus_collector.decode import decode_registers  # noqa: E402
from modbus_collector.errors import ConfigError, PlanError  # noqa: E402
from modbus_collector.planner import build_plan  # noqa: E402
from modbus_collector.points import load_point_table, point_table_from_dict  # noqa: E402

FIXTURE = ROOT / "fixtures" / "points.json"


def fixture_table():
    return load_point_table(FIXTURE)


def table_dict(**overrides):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw


class PlanTests(unittest.TestCase):
    def test_fixture_plan_merges_and_respects_limits(self):
        plan = build_plan(fixture_table())
        segments = [(r.start, r.count) for r in plan.data_requests]
        self.assertEqual(segments, [(0, 3), (4, 2), (8, 2)])
        for request in plan.data_requests:
            self.assertLessEqual(request.count, 4)
            self.assertGreaterEqual(request.count, 1)
        self.assertEqual(plan.requested_registers, 7)

    def test_forbidden_range_is_not_crossed(self):
        plan = build_plan(fixture_table())
        for request in plan.data_requests:
            self.assertFalse(request.start < 8 and 6 < request.end)

    def test_alias_registers_read_once(self):
        plan = build_plan(fixture_table())
        self.assertEqual(plan.unique_point_registers, 7)
        self.assertEqual(plan.declared_point_registers, 8)
        self.assertEqual(plan.requested_registers, plan.unique_point_registers)
        interpretations = plan.as_dict()["merge"]["register_interpretations"]
        self.assertEqual(interpretations["2"], ["counter", "alias-low"])

    def test_request_ranges_disjoint(self):
        plan = build_plan(fixture_table())
        ranges = sorted((r.start, r.end) for r in plan.data_requests)
        for (start_a, end_a), (start_b, _) in zip(ranges, ranges[1:]):
            self.assertLessEqual(end_a, start_b)

    def test_wire_order_starts_and_ends_with_fences(self):
        order = build_plan(fixture_table()).request_order()
        self.assertEqual(order[0]["role"], "fence-before")
        self.assertEqual(order[-1]["role"], "fence-after")
        self.assertEqual(order[0]["start"], 100)
        self.assertEqual(order[-1]["start"], 100)
        self.assertEqual(
            [item["sequence"] for item in order], list(range(len(order)))
        )

    def test_merges_adjacent_points_into_one_request(self):
        raw = table_dict(
            max_registers=8,
            forbidden_ranges=[],
            points=[
                {"id": "a", "address": 0, "type": "u16"},
                {"id": "b", "address": 1, "type": "u16"},
                {"id": "c", "address": 2, "type": "u16"},
            ],
        )
        plan = build_plan(point_table_from_dict(raw))
        self.assertEqual([(r.start, r.count) for r in plan.data_requests], [(0, 3)])

    def test_splits_when_max_registers_reached(self):
        raw = table_dict(
            max_registers=2,
            forbidden_ranges=[],
            version_register=100,
            points=[
                {"id": "a", "address": 0, "type": "u16"},
                {"id": "b", "address": 1, "type": "u16"},
                {"id": "c", "address": 2, "type": "u16"},
            ],
        )
        plan = build_plan(point_table_from_dict(raw))
        self.assertEqual([(r.start, r.count) for r in plan.data_requests], [(0, 2), (2, 1)])

    def test_never_splits_a_32bit_point(self):
        raw = table_dict(
            max_registers=2,
            forbidden_ranges=[],
            points=[
                {"id": "a", "address": 0, "type": "u16"},
                {"id": "wide", "address": 1, "type": "u32"},
            ],
        )
        # A request starting at 0 may not include the u32 at 1..3 (count 3 >
        # max 2), so the planner must start a new request at 1 instead of
        # splitting the 32-bit value across requests.
        plan = build_plan(point_table_from_dict(raw))
        self.assertEqual([(r.start, r.count) for r in plan.data_requests], [(0, 1), (1, 2)])
        for request in plan.data_requests:
            self.assertLessEqual(request.count, 2)

    def test_rejects_point_straddling_forbidden_range(self):
        raw = table_dict(
            points=[{"id": "wide", "address": 5, "type": "u32"}],
            forbidden_ranges=[[6, 8]],
        )
        with self.assertRaises(ConfigError):
            point_table_from_dict(raw)

    def test_rejects_point_wider_than_max_registers(self):
        raw = table_dict(
            max_registers=1,
            points=[{"id": "wide", "address": 0, "type": "u32"}],
            forbidden_ranges=[],
        )
        with self.assertRaises(PlanError):
            build_plan(point_table_from_dict(raw))

    def test_rejects_invalid_type_and_orders(self):
        for bad in (
            {"id": "x", "address": 0, "type": "u64"},
            {"id": "x", "address": 0, "type": "u16", "byte_order": "middle"},
            {"id": "x", "address": 0, "type": "u16", "word_order": "little"},
        ):
            raw = table_dict(points=[bad], forbidden_ranges=[])
            with self.assertRaises(ConfigError):
                point_table_from_dict(raw)

    def test_rejects_non_decimal_scale(self):
        raw = table_dict(
            points=[{"id": "x", "address": 0, "type": "u16", "scale": "bad"}],
            forbidden_ranges=[],
        )
        with self.assertRaises(ConfigError):
            point_table_from_dict(raw)

    def test_rejects_version_register_inside_a_point(self):
        raw = table_dict(
            version_register=1,
            points=[{"id": "wide", "address": 0, "type": "u32"}],
            forbidden_ranges=[],
        )
        with self.assertRaises(ConfigError):
            point_table_from_dict(raw)


class DecodeTests(unittest.TestCase):
    def point(self, **overrides):
        data = {
            "id": "p",
            "address": 0,
            "type": "u16",
            "byte_order": "big",
            "word_order": "big",
            "scale": "1",
        }
        data.update(overrides)
        return point_table_from_dict(
            table_dict(points=[data], forbidden_ranges=[], version_register=100)
        ).points[0]

    def test_i16_negative(self):
        kind, raw, value = decode_registers(self.point(type="i16"), [65534])
        self.assertEqual(kind, "integer")
        self.assertEqual(raw, b"\xff\xfe")
        self.assertEqual(value, -2)

    def test_u32_big(self):
        kind, raw, value = decode_registers(self.point(type="u32"), [1, 2])
        self.assertEqual(raw, b"\x00\x01\x00\x02")
        self.assertEqual(value, 65538)

    def test_u32_word_little(self):
        kind, raw, value = decode_registers(
            self.point(type="u32", word_order="little"), [1, 2]
        )
        self.assertEqual(raw, b"\x00\x02\x00\x01")
        self.assertEqual(value, 131073)

    def test_u32_byte_little(self):
        kind, raw, value = decode_registers(
            self.point(type="u32", byte_order="little"), [1, 2]
        )
        self.assertEqual(raw, b"\x01\x00\x02\x00")
        self.assertEqual(value, 0x01000200)

    def test_float32_word_little(self):
        # Registers [0x0000, 0x3fa0] with word_order little → bytes 3fa00000.
        kind, raw, value = decode_registers(
            self.point(type="float32", word_order="little"), [0, 16288]
        )
        self.assertEqual(kind, "float")
        self.assertEqual(raw, b"\x3f\xa0\x00\x00")
        self.assertEqual(value, 1.25)

    def test_float32_nan_preserves_bytes(self):
        kind, raw, value = decode_registers(
            self.point(type="float32", word_order="little"), [0, 0x7FC0]
        )
        self.assertEqual(kind, "nan-or-inf")
        self.assertEqual(raw, b"\x7f\xc0\x00\x00")
        self.assertIsNone(value)

    def test_float32_infinity(self):
        kind, raw, value = decode_registers(self.point(type="float32"), [0x7F80, 0])
        self.assertEqual(kind, "nan-or-inf")
        self.assertEqual(raw, b"\x7f\x80\x00\x00")
        self.assertIsNone(value)

    def test_i32_little_byte(self):
        kind, raw, value = decode_registers(
            self.point(type="i32", byte_order="little"), [0xFEFF, 0xF9DB]
        )
        self.assertEqual(raw, b"\xff\xfe\xdb\xf9")
        self.assertEqual(value, -74759)

    def test_decimal_scale(self):
        kind, raw, value = decode_registers(
            self.point(type="u16", scale="0.1"), [2]
        )
        self.assertAlmostEqual(value, 0.2)

    def test_decode_wrong_register_count(self):
        with self.assertRaises(ConfigError):
            decode_registers(self.point(type="u32"), [1])


if __name__ == "__main__":
    unittest.main()
