# -*- coding: utf-8 -*-
"""tablefreeze 测试：等价页面核对、手工追查跨格与显式 headers、坏例拒绝。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tablefreeze as tf  # noqa: E402

FIX = ROOT / "fixtures"
SCOPE = FIX / "scope.json"


def run_fixture(name, table_id):
    html = (FIX / name).read_text(encoding="utf-8")
    return tf.extract_tables(html, [table_id])[0]


def run_inline(html, table_id="t"):
    return tf.extract_tables(html, [table_id])[0]


def cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tablefreeze.py"), *args],
        capture_output=True, text=True)


class CatalogTests(unittest.TestCase):
    """catalog.html 与 catalog-variant.html 结构语义相同，输出必须一致。"""

    @classmethod
    def setUpClass(cls):
        cls.a = run_fixture("catalog.html", "catalog")
        cls.b = run_fixture("catalog-variant.html", "catalog")

    def test_both_ok(self):
        self.assertEqual(self.a.status, "ok")
        self.assertEqual(self.b.status, "ok")

    def test_variant_outputs_identical(self):
        self.assertEqual(self.a.long_rows, self.b.long_rows)
        self.assertEqual(self.a.headers_doc, self.b.headers_doc)
        self.assertEqual([p.to_dict() for p in self.a.problems],
                         [p.to_dict() for p in self.b.problems])

    def test_variant_csv_bytes_identical(self):
        with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
            for html, out in (("catalog.html", da), ("catalog-variant.html", db)):
                proc = cli("extract", "--html", str(FIX / html),
                           "--scope", str(SCOPE), "--out", out)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            for name in ("long_table.csv", "headers.json", "problems.json"):
                self.assertEqual((Path(da) / "catalog" / name).read_bytes(),
                                 (Path(db) / "catalog" / name).read_bytes(), name)

    def test_nested_table_text_excluded(self):
        v3 = next(r for r in self.a.long_rows if r["cell_id"] == "v3")
        self.assertEqual(v3["raw_text"], "统一报价")
        self.assertNotIn("不应进入父表", v3["raw_text"])

    def test_span_2x2_shared_origin(self):
        """手工追查跨两行两列的 v4：四个位置、同一 cell ID、仅 origin 带原文。"""
        v4 = [r for r in self.a.long_rows if r["cell_id"] == "v4"]
        positions = {(r["row_group"], r["row_index"], r["col_index"]) for r in v4}
        self.assertEqual(positions, {("south", 0, 2), ("south", 0, 3),
                                     ("south", 1, 2), ("south", 1, 3)})
        origins = [r for r in v4 if r["slot_role"] == "origin"]
        self.assertEqual(len(origins), 1)
        self.assertEqual((origins[0]["row_index"], origins[0]["col_index"]), (0, 2))
        self.assertEqual(origins[0]["raw_text"], "共享说明")
        shared = [r for r in v4 if r["slot_role"] == "shared"]
        self.assertEqual(len(shared), 3)
        for r in shared:  # 共享格不假装原文独立存在
            self.assertEqual(r["raw_text"], "")
            self.assertEqual(r["value_status"], "skipped")
        # 共享位置仍按各自位置得到表头路径
        by_pos = {(r["row_index"], r["col_index"]): r for r in v4}
        self.assertEqual(by_pos[(1, 3)]["column_header_ids"], "q1 stock")
        self.assertEqual(by_pos[(1, 3)]["row_header_ids"], "south-head p04")
        self.assertEqual(by_pos[(0, 3)]["column_header_ids"], "q1 stock")

    def test_rowspan_zero_bounded_to_rowgroup(self):
        cell = {c.cell_id: c for c in self.a.cells}["north-head"]
        self.assertEqual(cell.rowspan, 2)  # 本行组剩余行数
        south = next(g for g in self.a.groups if g.group_id == "south")
        for (order, _, _), (c, _) in self.a.slots.items():
            if order == south.order:
                self.assertNotEqual(c.cell_id, "north-head")  # 不跨行组

    def test_explicit_headers_postorder_and_override(self):
        """v3 的显式 headers 取代 scope 推断；祖先 q1 先于 price 展开。"""
        v3 = next(r for r in self.a.long_rows
                  if r["cell_id"] == "v3" and r["slot_role"] == "origin")
        self.assertEqual(v3["header_source"], "headers")
        self.assertEqual(v3["header_ids"], "north-head p02 q1 price")
        self.assertEqual(v3["row_header_ids"], "north-head p02")
        self.assertEqual(v3["column_header_ids"], "q1 price")
        # 对照：同列的 v1 走 scope 推断
        v1 = next(r for r in self.a.long_rows if r["cell_id"] == "v1")
        self.assertEqual(v1["header_source"], "scope")
        self.assertEqual(v1["column_header_ids"], "q1 price")
        self.assertEqual(v1["row_header_ids"], "north-head p01")
        # th 自身的 headers 链登记在表头关系中
        price = next(h for h in self.a.headers_doc["headers"] if h["id"] == "price")
        self.assertEqual(price["explicit_header_ids"], ["q1"])

    def test_types(self):
        rows = {r["cell_id"]: r for r in self.a.long_rows if r["slot_role"] == "origin"}
        self.assertEqual(rows["v1"]["value"], "12.30")   # decimal 两位小数
        self.assertEqual(rows["v1"]["value_status"], "ok")
        self.assertEqual(rows["v2"]["value"], "7")       # int 规范有符号十进制
        self.assertEqual(rows["v3"]["value"], "统一报价")  # text 原样


class BadFixtureTests(unittest.TestCase):
    """坏例：断言失败定位到源 cell，不发布成功表。"""

    def assert_rejected(self, fixture, code, cell_id):
        res = run_fixture(fixture, "t")
        self.assertEqual(res.status, "rejected")
        fatals = [p for p in res.problems if p.severity == "fatal"]
        self.assertTrue(fatals, "应有 fatal 问题")
        hit = [p for p in fatals if p.code == code]
        self.assertEqual(len(hit), 1, f"应恰有一条 {code}")
        self.assertEqual(hit[0].cell_id, cell_id)
        self.assertIsNotNone(hit[0].row_group)
        self.assertIsNotNone(hit[0].row_index)
        self.assertIsNotNone(hit[0].col_index)
        self.assertEqual(res.long_rows, [])  # 不生成成功长表
        return hit[0]

    def test_headers_cycle(self):
        p = self.assert_rejected("bad-cycle.html", "headers-cycle", "a")
        self.assertEqual(p.details["cycle"], ["a", "b", "a"])

    def test_rowspan_crosses_rowgroup(self):
        p = self.assert_rejected("bad-cross-group.html", "rowspan-out-of-bounds", "x")
        self.assertEqual(p.details["rowspan"], 2)
        self.assertEqual(p.details["rows_available"], 1)

    def test_headers_cross_table(self):
        p = self.assert_rejected("bad-cross-table.html", "headers-cross-table", "x")
        self.assertEqual(p.details["token"], "external")
        self.assertEqual(p.details["found_in_table"], "u")

    def test_rejected_publishes_no_success_table(self):
        with tempfile.TemporaryDirectory() as out:
            for fixture in ("bad-cycle.html", "bad-cross-group.html",
                            "bad-cross-table.html"):
                proc = cli("extract", "--html", str(FIX / fixture),
                           "--scope", str(SCOPE), "--out", out)
                self.assertEqual(proc.returncode, 2, fixture)
                tdir = Path(out) / "t"
                self.assertFalse((tdir / "long_table.csv").exists(), fixture)
                self.assertFalse((tdir / "headers.json").exists(), fixture)
                doc = json.loads((tdir / "problems.json").read_text(encoding="utf-8"))
                self.assertEqual(doc["status"], "rejected")
                self.assertGreater(doc["fatal_count"], 0)


class InlineTests(unittest.TestCase):
    """针对受限规范边界的小表。"""

    def test_cell_overlap_rejected(self):
        html = ('<table id="t"><tbody>'
                '<tr><td id="a">A</td><td id="b" rowspan="2">B</td></tr>'
                '<tr><td id="c" colspan="2">C</td></tr>'
                '</tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.status, "rejected")
        hit = [p for p in res.problems if p.code == "cell-overlap"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].cell_id, "c")            # 定位到源 cell
        self.assertEqual(hit[0].details["conflicts_with"], "b")

    def test_missing_slot_and_empty_cell_recorded_separately(self):
        html = ('<table id="t"><thead><tr><th id="h1" scope="col">甲</th>'
                '<th id="h2" scope="col">乙</th></tr></thead>'
                '<tbody><tr><td>1</td><td></td></tr>'
                '<tr><td>2</td></tr></tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.status, "ok")
        codes = {(p.code, p.row_index, p.col_index) for p in res.problems}
        self.assertIn(("empty-cell", 0, 1), codes)       # 内容为空的 td
        self.assertIn(("missing-slot", 1, 1), codes)     # 缺失格
        by_pos = {(r["row_index"], r["col_index"]): r for r in res.long_rows}
        self.assertEqual(by_pos[(0, 1)]["is_empty"], "true")
        self.assertEqual(by_pos[(0, 1)]["value_status"], "empty")
        self.assertEqual(by_pos[(1, 1)]["slot_role"], "missing")
        self.assertEqual(by_pos[(1, 1)]["cell_id"], "")

    def test_same_text_different_id_not_merged(self):
        html = ('<table id="t"><thead><tr>'
                '<th id="x1" scope="col">相同</th><th id="x2" scope="col">相同</th>'
                '</tr></thead><tbody><tr><td>a</td><td>b</td></tr></tbody></table>')
        res = run_inline(html)
        headers = res.headers_doc["headers"]
        self.assertEqual([h["id"] for h in headers], ["x1", "x2"])
        self.assertEqual([h["text"] for h in headers], ["相同", "相同"])
        rows = res.long_rows
        self.assertEqual(rows[0]["column_header_ids"], "x1")
        self.assertEqual(rows[1]["column_header_ids"], "x2")

    def test_header_text_inner_whitespace_preserved(self):
        html = ('<table id="t"><thead><tr><th id="h" scope="col">  多  词\n</th></tr>'
                '</thead><tbody><tr><td>1</td></tr></tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.headers_doc["headers"][0]["text"], "多  词")

    def test_type_conversion_failure_keeps_raw_and_reason(self):
        html = ('<table id="t"><tbody>'
                '<tr><td id="d1" data-type="decimal">9.5</td>'
                '<td id="d2" data-type="int">007</td>'
                '<td id="d3" data-type="int">7.0</td></tr>'
                '</tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.status, "ok")  # 类型失败不拒绝表
        rows = {r["cell_id"]: r for r in res.long_rows}
        self.assertEqual(rows["d1"]["value_status"], "error")
        self.assertEqual(rows["d1"]["raw_text"], "9.5")  # 保留原文
        self.assertIn("两位小数", rows["d1"]["error_reason"])
        self.assertEqual(rows["d2"]["value"], "7")       # int 规范化去前导零
        self.assertEqual(rows["d3"]["value_status"], "error")
        problems = [p for p in res.problems if p.code == "type-conversion"]
        self.assertEqual({p.cell_id for p in problems}, {"d1", "d3"})
        for p in problems:                               # 定位到源 cell
            self.assertIsNotNone(p.row_group)
            self.assertEqual(p.severity, "warning")

    def test_identifier_like_text_keeps_leading_zeros(self):
        html = ('<table id="t"><tbody><tr><td id="c">001</td></tr></tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.long_rows[0]["value"], "001")  # 无 data-type 按 text

    def test_headers_to_td_and_missing_id_diagnosed(self):
        html = ('<table id="t"><tbody>'
                '<tr><td id="plain">a</td>'
                '<td id="src" headers="plain ghost">b</td></tr>'
                '</tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.status, "rejected")
        codes = {p.code for p in res.problems if p.severity == "fatal"}
        self.assertEqual(codes, {"headers-target-not-th", "headers-id-missing"})
        for p in res.problems:
            self.assertEqual(p.cell_id, "src")

    def test_invalid_span_values_rejected(self):
        html = ('<table id="t"><tbody>'
                '<tr><td id="s" colspan="0" rowspan="x">a</td></tr>'
                '</tbody></table>')
        res = run_inline(html)
        self.assertEqual(res.status, "rejected")
        codes = {p.code for p in res.problems if p.severity == "fatal"}
        self.assertEqual(codes, {"invalid-colspan", "invalid-rowspan"})

    def test_table_not_found(self):
        res = run_inline('<table id="u"><tr><td>1</td></tr></table>', "t")
        self.assertEqual(res.status, "rejected")
        self.assertEqual(res.problems[0].code, "table-not-found")


class CliTests(unittest.TestCase):

    def test_extract_ok_exit_0(self):
        with tempfile.TemporaryDirectory() as out:
            proc = cli("extract", "--html", str(FIX / "catalog.html"),
                       "--scope", str(SCOPE), "--out", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tdir = Path(out) / "catalog"
            self.assertTrue((tdir / "long_table.csv").exists())
            self.assertTrue((tdir / "headers.json").exists())
            self.assertTrue((tdir / "problems.json").exists())

    def test_inspect_exit_0_and_shows_grid(self):
        proc = cli("inspect", "--html", str(FIX / "catalog.html"),
                   "--scope", str(SCOPE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("v4(2x2)", proc.stdout)
        self.assertIn("v4~", proc.stdout)
        self.assertIn("headers", proc.stdout)

    def test_inspect_rejected_shows_problem(self):
        proc = cli("inspect", "--html", str(FIX / "bad-cycle.html"),
                   "--scope", str(SCOPE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("headers-cycle", proc.stdout)
        self.assertIn("rejected", proc.stdout)

    def test_file_not_in_scope_errors(self):
        proc = cli("extract", "--html", "no-such-page.html",
                   "--scope", str(SCOPE), "--out", "/tmp/x")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
