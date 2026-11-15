"""Tests for the restricted relational ETL executor.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from provenance_etl import EtlError, PlanError, SourceError, run, trace, verify


class Workspace:
    """Temporary fixture workspace (never touches the repository fixtures/)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def table(self, name: str, rows: list[dict]) -> str:
        path = os.path.join(self.root, f"{name}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def plan(self, plan: dict, name: str = "plan.json") -> str:
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(plan, handle)
        return path


def source(node_id: str, table: str, path: str) -> dict:
    return {"id": node_id, "op": "source", "table": table, "path": path}


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)


class SamplePlanTest(BaseCase):
    """The documented sample: duplicates, both union branches, two right rows."""

    def setUp(self) -> None:
        super().setUp()
        self.payments = self.ws.table(
            "payments",
            [
                {"row_id": "p1", "account": "A", "amount": 10, "status": "ok"},
                {"row_id": "p2", "account": "A", "amount": 10, "status": "ok"},
                {"row_id": "p3", "account": "A", "amount": None, "status": "ok"},
                {"row_id": "p4", "account": "B", "amount": 8, "status": "bad"},
                {"row_id": "p5", "account": None, "amount": 9, "status": "ok"},
            ],
        )
        self.buckets = self.ws.table(
            "buckets",
            [
                {"row_id": "b1", "account": "A", "region": "N"},
                {"row_id": "b2", "account": "A", "region": "N"},
                {"row_id": "b3", "account": None, "region": "X"},
            ],
        )
        self.plan = {
            "nodes": [
                source("p", "payments", self.payments),
                source("b", "buckets", self.buckets),
                {"id": "ok", "op": "filter", "input": "p", "field": "status", "cmp": "eq", "value": "ok"},
                {"id": "positive", "op": "filter", "input": "p", "field": "amount", "cmp": "gt", "value": 0},
                {"id": "u", "op": "union_all", "left": "ok", "right": "positive"},
                {
                    "id": "j",
                    "op": "inner_join",
                    "left": "u",
                    "right": "b",
                    "left_key": "account",
                    "right_key": "account",
                    "left_prefix": "p_",
                    "right_prefix": "b_",
                },
                {
                    "id": "g",
                    "op": "group",
                    "input": "j",
                    "keys": ["b_region"],
                    "aggregates": [
                        {"name": "n", "fn": "count"},
                        {"name": "amount", "fn": "sum", "field": "p_amount"},
                    ],
                },
            ],
            "output": "g",
        }
        self.plan_path = self.ws.plan(self.plan)

    def test_result_matches_hand_computation(self):
        report = run(self.plan_path, os.path.join(self.ws.root, "out"))
        self.assertEqual(report["row_count"], 1)
        self.assertEqual(report["rows"][0]["values"], {"b_region": "N", "n": 10, "amount": 80})

    def test_verify_replays_and_matches_sqlite(self):
        report = verify(self.plan_path)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["sqlite"], [{"b_region": "N", "n": 10, "amount": 80}])
        for group in report["groups"]:
            for info in group["aggregates"].values():
                self.assertTrue(info["ok"])
                self.assertEqual(info["actual"], info["replayed"])

    def test_trace_preserves_duplicate_contributions(self):
        report = trace(self.plan_path)
        group = report["groups"][0]
        count = group["aggregates"]["n"]
        total = group["aggregates"]["amount"]

        self.assertEqual(count["value"], 10)
        self.assertEqual(count["contribution_count"], 10)
        # P1 and P2 have identical business values but remain distinct records.
        records = {r["row_id"]: r for r in count["source_records"]}
        self.assertEqual(records["p1"]["occurrences"], 4)
        self.assertEqual(records["p2"]["occurrences"], 4)
        # p1 entered through both union branches.
        branches = {step["detail"] for path in records["p1"]["paths"] for step in path["path"]}
        self.assertIn("L_branch", branches)
        self.assertIn("R_branch", branches)

        # SUM excludes p3 (NULL amount), so 8 contributions, all equal to 10.
        self.assertEqual(total["value"], 80)
        self.assertEqual(total["contribution_count"], 8)
        self.assertEqual(total["contributing_values"], [{"value": 10, "occurrences": 8}])
        self.assertNotIn("p3", {r["row_id"] for r in total["source_records"]})

    def test_run_writes_requested_output_only(self):
        out = os.path.join(self.ws.root, "separate")
        run(self.plan_path, out)
        self.assertTrue(os.path.exists(os.path.join(out, "results.json")))
        with open(self.payments, "rb") as handle:
            self.assertIn(b'"row_id": "p1"', handle.read())

    def test_physical_row_order_does_not_change_results_or_identities(self):
        baseline = run(self.plan_path, os.path.join(self.ws.root, "o1"))
        baseline_trace = trace(self.plan_path)

        shuffled_payments = self.ws.table(
            "payments",
            [
                {"row_id": "p5", "account": None, "amount": 9, "status": "ok"},
                {"row_id": "p3", "account": "A", "amount": None, "status": "ok"},
                {"row_id": "p1", "account": "A", "amount": 10, "status": "ok"},
                {"row_id": "p4", "account": "B", "amount": 8, "status": "bad"},
                {"row_id": "p2", "account": "A", "amount": 10, "status": "ok"},
            ],
        )
        shuffled_buckets = self.ws.table(
            "buckets",
            [
                {"row_id": "b2", "account": "A", "region": "N"},
                {"row_id": "b3", "account": None, "region": "X"},
                {"row_id": "b1", "account": "A", "region": "N"},
            ],
        )
        shuffled_plan = json.loads(json.dumps(self.plan))
        for node in shuffled_plan["nodes"]:
            if node["id"] == "p":
                node["path"] = shuffled_payments
            elif node["id"] == "b":
                node["path"] = shuffled_buckets
        shuffled_path = self.ws.plan(shuffled_plan, "shuffled.json")

        shuffled = run(shuffled_path, os.path.join(self.ws.root, "o2"))
        self.assertEqual(baseline["rows"], shuffled["rows"])
        self.assertEqual(trace(shuffled_path), baseline_trace)

    def test_same_source_row_twice_through_union_keeps_both(self):
        # m1 qualifies for both branches, so it appears twice before the join.
        rows = self.ws.table(
            "docs",
            [{"row_id": "m1", "k": "K", "v": 5, "flag": "yes"}],
        )
        lookup = self.ws.table("lookup", [{"row_id": "l1", "k": "K", "g": "G"}])
        plan = {
            "nodes": [
                source("d", "docs", rows),
                source("l", "lookup", lookup),
                {"id": "a", "op": "filter", "input": "d", "field": "flag", "cmp": "eq", "value": "yes"},
                {"id": "b", "op": "filter", "input": "d", "field": "v", "cmp": "gt", "value": 0},
                {"id": "u", "op": "union_all", "left": "a", "right": "b"},
                {
                    "id": "j",
                    "op": "inner_join",
                    "left": "u",
                    "right": "l",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "d_",
                    "right_prefix": "l_",
                },
                {
                    "id": "g",
                    "op": "group",
                    "input": "j",
                    "keys": ["l_g"],
                    "aggregates": [{"name": "n", "fn": "count"}, {"name": "s", "fn": "sum", "field": "d_v"}],
                },
            ],
            "output": "g",
        }
        path = self.ws.plan(plan, "dupe_src.json")
        report = run(path, os.path.join(self.ws.root, "out"))
        self.assertEqual(report["rows"][0]["values"], {"l_g": "G", "n": 2, "s": 10})
        self.assertTrue(verify(path)["ok"])


