"""端到端验证（标准库 unittest，规模几十点）。

覆盖：
1. 乱序批 metrics-shuffled.json 与原批归一化结果完全一致；
2. cum_events/delta_events、cum_hist/delta_hist 两组独立流的区间事实规范等价；
3. 手算的 histogram 桶差值；
4. 桶边界变化 -> incomparable + epoch 停止，新 startTime=20 重新形成区间；
5. delta 部分重叠 -> 全部冲突排除，不摊分；
6. 大整数精确（9007199254740997 等超过 2^53 的值不经过 float）；
7. 属性换序不影响身份，字符串 1 与整数 1 分流；
8. 混合 temporality 整流拒绝、单调倒退、缺口不补零、精确折叠、同区间冲突、
   缺 sum、非法 schema/重复属性等。
"""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal

from otlp_normalize import inspect, normalize
from otlp_normalize.model import load_exact
from otlp_normalize.report import render_facts, render_origins, render_unconfirmed

FIXTURE = "fixtures/metrics.json"
SHUFFLED = "fixtures/metrics-shuffled.json"


def facts_by_name(result, name):
    return [f for f in result.facts if f.identity[3] == name]


def canonical_payload(result):
    """剥离分配型 ID（F/P/U 序号随读取顺序变化）后的可比较结构。"""
    facts = []
    for row in render_facts(result.facts):
        row = copy.deepcopy(row)
        row.pop("fact_id")
        row.pop("source_origin_ids")
        facts.append(row)
    unconf = []
    for row in render_unconfirmed(result.unconfirmed):
        row = copy.deepcopy(row)
        row.pop("unconfirmed_id")
        row.pop("origin_ids")
        payload = row.get("payload") or {}
        for point in payload.get("points", []):
            point.pop("origin_id", None)
        unconf.append(row)
    unconf.sort(key=lambda r: (r["reason"], str(r["identity"]), str(r["startTimeUnixNano"])))
    return {"facts": facts, "unconfirmed": unconf}


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = normalize(FIXTURE)
        cls.rs = normalize(SHUFFLED)

    def test_01_shuffled_equivalent(self):
        self.assertEqual(canonical_payload(self.r), canonical_payload(self.rs))
        # origin 位置不同，但每个输入点都有归宿且状态分布一致
        def statuses(result):
            return sorted((o.status, o.reason or "") for o in result.origins)
        self.assertEqual(statuses(self.r), statuses(self.rs))

    def test_02_origin_closure(self):
        # 每个输入点恰好一条 origin，且不存在 pending
        self.assertEqual(len(self.r.origins), 17)
        self.assertTrue(all(o.status in {"fact", "collapsed", "unconfirmed"} for o in self.r.origins))
        # 事实引用的 origin 全部存在
        ids = {o.origin_id for o in self.r.origins}
        for f in self.r.facts:
            self.assertTrue(set(f.source_origin_ids) <= ids)

    def test_03_sum_delta_cumulative_equivalence(self):
        cum = facts_by_name(self.r, "cum_events")
        delta = facts_by_name(self.r, "delta_events")
        self.assertEqual(len(cum), len(delta))
        for c, d in zip(sorted(cum, key=lambda f: f.start_time),
                        sorted(delta, key=lambda f: f.start_time)):
            self.assertEqual((c.start_time, c.end_time), (d.start_time, d.end_time))
            self.assertEqual(c.value["asInt"], d.value["asInt"])
        # 手算：4, 9-4=5, 重置代 3
        self.assertEqual([f.value["asInt"] for f in sorted(cum, key=lambda f: f.start_time)],
                         ["4", "5", "3"])
        # 第三代从 startTime=20 开始，与前一结束点 20 衔接（允许相等）
        self.assertEqual([f.generation for f in sorted(cum, key=lambda f: f.start_time)], [1, 1, 2])

    def test_04_histogram_hand_computed_buckets(self):
        cum = sorted(facts_by_name(self.r, "cum_hist"), key=lambda f: f.start_time)
        delta = sorted(facts_by_name(self.r, "delta_hist"), key=lambda f: f.start_time)
        # 首区间 [0,10) 直接取首点
        self.assertEqual(cum[0].value["bucketCounts"], ["1", "2", "1"])
        self.assertEqual(cum[0].value["count"], "4")
        # [10,20) 手算逐桶相减：[2,4,3]-[1,2,1]=[1,2,2]，count 9-4=5，sum 140-50=90
        self.assertEqual(cum[1].value["bucketCounts"], ["1", "2", "2"])
        self.assertEqual(cum[1].value["count"], "5")
        self.assertEqual(cum[1].value["sum"], "90")
        # 与独立 delta 流规范等价（不同 metric 身份，不自动合并，只在此显式配对核验）
        for c, d in zip(cum, delta):
            self.assertEqual((c.start_time, c.end_time), (d.start_time, d.end_time))
            self.assertEqual(c.value["bucketCounts"], d.value["bucketCounts"])
            self.assertEqual(c.value["count"], d.value["count"])
            self.assertEqual(c.value["sum"], d.value["sum"])
            self.assertEqual(c.value["explicitBounds"], d.value["explicitBounds"])

    def test_05_bad_hist_schema(self):
        rows = facts_by_name(self.r, "bad_hist_schema")
        # 首点形成区间（缺 sum -> sum_unknown）；同 startTime 变边界 -> 停止；新 startTime=20 恢复
        self.assertEqual([(f.start_time, f.end_time) for f in rows], [(0, 10), (20, 30)])
        self.assertIn("sum_unknown", rows[0].quality)
        self.assertIsNone(rows[0].value["sum"])
        self.assertFalse(rows[0].value["sum_known"])
        self.assertEqual(rows[1].generation, 2)
        self.assertIn("gap_before_interval", rows[1].quality)  # [10,20) 缺口不补零
        reasons = {u.reason for u in self.r.unconfirmed}
        self.assertIn("incomparable_bucket_bounds", reasons)
        # 边界失效点没有被强行按下标相减
        bad = next(u for u in self.r.unconfirmed if u.reason == "incomparable_bucket_bounds")
        self.assertEqual(bad.origin_ids, ["P0012"])

    def test_06_delta_overlap_excluded(self):
        self.assertEqual(facts_by_name(self.r, "overlap"), [])
        rec = next(u for u in self.r.unconfirmed if u.reason == "delta_partial_overlap_conflict")
        self.assertEqual(sorted(rec.origin_ids), ["P0014", "P0015"])

    def test_07_large_integers_exact(self):
        rows = sorted(facts_by_name(self.r, "large"), key=lambda f: f.start_time)
        self.assertEqual(rows[0].value["asInt"], "9007199254740997")
        self.assertEqual(rows[1].value["asInt"], "2")  # 9007199254740999-...997，精确差
        # 确认内部是 Python int 而非 float
        self.assertIsInstance(rows[0].start_time, int)
        self.assertEqual(9007199254740999 - 9007199254740997, 2)

    def test_08_inspect_text_runs(self):
        text = inspect(FIXTURE)
        self.assertIn("inspect 报告", text)
        self.assertIn("incomparable_bucket_bounds", text)


