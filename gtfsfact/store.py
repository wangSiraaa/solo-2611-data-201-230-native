"""SQLite 输出、按实际时刻查询、以及事实表自检。

表结构：
- trip_instances(instance_id PK, trip_id, service_id, service_date, start_dep_secs,
  start_dep_ts, start_dep_epoch, source, trips_row, frequencies_row,
  UNIQUE(trip_id, service_date, start_dep_secs))
- stop_calls(instance_id FK -> trip_instances, stop_sequence, stop_id,
  arr_secs, dep_secs, arr_ts, dep_ts, arr_epoch, dep_epoch, stop_times_row,
  PRIMARY KEY(instance_id, stop_sequence))

秒数（*_secs）始终相对服务日午夜，可 >= 86400；*_ts 为 Asia/Shanghai 的 ISO 8601
本地时刻（跨午夜落到次日真实日期，但实例身份不变）；*_epoch 为 UTC  epoch 秒，
用于按实际时刻做半开区间查询。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .expand import Instance
from .gtfs import AGENCY_TIMEZONE, Feed

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(AGENCY_TIMEZONE)
except Exception:  # 无 tzdata 时退化为固定 +08:00（Asia/Shanghai 自 1991 年起无夏令时）
    TZ = timezone(timedelta(hours=8), AGENCY_TIMEZONE)

FORMAT_VERSION = "1"

SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE trip_instances (
    instance_id     INTEGER PRIMARY KEY,
    trip_id         TEXT NOT NULL,
    service_id      TEXT NOT NULL,
    service_date    TEXT NOT NULL,            -- YYYY-MM-DD，服务日（本地日期）
    start_dep_secs  INTEGER NOT NULL,         -- 实际首站发车秒（自服务日午夜，可 >= 86400）
    start_dep_ts    TEXT NOT NULL,            -- ISO 8601，Asia/Shanghai
    start_dep_epoch INTEGER NOT NULL,         -- UTC epoch 秒
    source          TEXT NOT NULL CHECK (source IN ('schedule', 'frequency')),
    trips_row       INTEGER NOT NULL,         -- trips.txt 逻辑行号
    frequencies_row INTEGER,                  -- frequencies.txt 逻辑行号（仅 frequency）
    UNIQUE (trip_id, service_date, start_dep_secs)
);
CREATE TABLE stop_calls (
    instance_id    INTEGER NOT NULL REFERENCES trip_instances (instance_id),
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arr_secs       INTEGER NOT NULL,
    dep_secs       INTEGER NOT NULL,
    arr_ts         TEXT NOT NULL,
    dep_ts         TEXT NOT NULL,
    arr_epoch      INTEGER NOT NULL,
    dep_epoch      INTEGER NOT NULL,
    stop_times_row INTEGER NOT NULL,          -- stop_times.txt 逻辑行号
    PRIMARY KEY (instance_id, stop_sequence)
);
CREATE INDEX idx_stop_calls_stop_time ON stop_calls (stop_id, arr_epoch, dep_epoch);
"""


def secs_to_local(service_date: date, secs: int) -> datetime:
    """服务日午夜（Asia/Shanghai）+ secs 秒 -> 带时区的真实时刻。"""
    base = datetime(service_date.year, service_date.month, service_date.day, tzinfo=TZ)
    return base + timedelta(seconds=secs)


def _ts_pair(service_date: date, secs: int) -> tuple[str, int]:
    ts = secs_to_local(service_date, secs)
    return ts.isoformat(), int(ts.timestamp())