class NullSemanticsTest(BaseCase):
    def test_filter_eq_and_gt_never_match_null(self):
        rows = self.ws.table(
            "t",
            [
                {"row_id": "r1", "x": 1, "s": None},
                {"row_id": "r2", "x": None, "s": "a"},
                {"row_id": "r3", "x": 0, "s": "a"},
            ],
        )
        plan = {
            "nodes": [
                source("t", "t", rows),
                {"id": "eq", "op": "filter", "input": "t", "field": "s", "cmp": "eq", "value": "a"},
                {"id": "gt", "op": "filter", "input": "eq", "field": "x", "cmp": "gt", "value": 0},
            ],
            "output": "gt",
        }
        path = self.ws.plan(plan)
        report = run(path, os.path.join(self.ws.root, "o"))
        self.assertEqual(report["row_count"], 0)  # r2 is NULL, r3 is 0

    def test_join_null_keys_do_not_match(self):
        left = self.ws.table("l", [{"row_id": "l1", "k": None, "v": 1}])
        right = self.ws.table("r", [{"row_id": "r1", "k": None, "v": 2}])
        plan = {
            "nodes": [
                source("l", "l", left),
                source("r", "r", right),
                {
                    "id": "j",
                    "op": "inner_join",
                    "left": "l",
                    "right": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "l_",
                    "right_prefix": "r_",
                },
            ],
            "output": "j",
        }
        report = run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))
        self.assertEqual(report["row_count"], 0)

    def test_group_null_keys_share_a_group_and_sum_all_null_is_null(self):
        rows = self.ws.table(
            "t",
            [
                {"row_id": "r1", "g": None, "v": None},
                {"row_id": "r2", "g": None, "v": None},
                {"row_id": "r3", "g": "a", "v": 4},
            ],
        )
        plan = {
            "nodes": [
                source("t", "t", rows),
                {
                    "id": "g",
                    "op": "group",
                    "input": "t",
                    "keys": ["g"],
                    "aggregates": [
                        {"name": "n", "fn": "count"},
                        {"name": "s", "fn": "sum", "field": "v"},
                    ],
                },
            ],
            "output": "g",
        }
        path = self.ws.plan(plan)
        report = run(path, os.path.join(self.ws.root, "o"))
        by_group = {row["values"]["g"]: row["values"] for row in report["rows"]}
        self.assertEqual(by_group[None], {"g": None, "n": 2, "s": None})
        self.assertEqual(by_group["a"], {"g": "a", "n": 1, "s": 4})
        self.assertTrue(verify(path)["ok"])

    def test_keyless_group_over_empty_input(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        plan = {
            "nodes": [
                source("t", "t", rows),
                {"id": "f", "op": "filter", "input": "t", "field": "v", "cmp": "gt", "value": 100},
                {
                    "id": "g",
                    "op": "group",
                    "input": "f",
                    "keys": [],
                    "aggregates": [
                        {"name": "n", "fn": "count"},
                        {"name": "s", "fn": "sum", "field": "v"},
                    ],
                },
            ],
            "output": "g",
        }
        path = self.ws.plan(plan)
        report = run(path, os.path.join(self.ws.root, "o"))
        self.assertEqual(report["rows"], [{"values": {"n": 0, "s": None}, "provenance": ["group"]}])
        self.assertTrue(verify(path)["ok"])


class RejectionTest(BaseCase):
    def _write_plan(self, nodes, output="o"):
        return self.ws.plan({"nodes": nodes, "output": output})

    def test_duplicate_row_id_rejected(self):
        rows = self.ws.table(
            "t",
            [
                {"row_id": "same", "v": 1},
                {"row_id": "same", "v": 2},
            ],
        )
        path = self._write_plan([source("o", "t", rows)])
        with self.assertRaises(SourceError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_unknown_filter_field_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan(
            [
                source("t", "t", rows),
                {"id": "o", "op": "filter", "input": "t", "field": "missing", "cmp": "eq", "value": 1},
            ]
        )
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_unknown_join_key_rejected(self):
        left = self.ws.table("l", [{"row_id": "l1", "k": 1}])
        right = self.ws.table("r", [{"row_id": "r1", "k": 1}])
        path = self._write_plan(
            [
                source("l", "l", left),
                source("r", "r", right),
                {
                    "id": "o",
                    "op": "inner_join",
                    "left": "l",
                    "right": "r",
                    "left_key": "nope",
                    "right_key": "k",
                    "left_prefix": "l_",
                    "right_prefix": "r_",
                },
            ]
        )
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_cycle_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan(
            [
                source("t", "t", rows),
                {"id": "a", "op": "filter", "input": "b", "field": "v", "cmp": "eq", "value": 1},
                {"id": "b", "op": "filter", "input": "a", "field": "v", "cmp": "eq", "value": 1},
            ],
            output="a",
        )
        with self.assertRaises(PlanError) as ctx:
            run(path, os.path.join(self.ws.root, "o"))
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_self_reference_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan(
            [
                source("t", "t", rows),
                {"id": "a", "op": "union_all", "left": "t", "right": "a"},
            ],
            output="a",
        )
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_union_with_different_fields_rejected(self):
        left = self.ws.table("l", [{"row_id": "l1", "a": 1}])
        right = self.ws.table("r", [{"row_id": "r1", "b": 1}])
        path = self._write_plan(
            [
                source("l", "l", left),
                source("r", "r", right),
                {"id": "o", "op": "union_all", "left": "l", "right": "r"},
            ]
        )
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_group_cannot_feed_another_operator(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan(
            [
                source("t", "t", rows),
                {"id": "g", "op": "group", "input": "t", "keys": [], "aggregates": [{"name": "n", "fn": "count"}]},
                {"id": "o", "op": "filter", "input": "g", "field": "n", "cmp": "gt", "value": 0},
            ]
        )
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_unknown_output_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan([source("t", "t", rows)], output="nope")
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_duplicate_node_id_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan([source("o", "t", rows), source("o", "t", rows)])
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_unsupported_operator_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan([source("t", "t", rows), {"id": "o", "op": "left_join", "input": "t"}])
        with self.assertRaises(PlanError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_bad_row_value_type_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": [1, 2]}])
        path = self._write_plan([source("o", "t", rows)])
        with self.assertRaises(SourceError):
            run(path, os.path.join(self.ws.root, "o"))

    def test_trace_requires_group_output(self):
        rows = self.ws.table("t", [{"row_id": "r1", "v": 1}])
        path = self._write_plan([source("o", "t", rows)])
        with self.assertRaises(EtlError):
            trace(path)
        with self.assertRaises(EtlError):
            verify(path)


class SourceRelationTest(BaseCase):
    def test_identical_business_values_remain_two_rows(self):
        rows = self.ws.table(
            "t",
            [
                {"row_id": "r1", "v": 7},
                {"row_id": "r2", "v": 7},
            ],
        )
        plan = {"nodes": [source("o", "t", rows)], "output": "o"}
        report = run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))
        self.assertEqual(report["row_count"], 2)
        ids = {
            row["source_occurrences"][0]["row_id"] for row in report["rows"]
        }
        self.assertEqual(ids, {"r1", "r2"})
        self.assertNotEqual(report["rows"][0]["provenance"], report["rows"][1]["provenance"])

    def test_join_to_two_right_rows_produces_two_rows_per_left(self):
        left = self.ws.table("l", [{"row_id": "l1", "k": "K", "v": 1}])
        right = self.ws.table(
            "r",
            [
                {"row_id": "r1", "k": "K", "w": 2},
                {"row_id": "r2", "k": "K", "w": 3},
            ],
        )
        plan = {
            "nodes": [
                source("l", "l", left),
                source("r", "r", right),
                {
                    "id": "o",
                    "op": "inner_join",
                    "left": "l",
                    "right": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "l_",
                    "right_prefix": "r_",
                },
            ],
            "output": "o",
        }
        report = run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))
        self.assertEqual(report["row_count"], 2)
        pairs = {
            tuple(occ["row_id"] for occ in row["source_occurrences"])
            for row in report["rows"]
        }
        self.assertEqual(pairs, {("l1", "r1"), ("l1", "r2")})

    def test_join_prefix_collision_rejected(self):
        left = self.ws.table("l", [{"row_id": "l1", "k": 1, "x": 1}])
        right = self.ws.table("r", [{"row_id": "r1", "k": 1, "y": 2}])
        plan = {
            "nodes": [
                source("l", "l", left),
                source("r", "r", right),
                {
                    "id": "o",
                    "op": "inner_join",
                    "left": "l",
                    "right": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "",
                    "right_prefix": "",
                },
            ],
            "output": "o",
        }
        with self.assertRaises(PlanError):
            run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))