# ---------------------------------------------------------------------------
# 合成批：身份、折叠、冲突、倒退、代重叠、混合 temporality 等
# ---------------------------------------------------------------------------
def sum_point(start, end, value, attrs=None, temp=2):
    return {
        "startTimeUnixNano": str(start),
        "timeUnixNano": str(end),
        "asInt": str(value),
        "attributes": attrs or [{"key": "k", "value": {"stringValue": "v"}}],
        "_temp": temp,
    }


def batch_of(points, *, metric_name="m", monotonic=True, histogram=False, temporality=None):
    def strip(p):
        p = {k: v for k, v in p.items() if not k.startswith("_")}
        return p

    temps = {p.get("_temp", temporality) for p in points}
    assert len(temps) == 1 or temporality is None
    temp = temporality if temporality is not None else next(iter(temps))
    if histogram:
        metric = {"name": metric_name, "unit": "1",
                  "histogram": {"aggregationTemporality": temp, "dataPoints": [strip(p) for p in points]}}
    else:
        metric = {"name": metric_name, "unit": "1",
                  "sum": {"aggregationTemporality": temp, "isMonotonic": monotonic,
                          "dataPoints": [strip(p) for p in points]}}
    return {"resourceMetrics": [{"resource": {"attributes": []},
                                 "scopeMetrics": [{"scope": {"name": "s", "version": "v1"},
                                                   "metrics": [metric]}]}]}