def write_db(
    out_path: str | Path,
    feed: Feed,
    instances: list[Instance],
    service_start: date,
    service_end: date,
) -> tuple[int, int]:
    """原子写入：先写临时文件，全部成功后 os.replace；失败不留半份数据。

    返回 (实例数, 站点到发数)。
    """
    out_path = Path(out_path)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    n_calls = 0
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            with conn:
                conn.executescript(SCHEMA)
                meta = {
                    "format_version": FORMAT_VERSION,
                    "agency_id": feed.agency_id,
                    "agency_timezone": AGENCY_TIMEZONE,
                    "feed_dir": str(feed.feed_dir),
                    "service_start": service_start.isoformat(),
                    "service_end": service_end.isoformat(),
                    "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
                for instance_id, inst in enumerate(instances, start=1):
                    ts, epoch = _ts_pair(inst.service_date, inst.start_dep_secs)
                    conn.execute(
                        "INSERT INTO trip_instances (instance_id, trip_id, service_id,"
                        " service_date, start_dep_secs, start_dep_ts, start_dep_epoch,"
                        " source, trips_row, frequencies_row)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            instance_id,
                            inst.trip_id,
                            inst.service_id,
                            inst.service_date.isoformat(),
                            inst.start_dep_secs,
                            ts,
                            epoch,
                            inst.source,
                            inst.trips_row,
                            inst.frequencies_row,
                        ),
                    )
                    for call in inst.calls:
                        arr_ts, arr_epoch = _ts_pair(inst.service_date, call.arr_secs)
                        dep_ts, dep_epoch = _ts_pair(inst.service_date, call.dep_secs)
                        conn.execute(
                            "INSERT INTO stop_calls (instance_id, stop_sequence, stop_id,"
                            " arr_secs, dep_secs, arr_ts, dep_ts, arr_epoch, dep_epoch,"
                            " stop_times_row) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                instance_id,
                                call.stop_sequence,
                                call.stop_id,
                                call.arr_secs,
                                call.dep_secs,
                                arr_ts,
                                dep_ts,
                                arr_epoch,
                                dep_epoch,
                                call.stop_times_row,
                            ),
                        )
                        n_calls += 1
        finally:
            conn.close()
        os.replace(tmp_path, out_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return len(instances), n_calls


def query_stop_calls(
    db_path: str | Path, stop_id: str, from_epoch: int, to_epoch: int
) -> list[dict]:
    """按实际时刻半开区间 [from_epoch, to_epoch) 与 stop_id 读取 stop_calls。

    一个 stop_call 占用实际时段 [arr, dep]；当且仅当该时段与查询区间相交时返回
    （零停时 call 视为瞬时点 t，包含条件为 from <= t < to）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sc.instance_id, ti.trip_id, ti.service_id, ti.service_date,"
            " sc.stop_sequence, sc.stop_id, sc.arr_secs, sc.dep_secs,"
            " sc.arr_ts, sc.dep_ts, sc.arr_epoch, sc.dep_epoch, sc.stop_times_row"
            " FROM stop_calls sc JOIN trip_instances ti USING (instance_id)"
            " WHERE sc.stop_id = ? AND sc.arr_epoch < ? AND sc.dep_epoch >= ?"
            " ORDER BY sc.arr_epoch, sc.instance_id, sc.stop_sequence",
            (stop_id, to_epoch, from_epoch),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def verify_db(db_path: str | Path) -> list[CheckResult]:
    """对已生成的数据库做结构自检；全部通过才应返回全 ok 的列表。"""
    results: list[CheckResult] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(CheckResult(name, ok, detail))

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        for key in ("format_version", "agency_timezone", "service_start", "service_end"):
            check(f"meta.{key} 存在", key in meta, meta.get(key, "缺失"))
        if meta.get("agency_timezone") != AGENCY_TIMEZONE:
            check("meta.agency_timezone 合法", False, meta.get("agency_timezone", "缺失"))

        instances = conn.execute(
            "SELECT * FROM trip_instances ORDER BY instance_id"
        ).fetchall()
        calls = conn.execute(
            "SELECT * FROM stop_calls ORDER BY instance_id, stop_sequence"
        ).fetchall()

        orphan = conn.execute(
            "SELECT COUNT(*) c FROM stop_calls sc"
            " LEFT JOIN trip_instances ti USING (instance_id)"
            " WHERE ti.instance_id IS NULL"
        ).fetchone()["c"]
        check("stop_calls 全部可连接回 trip_instances", orphan == 0, f"孤儿行 {orphan}")

        dup = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT trip_id, service_date, start_dep_secs"
            " FROM trip_instances GROUP BY 1, 2, 3 HAVING COUNT(*) > 1)"
        ).fetchone()["c"]
        check("实例身份 (trip_id, service_date, start_dep_secs) 唯一", dup == 0, f"重复 {dup}")

        by_inst: dict[int, list[sqlite3.Row]] = {}
        for c in calls:
            by_inst.setdefault(c["instance_id"], []).append(c)

        range_ok = True
        mono_bad: list[str] = []
        empty_inst: list[int] = []
        ts_bad: list[str] = []
        src_bad: list[int] = []
        svc_lo = date.fromisoformat(meta.get("service_start", "0001-01-01"))
        svc_hi = date.fromisoformat(meta.get("service_end", "9999-12-31"))
        for inst in instances:
            iid = inst["instance_id"]
            svc_date = date.fromisoformat(inst["service_date"])
            if not (svc_lo <= svc_date <= svc_hi):
                range_ok = False
            inst_calls = by_inst.get(iid, [])
            if not inst_calls:
                empty_inst.append(iid)
                continue
            # 时序：stop_sequence 严格递增；同站 arr<=dep；后站到达不早于前站离开
            for prev, cur in zip(inst_calls, inst_calls[1:]):
                if cur["stop_sequence"] <= prev["stop_sequence"]:
                    mono_bad.append(f"实例 {iid} stop_sequence 非递增")
                if cur["arr_secs"] < prev["dep_secs"]:
                    mono_bad.append(
                        f"实例 {iid} seq {cur['stop_sequence']} 到达早于前站离开"
                    )
            for c in inst_calls:
                if c["arr_secs"] > c["dep_secs"]:
                    mono_bad.append(f"实例 {iid} seq {c['stop_sequence']} 到达晚于离开")
            # 首站发车秒必须与实例身份一致
            first = inst_calls[0]
            if first["dep_secs"] != inst["start_dep_secs"]:
                mono_bad.append(f"实例 {iid} start_dep_secs 与首站离开不一致")
            # 秒数与 ISO 时刻 / epoch 互相一致（Asia/Shanghai）
            for c in inst_calls:
                for col_secs, col_ts, col_epoch in (
                    ("arr_secs", "arr_ts", "arr_epoch"),
                    ("dep_secs", "dep_ts", "dep_epoch"),
                ):
                    ts, epoch = _ts_pair(svc_date, c[col_secs])
                    if ts != c[col_ts] or epoch != c[col_epoch]:
                        ts_bad.append(f"实例 {iid} seq {c['stop_sequence']} {col_ts}")
            ts, epoch = _ts_pair(svc_date, inst["start_dep_secs"])
            if ts != inst["start_dep_ts"] or epoch != inst["start_dep_epoch"]:
                ts_bad.append(f"实例 {iid} start_dep_ts")
            if (inst["source"] == "frequency") != (inst["frequencies_row"] is not None):
                src_bad.append(iid)
        check("每个实例至少含 1 条 stop_call", not empty_inst, f"空实例 {empty_inst}")
        check("实例 service_date 均落在构建区间内", range_ok)
        check("实例内 stop_sequence 递增且到发时序单调", not mono_bad, "; ".join(mono_bad[:5]))
        check("秒数与 ISO 时刻 / epoch 一致", not ts_bad, "; ".join(ts_bad[:5]))
        check("source 与 frequencies_row 一致", not src_bad, f"异常实例 {src_bad}")
    finally:
        conn.close()
    return results
