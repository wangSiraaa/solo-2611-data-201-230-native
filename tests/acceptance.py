#!/usr/bin/env python3
"""独立验收断言（仅标准库）：python3 tests/acceptance.py 或 python3 -m unittest tests.acceptance

覆盖题面样例：普通服务、周末新增、工作日取消、仅例外服务、跨午夜、重复站点、
两段频率；核对每趟站数、时序、频率生成个数、真实时刻与来源行号；
并对矛盾例外、未知外键、重复 stop_sequence、非法频率等无效输入断言整体拒绝。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gtfsfact import BuildError, expand, load_feed, query_stop_calls, verify_db, write_db  # noqa: E402
from gtfsfact.store import secs_to_local  # noqa: E402

FEED = REPO / "fixtures" / "feed"
START_STR, END_STR = "2026-01-01", "2026-01-03"

from datetime import date  # noqa: E402

START, END = date(2026, 1, 1), date(2026, 1, 3)


def build_db(feed_dir: Path, out: Path, start: date = START, end: date = END) -> None:
    feed = load_feed(feed_dir)
    instances = expand(feed, start, end)
    write_db(out, feed, instances, start, end)


def rows(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


class FixtureBuildTest(unittest.TestCase):
    """对 fixtures/feed 在 [2026-01-01, 2026-01-03] 闭区间上的正向验收。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtfsfact_ok_"))
        cls.db = cls.tmp / "fact.sqlite"
        build_db(FEED, cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def instances(self) -> list[tuple]:
        return rows(
            self.db,
            "SELECT instance_id, trip_id, service_id, service_date, start_dep_secs,"
            " start_dep_ts, source, trips_row, frequencies_row FROM trip_instances"
            " ORDER BY instance_id",
        )

    def calls(self, instance_id: int) -> list[tuple]:
        return rows(
            self.db,
            "SELECT stop_sequence, stop_id, arr_secs, dep_secs, arr_ts, dep_ts, stop_times_row"
            " FROM stop_calls WHERE instance_id = ? ORDER BY stop_sequence",
            (instance_id,),
        )

    def test_counts(self) -> None:
        self.assertEqual(7, len(self.instances()))
        self.assertEqual(16, rows(self.db, "SELECT COUNT(*) FROM stop_calls")[0][0])

    def test_service_dates_weekday_cancel_and_weekend_add(self) -> None:
        # weekday：周一~周五为基本盘；2026-01-02(周五)被 exception_type=2 取消，
        # 2026-01-03(周六)被 exception_type=1 新增 => {01-01, 01-03}
        dates = {
            r[0]
            for r in rows(
                self.db, "SELECT DISTINCT service_date FROM trip_instances WHERE service_id='weekday'"
            )
        }
        self.assertEqual({"2026-01-01", "2026-01-03"}, dates)

    def test_exception_only_service(self) -> None:
        # special 没有 calendar 行，仅由 calendar_dates 例外定义 => {2026-01-02}
        dates = {
            r[0]
            for r in rows(
                self.db, "SELECT DISTINCT service_date FROM trip_instances WHERE service_id='special'"
            )
        }
        self.assertEqual({"2026-01-02"}, dates)

    def test_night_trip_cross_midnight_repeated_stop(self) -> None:
        night = [i for i in self.instances() if i[1] == "t_night"]
        self.assertEqual(2, len(night))
        for iid, _, _, svc_date, start_dep, start_ts, source, trips_row, freq_row in night:
            self.assertEqual(86160, start_dep)  # 23:56:00，首站 departure_time
            self.assertEqual("schedule", source)
            self.assertEqual(1, trips_row)  # trips.txt 第 1 个数据行
            self.assertIsNone(freq_row)
            self.assertEqual(f"{svc_date}T23:56:00+08:00", start_ts)
            cs = self.calls(iid)
            self.assertEqual(3, len(cs))  # 每趟 3 站
            # 按数值 stop_sequence 排序（CSV 行序是 30,10,20），允许 s1 重复访问
            self.assertEqual([10, 20, 30], [c[0] for c in cs])
            self.assertEqual(["s1", "s2", "s1"], [c[1] for c in cs])
            self.assertEqual(
                [(86100, 86160), (86700, 86760), (87600, 87660)],
                [(c[2], c[3]) for c in cs],
            )
            # 跨午夜：seq 30 落在真实次日 00:20，但仍是同一实例
            next_day = date.fromisoformat(svc_date).toordinal() + 1
            self.assertEqual(
                f"{date.fromordinal(next_day)}T00:20:00+08:00", cs[2][4]
            )
            # 来源逻辑行号：stop_times.txt 中行序为 seq30=行1, seq10=行2, seq20=行3
            self.assertEqual([2, 3, 1], [c[6] for c in cs])

    def test_frequency_two_windows(self) -> None:
        freq = [i for i in self.instances() if i[1] == "t_freq"]
        # [09:00,09:30)/600 -> 3 班；[10:00,10:20)/600 -> 2 班；模板 08:02 不输出
        self.assertEqual([32400, 33000, 33600, 36000, 36600], [i[4] for i in freq])
        self.assertTrue(all(i[3] == "2026-01-02" for i in freq))
        self.assertTrue(all(i[6] == "frequency" for i in freq))
        self.assertEqual([1, 1, 1, 2, 2], [i[8] for i in freq])  # frequencies.txt 行号
        self.assertNotIn(28920, [i[4] for i in freq])  # 模板首站发车 08:02:00 不输出
        for _, _, _, svc_date, start_dep, _, _, _, _ in freq:
            cs = self.calls(self._iid(svc_date, start_dep))
            self.assertEqual(2, len(cs))  # 每趟 2 站
            # 相对首站 departure 的偏移平移：arr-120/dep+0，arr+600/dep+660
            self.assertEqual(
                [(start_dep - 120, start_dep), (start_dep + 600, start_dep + 660)],
                [(c[2], c[3]) for c in cs],
            )
            self.assertEqual([4, 5], [c[6] for c in cs])  # stop_times.txt 行号

    def _iid(self, service_date: str, start_dep: int) -> int:
        got = rows(
            self.db,
            "SELECT instance_id FROM trip_instances WHERE service_date=? AND start_dep_secs=?",
            (service_date, start_dep),
        )
        self.assertEqual(1, len(got))
        return got[0][0]

    def test_real_timestamps(self) -> None:
        # 秒数 -> 真实时刻（Asia/Shanghai，固定 +08:00）抽查
        iid = self._iid("2026-01-02", 32400)
        cs = self.calls(iid)
        self.assertEqual("2026-01-02T08:58:00+08:00", cs[0][4])
        self.assertEqual("2026-01-02T09:00:00+08:00", cs[0][5])
        self.assertEqual("2026-01-02T09:10:00+08:00", cs[1][4])
        self.assertEqual("2026-01-02T09:11:00+08:00", cs[1][5])

    def test_all_calls_join_back_to_same_instance(self) -> None:
        orphan = rows(
            self.db,
            "SELECT COUNT(*) FROM stop_calls sc LEFT JOIN trip_instances ti"
            " USING (instance_id) WHERE ti.instance_id IS NULL",
        )[0][0]
        self.assertEqual(0, orphan)
        dup = rows(
            self.db,
            "SELECT COUNT(*) FROM (SELECT trip_id, service_date, start_dep_secs"
            " FROM trip_instances GROUP BY 1,2,3 HAVING COUNT(*)>1)",
        )[0][0]
        self.assertEqual(0, dup)

    def test_verify_db_passes(self) -> None:
        results = verify_db(self.db)
        failed = [r for r in results if not r.ok]
        self.assertEqual([], [f"{r.name}: {r.detail}" for r in failed])

    def test_query_cross_midnight(self) -> None:
        # 半开区间跨真实午夜：t_night@01-01 的 seq10 与 seq30（次日 00:20）都命中同一实例
        lo = int(secs_to_local(date(2026, 1, 1), 23 * 3600).timestamp())
        hi = int(secs_to_local(date(2026, 1, 2), 2 * 3600).timestamp())
        got = query_stop_calls(self.db, "s1", lo, hi)
        self.assertEqual(2, len(got))
        self.assertEqual({got[0]["instance_id"], got[1]["instance_id"]}, {got[0]["instance_id"]})
        self.assertEqual([10, 30], [g["stop_sequence"] for g in got])
        self.assertEqual("2026-01-02T00:20:00+08:00", got[1]["arr_ts"])

    def test_query_frequency_morning(self) -> None:
        lo = int(secs_to_local(date(2026, 1, 2), 8 * 3600).timestamp())
        hi = int(secs_to_local(date(2026, 1, 2), 11 * 3600).timestamp())
        got = query_stop_calls(self.db, "s1", lo, hi)
        self.assertEqual(5, len(got))
        self.assertEqual(
            ["08:58", "09:08", "09:18", "09:58", "10:08"],
            [g["arr_ts"][11:16] for g in got],
        )

    def test_query_respects_cancellation_and_half_open(self) -> None:
        # 01-02(周五) 被 exception_type=2 取消：01-02 当夜发车窗口内 s1 无任何到发
        lo = int(secs_to_local(date(2026, 1, 2), 23 * 3600).timestamp())
        hi = int(secs_to_local(date(2026, 1, 3), 2 * 3600).timestamp())
        self.assertEqual([], query_stop_calls(self.db, "s1", lo, hi))
        # 但 01-02 凌晨 00:05 的 s2 到发属于 01-01 服务日的跨午夜实例，必须存在
        lo = int(secs_to_local(date(2026, 1, 2), 0).timestamp())
        hi = int(secs_to_local(date(2026, 1, 2), 3600).timestamp())
        got = query_stop_calls(self.db, "s2", lo, hi)
        self.assertEqual(1, len(got))
        self.assertEqual("2026-01-01", got[0]["service_date"])
        # 半开边界：终点恰好等于到达时刻则不命中
        arr_epoch = got[0]["arr_epoch"]
        self.assertEqual([], query_stop_calls(self.db, "s2", lo, arr_epoch))
        self.assertEqual(1, len(query_stop_calls(self.db, "s2", lo, arr_epoch + 1)))


class RejectTest(unittest.TestCase):
    """无效输入必须整体拒绝：抛 BuildError 且不产生输出文件。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gtfsfact_bad_"))
        self.feed = self.tmp / "feed"
        shutil.copytree(FEED, self.feed)
        self.out = self.tmp / "out.sqlite"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mutate(self, name: str, content: str | None) -> None:
        path = self.feed / name
        if content is None:
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8")

    def read(self, name: str) -> str:
        return (self.feed / name).read_text(encoding="utf-8")

    def assert_rejected(self, needle: str) -> None:
        with self.assertRaises(BuildError) as ctx:
            build_db(self.feed, self.out)
        self.assertIn(needle, str(ctx.exception))
        self.assertFalse(self.out.exists(), "失败构建不得留下输出文件")
        self.assertFalse(
            Path(str(self.out) + ".tmp").exists(), "失败构建不得留下临时文件"
        )

    def test_overlapping_frequencies(self) -> None:
        self.mutate("frequencies.txt", (REPO / "fixtures" / "invalid_frequencies.txt").read_text(encoding="utf-8"))
        self.assert_rejected("重叠")

    def test_exact_times_zero_unsupported(self) -> None:
        self.mutate(
            "frequencies.txt",
            "trip_id,start_time,end_time,headway_secs,exact_times\n"
            "t_freq,09:00:00,09:30:00,600,0\n",
        )
        self.assert_rejected("exact_times")

    def test_non_positive_headway(self) -> None:
        self.mutate(
            "frequencies.txt",
            "trip_id,start_time,end_time,headway_secs,exact_times\n"
            "t_freq,09:00:00,09:30:00,0,1\n",
        )
        self.assert_rejected("headway_secs")

    def test_contradictory_exceptions(self) -> None:
        self.mutate(
            "calendar_dates.txt",
            self.read("calendar_dates.txt") + "special,20260102,2\n",
        )
        self.assert_rejected("矛盾")

    def test_duplicate_stop_sequence(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt") + "t_night,25:00:00,25:01:00,s2,20\n",
        )
        self.assert_rejected("stop_sequence 20 重复")

    def test_unknown_trip_in_stop_times(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt") + "t_ghost,01:00:00,01:01:00,s1,1\n",
        )
        self.assert_rejected("未知外键 trip_id")

    def test_unknown_service_in_trips(self) -> None:
        self.mutate(
            "trips.txt", self.read("trips.txt").replace("r1,special,t_freq", "r1,ghost,t_freq")
        )
        self.assert_rejected("未知外键 service_id")

    def test_unknown_stop_in_stop_times(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt").replace("t_freq,08:12:00,08:13:00,s2,2", "t_freq,08:12:00,08:13:00,s9,2"),
        )
        self.assert_rejected("未知外键 stop_id")

    def test_unknown_route_in_trips(self) -> None:
        self.mutate("trips.txt", self.read("trips.txt").replace("r1,weekday", "r9,weekday"))
        self.assert_rejected("未知外键 route_id")

    def test_hour_above_47(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt").replace("t_night,24:20:00,24:21:00", "t_night,48:00:00,48:01:00"),
        )
        self.assert_rejected("上限")

    def test_arrival_after_departure_same_stop(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt").replace("t_night,23:55:00,23:56:00", "t_night,23:57:00,23:56:00"),
        )
        self.assert_rejected("到达")

    def test_next_arrival_before_previous_departure(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt").replace("t_night,24:05:00,24:06:00", "t_night,23:50:00,24:06:00"),
        )
        self.assert_rejected("早于前一站")

    def test_trip_without_stop_times(self) -> None:
        self.mutate("trips.txt", self.read("trips.txt") + "r1,weekday,t_empty\n")
        self.assert_rejected("没有任何 stop_times")

    def test_duplicate_calendar_service(self) -> None:
        self.mutate(
            "calendar.txt",
            self.read("calendar.txt") + "weekday,0,0,0,0,0,1,1,20260101,20260131\n",
        )
        self.assert_rejected("重复")

    def test_invalid_time_format(self) -> None:
        self.mutate(
            "stop_times.txt",
            self.read("stop_times.txt").replace("t_night,23:55:00,23:56:00", "t_night,23:55,23:56:00"),
        )
        self.assert_rejected("无效时间")


class CliTest(unittest.TestCase):
    """命令行端到端：build / query / verify，以及无效区间拒绝。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gtfsfact_cli_"))
        self.db = self.tmp / "cli.sqlite"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "gtfsfact", *argv],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_build_query_verify_roundtrip(self) -> None:
        r = self.run_cli(
            "build", "--config", "fixtures/build.json", "--out", str(self.db)
        )
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("trip_instances: 7", r.stdout)

        r = self.run_cli("verify", "--db", str(self.db))
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("全部通过", r.stdout)

        r = self.run_cli(
            "query", "--db", str(self.db), "--stop", "s1",
            "--from", "2026-01-01T23:00:00+08:00", "--to", "2026-01-02T02:00:00+08:00",
        )
        self.assertEqual(0, r.returncode, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(2, len(got))
        self.assertEqual([10, 30], [g["stop_sequence"] for g in got])

    def test_build_rejects_inverted_range(self) -> None:
        r = self.run_cli(
            "build", "--feed", "fixtures/feed",
            "--start", "2026-01-03", "--end", "2026-01-01", "--out", str(self.db),
        )
        self.assertEqual(2, r.returncode)
        self.assertIn("区间无效", r.stderr)
        self.assertFalse(self.db.exists())

    def test_verify_detects_tampered_db(self) -> None:
        r = self.run_cli(
            "build", "--config", "fixtures/build.json", "--out", str(self.db)
        )
        self.assertEqual(0, r.returncode, r.stderr)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE stop_calls SET arr_epoch = arr_epoch + 1 WHERE rowid = 1")
        conn.commit()
        conn.close()
        r = self.run_cli("verify", "--db", str(self.db))
        self.assertEqual(1, r.returncode)
        self.assertIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