class SchemaSemanticsTest(BaseCase):
    def test_sum_on_string_field_rejected(self):
        rows = self.ws.table("t", [{"row_id": "r1", "s": "x"}])
        plan = {
            "nodes": [
                source("t", "t", rows),
                {
                    "id": "g",
                    "op": "group",
                    "input": "t",
                    "keys": [],
                    "aggregates": [{"name": "s", "fn": "sum", "field": "s"}],
                },
            ],
            "output": "g",
        }
        with self.assertRaises(PlanError):
            run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))

    def test_union_accepts_same_fields_in_different_physical_order(self):
        left = self.ws.table("l", [{"row_id": "a1", "x": 1, "y": "p"}])
        right = self.ws.table("r", [{"row_id": "b1", "y": "q", "x": 2}])
        plan = {
            "nodes": [
                source("l", "l", left),
                source("r", "r", right),
                {"id": "u", "op": "union_all", "left": "l", "right": "r"},
                {
                    "id": "g",
                    "op": "group",
                    "input": "u",
                    "keys": ["y"],
                    "aggregates": [
                        {"name": "n", "fn": "count"},
                        {"name": "s", "fn": "sum", "field": "x"},
                    ],
                },
            ],
            "output": "g",
        }
        path = self.ws.plan(plan)
        report = run(path, os.path.join(self.ws.root, "o"))
        by_group = {row["values"]["y"]: row["values"] for row in report["rows"]}
        self.assertEqual(by_group, {"p": {"y": "p", "n": 1, "s": 1}, "q": {"y": "q", "n": 1, "s": 2}})
        self.assertTrue(verify(path)["ok"])

    def test_non_group_output_reports_join_provenance(self):
        left = self.ws.table("l", [{"row_id": "l1", "k": "A", "v": 1}])
        right = self.ws.table("r", [{"row_id": "r1", "k": "A", "w": 2}])
        plan = {
            "nodes": [
                source("l", "l", left),
                source("r", "r", right),
                {
                    "id": "j",
                    "op": "inner_join",
                    "left": "l",
                    "right": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "l_",
                    "right_prefix": "r_",
                },
            ],
            "output": "j",
        }
        report = run(self.ws.plan(plan), os.path.join(self.ws.root, "o"))
        self.assertEqual(
            report["rows"][0]["values"],
            {"l_row_id": "l1", "l_k": "A", "l_v": 1, "r_row_id": "r1", "r_k": "A", "r_w": 2},
        )
        occurrences = {
            (occ["table"], occ["row_id"]) for occ in report["rows"][0]["source_occurrences"]
        }
        self.assertEqual(occurrences, {("l", "l1"), ("r", "r1")})

    def test_self_join_is_supported_and_verified(self):
        rows = self.ws.table(
            "t",
            [
                {"row_id": "r1", "k": "A", "v": 1},
                {"row_id": "r2", "k": "A", "v": 2},
            ],
        )
        plan = {
            "nodes": [
                source("t", "t", rows),
                {
                    "id": "j",
                    "op": "inner_join",
                    "left": "t",
                    "right": "t",
                    "left_key": "k",
                    "right_key": "k",
                    "left_prefix": "l_",
                    "right_prefix": "r_",
                },
                {
                    "id": "g",
                    "op": "group",
                    "input": "j",
                    "keys": ["l_k"],
                    "aggregates": [
                        {"name": "n", "fn": "count"},
                        {"name": "s", "fn": "sum", "field": "r_v"},
                    ],
                },
            ],
            "output": "g",
        }
        path = self.ws.plan(plan)
        report = run(path, os.path.join(self.ws.root, "o"))
        self.assertEqual(report["rows"][0]["values"], {"l_k": "A", "n": 4, "s": 6})
        self.assertTrue(verify(path)["ok"])


if __name__ == "__main__":
    unittest.main()