class SyntheticTests(unittest.TestCase):
    def test_10_attribute_order_irrelevant(self):
        attrs_a = [
            {"key": "z", "value": {"stringValue": "1"}},
            {"key": "a", "value": {"intValue": "1"}},
        ]
        attrs_b = [
            {"key": "a", "value": {"intValue": "1"}},
            {"key": "z", "value": {"stringValue": "1"}},
        ]
        b1 = batch_of([sum_point(0, 10, 1, attrs_a)], temporality=1)
        b2 = batch_of([sum_point(0, 10, 1, attrs_b)], temporality=1)
        r1, r2 = normalize(b1), normalize(b2)
        self.assertEqual(r1.facts[0].identity, r2.facts[0].identity)

    def test_11_string_1_vs_int_1_distinct_streams(self):
        s_attr = [{"key": "k", "value": {"stringValue": "1"}}]
        i_attr = [{"key": "k", "value": {"intValue": "1"}}]
        batch = {
            "resourceMetrics": [{
                "resource": {"attributes": []},
                "scopeMetrics": [{
                    "scope": {"name": "s", "version": "v1"},
                    "metrics": [{
                        "name": "m", "unit": "1",
                        "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                            sum_point(0, 10, 1, s_attr, temp=1),
                            sum_point(0, 10, 1, i_attr, temp=1),
                        ]},
                    }],
                }],
            }]
        }
        r = normalize(batch)
        identities = {f.identity for f in r.facts}
        self.assertEqual(len(identities), 2)
        self.assertEqual(len(r.facts), 2)

    def test_12_exact_duplicate_collapses(self):
        pts = [sum_point(0, 10, 5, temp=1), sum_point(10, 20, 7, temp=1),
               sum_point(0, 10, 5, temp=1)]  # 精确重复（含同内容）
        r = normalize(batch_of(pts, temporality=1))
        self.assertEqual(len(r.facts), 2)
        folded = [o for o in r.origins if o.status == "collapsed"]
        self.assertEqual(len(folded), 1)
        anchor = folded[0].collapsed_into_origin
        self.assertEqual(folded[0].fact_id,
                         next(o.fact_id for o in r.origins if o.origin_id == anchor))

    def test_13_same_interval_content_conflict_isolated(self):
        pts = [sum_point(0, 10, 5, temp=1), sum_point(0, 10, 6, temp=1)]
        r = normalize(batch_of(pts, temporality=1))
        self.assertEqual(r.facts, [])
        self.assertEqual(r.unconfirmed[0].reason, "duplicate_identity_interval_conflict")

    def test_14_mixed_temporality_rejected_whole_stream(self):
        # OTLP 中 temporality 挂在 metric 描述符上；两个同身份描述符分别声明
        # delta / cumulative，整流必须拒绝，而不是拆成两条流掩盖矛盾。
        point = {k: v for k, v in sum_point(0, 10, 5).items() if not k.startswith("_")}
        batch = {
            "resourceMetrics": [{
                "resource": {"attributes": []},
                "scopeMetrics": [{
                    "scope": {"name": "s", "version": "v1"},
                    "metrics": [
                        {"name": "m", "unit": "1",
                         "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                                 "dataPoints": [dict(point)]}},
                        {"name": "m", "unit": "1",
                         "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                 "dataPoints": [dict(point)]}},
                    ],
                }],
            }]
        }
        r = normalize(batch)
        self.assertEqual(r.facts, [])
        self.assertEqual(len(r.unconfirmed), 1)
        self.assertEqual(r.unconfirmed[0].reason, "mixed_temporality")
        self.assertEqual(len(r.unconfirmed[0].origin_ids), 2)

    def test_15_cumulative_monotonic_regression(self):
        pts = [sum_point(0, 10, 5), sum_point(0, 20, 3)]  # 倒退
        r = normalize(batch_of(pts, temporality=2))
        self.assertEqual(len(r.facts), 1)  # 仅首区间
        self.assertEqual(r.unconfirmed[0].reason, "monotonic_regression")

    def test_16_overlapping_epoch_ambiguous(self):
        # 第一代确认到 20；新代 startTime=15 早于 20 -> 歧义；后续 startTime=25 可重开
        pts = [sum_point(0, 10, 5), sum_point(0, 20, 8),
               sum_point(15, 25, 2), sum_point(25, 35, 4)]
        r = normalize(batch_of(pts, temporality=2))
        reasons = [u.reason for u in r.unconfirmed]
        self.assertIn("cumulative_epoch_overlap_ambiguous", reasons)
        # 新代 [25,35) 形成首区间；[20,25) 缺口不补
        last = sorted(r.facts, key=lambda f: f.start_time)[-1]
        self.assertEqual((last.start_time, last.end_time), (25, 35))
        self.assertEqual(last.value["asInt"], "4")

    def test_17_gap_not_zero_filled(self):
        pts = [sum_point(0, 10, 5), sum_point(20, 30, 7)]  # 新代，缺口 [10,20)
        r = normalize(batch_of(pts, temporality=2))
        self.assertEqual([(f.start_time, f.end_time) for f in r.facts], [(0, 10), (20, 30)])
        self.assertIn("gap_before_interval", r.facts[1].quality)

    def test_18_histogram_missing_sum_propagates_unknown(self):
        p1 = {"startTimeUnixNano": "0", "timeUnixNano": "10", "attributes": [],
              "count": "2", "bucketCounts": ["1", "1"], "explicitBounds": [5]}
        p2 = {"startTimeUnixNano": "0", "timeUnixNano": "20", "attributes": [],
              "count": "5", "bucketCounts": ["3", "2"], "explicitBounds": [5], "sum": 11}
        r = normalize(batch_of([p1, p2], histogram=True, temporality=2))
        f0, f1 = sorted(r.facts, key=lambda f: f.start_time)
        self.assertFalse(f0.value["sum_known"])
        self.assertIsNone(f0.value["sum"])
        # 差值区间：基线缺 sum -> 区间 sum 不可确定，绝不补 0
        self.assertFalse(f1.value["sum_known"])
        self.assertIsNone(f1.value["sum"])
        self.assertIn("sum_unknown", f1.quality)
        self.assertEqual(f1.value["bucketCounts"], ["2", "1"])

    def test_19_histogram_sum_known_when_both_present(self):
        p1 = {"startTimeUnixNano": "0", "timeUnixNano": "10", "attributes": [],
              "count": "2", "bucketCounts": ["1", "1"], "explicitBounds": [5], "sum": 4}
        p2 = {"startTimeUnixNano": "0", "timeUnixNano": "20", "attributes": [],
              "count": "5", "bucketCounts": ["3", "2"], "explicitBounds": [5], "sum": 11}
        r = normalize(batch_of([p1, p2], histogram=True, temporality=2))
        f1 = sorted(r.facts, key=lambda f: f.start_time)[1]
        self.assertTrue(f1.value["sum_known"])
        self.assertEqual(f1.value["sum"], "7")

    def test_20_bad_histogram_schemas_rejected(self):
        base = {"startTimeUnixNano": "0", "timeUnixNano": "10", "attributes": []}
        cases = [
            ({"count": "2", "bucketCounts": ["1", "2"], "explicitBounds": [5, 5]},
             "hist_bounds_not_strict_increasing"),
            ({"count": "2", "bucketCounts": ["1", "1", "0"], "explicitBounds": [5]},
             "hist_bucket_length_mismatch"),
            ({"count": "3", "bucketCounts": ["1", "1"], "explicitBounds": [5]},
             "hist_count_not_equal_bucket_sum"),
            ({"count": "-1", "bucketCounts": ["-1", "0"], "explicitBounds": [5]},
             "hist_bucket_count_negative"),
        ]
        for payload, reason in cases:
            r = normalize(batch_of([{**base, **payload}], histogram=True, temporality=2))
            self.assertEqual(r.facts, [], payload)
            self.assertEqual(r.unconfirmed[0].reason, reason, payload)

    def test_21_duplicate_attribute_key_invalid(self):
        attrs = [{"key": "k", "value": {"stringValue": "a"}},
                 {"key": "k", "value": {"stringValue": "b"}}]
        r = normalize(batch_of([sum_point(0, 10, 1, attrs)], temporality=1))
        self.assertEqual(r.facts, [])
        self.assertEqual(r.unconfirmed[0].reason, "invalid_attributes")

    def test_22_non_monotonic_and_unsupported_rejected(self):
        r = normalize(batch_of([sum_point(0, 10, 1)], monotonic=False, temporality=1))
        self.assertEqual(r.unconfirmed[0].reason, "non_monotonic_sum")
        gauge = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [
            {"scope": {"name": "s", "version": "v1"}, "metrics": [
                {"name": "g", "unit": "1", "gauge": {"dataPoints": [
                    {"startTimeUnixNano": "0", "timeUnixNano": "10", "asInt": "1",
                     "attributes": []}]}}]}]}]}
        r2 = normalize(gauge)
        self.assertEqual(r2.unconfirmed[0].reason, "unsupported_metric_kind")

    def test_23_decimal_bounds_never_float(self):
        # 非整数边界走 Decimal，输出保留精确十进制文本
        p1 = {"startTimeUnixNano": "0", "timeUnixNano": "10", "attributes": [],
              "count": "1", "bucketCounts": ["1", "0"], "explicitBounds": [Decimal("0.1")],
              "sum": Decimal("0.30")}
        r = normalize(batch_of([p1], histogram=True, temporality=1))
        value = r.facts[0].value
        self.assertEqual(value["explicitBounds"], ["0.1"])
        self.assertEqual(value["sum"], "0.30")
        # 输出 JSON 中数值不出现二进制浮点尾数
        self.assertNotIn("0.30000000000000004", r.to_json())

    def test_24_multi_file_shared_identity_no_filename_picking(self):
        a = batch_of([sum_point(0, 10, 5, temp=1)], metric_name="m", temporality=1)
        b = batch_of([sum_point(0, 10, 5, temp=1)], metric_name="m", temporality=1)
        r = normalize([a, b])
        # 跨文件精确重复 -> 折叠为一条，而不是按文件名选一条
        self.assertEqual(len(r.facts), 1)
        c = batch_of([sum_point(0, 10, 9, temp=1)], metric_name="m", temporality=1)
        r2 = normalize([a, c])
        self.assertEqual(r2.facts, [])
        self.assertEqual(r2.unconfirmed[0].reason, "duplicate_identity_interval_conflict")

    def test_25_half_open_and_timestamps_as_strings_only(self):
        bad = batch_of([
            {"startTimeUnixNano": 10, "timeUnixNano": "20", "asInt": "1",
             "attributes": []}], temporality=1)
        r = normalize(bad)
        self.assertEqual(r.unconfirmed[0].reason, "invalid_timestamp")
        empty = batch_of([
            {"startTimeUnixNano": "10", "timeUnixNano": "10", "asInt": "1",
             "attributes": []}], temporality=1)
        r2 = normalize(empty)
        self.assertEqual(r2.unconfirmed[0].reason, "non_positive_interval")

    def test_26_histogram_bucket_regression_closes_epoch(self):
        p1 = {"startTimeUnixNano": "0", "timeUnixNano": "10", "attributes": [],
              "count": "3", "bucketCounts": ["1", "2"], "explicitBounds": [5]}
        p2 = {"startTimeUnixNano": "0", "timeUnixNano": "20", "attributes": [],
              "count": "2", "bucketCounts": ["0", "2"], "explicitBounds": [5]}
        r = normalize(batch_of([p1, p2], histogram=True, temporality=2))
        self.assertEqual(len(r.facts), 1)
        self.assertEqual(r.unconfirmed[0].reason, "monotonic_regression")
        # 同代后续点无法再恢复增量
        p3 = {"startTimeUnixNano": "0", "timeUnixNano": "30", "attributes": [],
              "count": "5", "bucketCounts": ["2", "3"], "explicitBounds": [5]}
        r2 = normalize(batch_of([p1, p2, p3], histogram=True, temporality=2))
        self.assertEqual(len(r2.facts), 1)
        self.assertEqual({u.reason for u in r2.unconfirmed},
                         {"monotonic_regression", "baseline_unavailable"})
        # 新 startTime 显式开启新计数代后恢复
        p4 = {"startTimeUnixNano": "30", "timeUnixNano": "40", "attributes": [],
              "count": "1", "bucketCounts": ["1", "0"], "explicitBounds": [5]}
        r3 = normalize(batch_of([p1, p2, p4], histogram=True, temporality=2))
        self.assertEqual(len(r3.facts), 2)
        self.assertEqual(r3.facts[-1].generation, 2)

    def test_27_delta_gap_starts_new_generation(self):
        pts = [sum_point(0, 10, 2, temp=1), sum_point(10, 20, 3, temp=1),
               sum_point(30, 40, 4, temp=1)]  # [20,30) 缺口
        r = normalize(batch_of(pts, temporality=1))
        gens = [(f.start_time, f.generation) for f in r.facts]
        self.assertEqual(gens, [(0, 1), (10, 1), (30, 2)])
        self.assertIn("gap_before_interval", r.facts[-1].quality)
        # 没有任何补零区间
        self.assertEqual([(f.start_time, f.end_time) for f in r.facts],
                         [(0, 10), (10, 20), (30, 40)])

    def test_28_cumulative_exact_duplicate_collapses(self):
        pts = [sum_point(0, 10, 2), sum_point(0, 10, 2),  # 乱序+精确重复
               sum_point(0, 20, 5)]
        r = normalize(batch_of(pts, temporality=2))
        self.assertEqual(len(r.facts), 2)
        self.assertEqual([f.value["asInt"] for f in sorted(r.facts, key=lambda f: f.start_time)],
                         ["2", "3"])
        self.assertEqual(sum(1 for o in r.origins if o.status == "collapsed"), 1)

    def test_29_epoch_connects_when_start_equals_prev_end(self):
        # startTime == 前一已确认 end（20）允许衔接；< 才是重叠代
        pts = [sum_point(0, 10, 4), sum_point(0, 20, 9), sum_point(20, 30, 3)]
        r = normalize(batch_of(pts, temporality=2))
        reasons = [u.reason for u in r.unconfirmed]
        self.assertEqual(reasons, [])
        self.assertEqual([f.generation for f in r.facts], [1, 1, 2])
        self.assertNotIn("gap_before_interval", r.facts[-1].quality)

    def test_30_resource_attrs_participate_in_identity(self):
        b1 = {
            "resourceMetrics": [{
                "resource": {"attributes": [
                    {"key": "host", "value": {"stringValue": "x"}}]},
                "scopeMetrics": [{"scope": {"name": "s", "version": "v"}, "metrics": []}]}]}
        b2 = {
            "resourceMetrics": [{
                "resource": {"attributes": [
                    {"key": "host", "value": {"stringValue": "y"}}]},
                "scopeMetrics": [{"scope": {"name": "s", "version": "v"}, "metrics": []}]}]}
        # 借用 batch_of 后替换 resource
        full1 = batch_of([sum_point(0, 10, 1, temp=1)], temporality=1)
        full2 = batch_of([sum_point(0, 10, 1, temp=1)], temporality=1)
        full1["resourceMetrics"][0]["resource"]["attributes"] = b1["resourceMetrics"][0]["resource"]["attributes"]
        full2["resourceMetrics"][0]["resource"]["attributes"] = b2["resourceMetrics"][0]["resource"]["attributes"]
        r = normalize([full1, full2])
        self.assertEqual(len({f.identity for f in r.facts}), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
