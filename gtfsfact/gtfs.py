"""读取并校验 GTFS CSV 文件，产出内存中的 Feed。

约定：
- 逻辑行号 = 各 CSV 文件内数据行的 1 基序号（表头不计，空行不计）。
- 时间 HH:MM:SS 中 HH 允许 00..47（可跨午夜，仍属原服务日）。
- 所有校验失败都抛 BuildError / UnsupportedError，不静默任选、不部分接受。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .errors import BuildError, UnsupportedError

AGENCY_TIMEZONE = "Asia/Shanghai"
MAX_HOUR = 47  # 题面上限：HH 可大于 23，最大 47

WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_HMS_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")
_DATE8_RE = re.compile(r"^\d{8}$")
_UINT_RE = re.compile(r"^\d+$")


def parse_hms(raw: str, where: str) -> int:
    """HH:MM:SS -> 自服务日午夜起的秒数；HH 允许到 47。"""
    m = _HMS_RE.match(raw)
    if not m:
        raise BuildError(f"{where}: 无效时间 {raw!r}，应为 HH:MM:SS（分秒须为 00-59）")
    hh, mm, ss = (int(g) for g in m.groups())
    if hh > MAX_HOUR:
        raise BuildError(f"{where}: 小时 {hh} 超过支持上限 {MAX_HOUR}")
    return hh * 3600 + mm * 60 + ss


def parse_date8(raw: str, where: str) -> date:
    if not _DATE8_RE.match(raw):
        raise BuildError(f"{where}: 无效日期 {raw!r}，应为 YYYYMMDD")
    try:
        return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError as exc:
        raise BuildError(f"{where}: 无效日期 {raw!r}: {exc}") from exc


def parse_uint(raw: str, where: str) -> int:
    if not _UINT_RE.match(raw):
        raise BuildError(f"{where}: 应为非负整数，得到 {raw!r}")
    return int(raw)


def _require(value: str, where: str, col: str) -> str:
    if value == "":
        raise BuildError(f"{where}: 必填列 {col} 为空")
    return value


def read_csv(path: Path, required_cols: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    """返回 [(逻辑行号, {列名: 值})]；空行跳过且不占行号。"""
    rows: list[tuple[int, dict[str, str]]] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = [c.strip() for c in next(reader)]
        except StopIteration:
            raise BuildError(f"{path.name}: 空文件（缺少表头）") from None
        if len(set(header)) != len(header):
            raise BuildError(f"{path.name}: 表头存在重复列")
        missing = [c for c in required_cols if c not in header]
        if missing:
            raise BuildError(f"{path.name}: 缺少必填列: {', '.join(missing)}")
        rowno = 0
        for raw in reader:
            if not raw or all(c.strip() == "" for c in raw):
                continue
            rowno += 1
            if len(raw) > len(header):
                raise BuildError(f"{path.name} 第 {rowno} 行: 字段数多于表头")
            rec = {h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(header)}
            rows.append((rowno, rec))
    return rows


@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    row: int


@dataclass(frozen=True)
class StopTime:
    trip_id: str
    arr_secs: int
    dep_secs: int
    stop_id: str
    stop_sequence: int
    row: int


@dataclass(frozen=True)
class CalendarRow:
    service_id: str
    weekdays: tuple[bool, bool, bool, bool, bool, bool, bool]  # 周一..周日
    start: date
    end: date
    row: int


@dataclass(frozen=True)
class Frequency:
    trip_id: str
    start_secs: int
    end_secs: int  # 半开区间 [start, end)
    headway_secs: int
    row: int


@dataclass
class Feed:
    feed_dir: Path
    agency_id: str
    stops: set[str]
    routes: set[str]
    trips: dict[str, Trip]
    stop_times: dict[str, list[StopTime]]  # 按 trip 分组，已按 stop_sequence 数值排序
    calendars: dict[str, CalendarRow]
    exceptions: dict[str, dict[date, int]]  # service_id -> date -> exception_type
    frequencies: dict[str, list[Frequency]]  # 按 trip 分组，已按 start_secs 排序


def _load_agency(feed_dir: Path) -> str:
    rows = read_csv(feed_dir / "agency.txt", ("agency_name", "agency_timezone"))
    if len(rows) != 1:
        raise BuildError(f"agency.txt: 本实现固定单一 agency，得到 {len(rows)} 行")
    rowno, rec = rows[0]
    tz = _require(rec["agency_timezone"], f"agency.txt 第 {rowno} 行", "agency_timezone")
    if tz != AGENCY_TIMEZONE:
        raise BuildError(
            f"agency.txt 第 {rowno} 行: agency_timezone={tz!r}，本实现固定为 {AGENCY_TIMEZONE!r}"
        )
    return rec.get("agency_id", "")


def _load_stops(feed_dir: Path) -> set[str]:
    stops: set[str] = set()
    for rowno, rec in read_csv(feed_dir / "stops.txt", ("stop_id",)):
        sid = _require(rec["stop_id"], f"stops.txt 第 {rowno} 行", "stop_id")
        if sid in stops:
            raise BuildError(f"stops.txt 第 {rowno} 行: stop_id {sid!r} 重复")
        stops.add(sid)
    return stops


def _load_routes(feed_dir: Path, agency_id: str) -> set[str]:
    routes: set[str] = set()
    for rowno, rec in read_csv(feed_dir / "routes.txt", ("route_id",)):
        rid = _require(rec["route_id"], f"routes.txt 第 {rowno} 行", "route_id")
        if rid in routes:
            raise BuildError(f"routes.txt 第 {rowno} 行: route_id {rid!r} 重复")
        ref = rec.get("agency_id", "")
        if ref and agency_id and ref != agency_id:
            raise BuildError(
                f"routes.txt 第 {rowno} 行: agency_id {ref!r} 与唯一 agency {agency_id!r} 不一致"
            )
        routes.add(rid)
    return routes


def _load_calendar(feed_dir: Path) -> dict[str, CalendarRow]:
    path = feed_dir / "calendar.txt"
    calendars: dict[str, CalendarRow] = {}
    if not path.exists():
        return calendars
    required = ("service_id",) + WEEKDAY_COLUMNS + ("start_date", "end_date")
    for rowno, rec in read_csv(path, required):
        where = f"calendar.txt 第 {rowno} 行"
        sid = _require(rec["service_id"], where, "service_id")
        if sid in calendars:
            raise BuildError(f"{where}: service_id {sid!r} 在 calendar.txt 中重复")
        flags = []
        for col in WEEKDAY_COLUMNS:
            v = _require(rec[col], where, col)
            if v not in ("0", "1"):
                raise BuildError(f"{where}: {col} 应为 0/1，得到 {v!r}")
            flags.append(v == "1")
        start = parse_date8(_require(rec["start_date"], where, "start_date"), where)
        end = parse_date8(_require(rec["end_date"], where, "end_date"), where)
        if start > end:
            raise BuildError(f"{where}: start_date {start} 晚于 end_date {end}")
        calendars[sid] = CalendarRow(sid, tuple(flags), start, end, rowno)
    return calendars


def _load_calendar_dates(feed_dir: Path) -> dict[str, dict[date, int]]:
    path = feed_dir / "calendar_dates.txt"
    exceptions: dict[str, dict[date, int]] = {}
    if not path.exists():
        return exceptions
    for rowno, rec in read_csv(path, ("service_id", "date", "exception_type")):
        where = f"calendar_dates.txt 第 {rowno} 行"
        sid = _require(rec["service_id"], where, "service_id")
        day = parse_date8(_require(rec["date"], where, "date"), where)
        raw_type = _require(rec["exception_type"], where, "exception_type")
        if raw_type not in ("1", "2"):
            raise BuildError(f"{where}: exception_type 应为 1(新增)/2(删除)，得到 {raw_type!r}")
        etype = int(raw_type)
        per_service = exceptions.setdefault(sid, {})
        prev = per_service.get(day)
        if prev is not None and prev != etype:
            raise BuildError(
                f"{where}: service_id={sid!r} date={day.isoformat()} 存在互相矛盾的例外 "
                f"(exception_type {prev} 与 {etype})，拒绝静默任选"
            )
        per_service[day] = etype  # 完全相同的重复例外是幂等的，予以合并
    return exceptions


def _load_trips(feed_dir: Path, routes: set[str], service_ids: set[str]) -> dict[str, Trip]:
    trips: dict[str, Trip] = {}
    for rowno, rec in read_csv(feed_dir / "trips.txt", ("route_id", "service_id", "trip_id")):
        where = f"trips.txt 第 {rowno} 行"
        route_id = _require(rec["route_id"], where, "route_id")
        service_id = _require(rec["service_id"], where, "service_id")
        trip_id = _require(rec["trip_id"], where, "trip_id")
        if trip_id in trips:
            raise BuildError(f"{where}: trip_id {trip_id!r} 重复")
        if route_id not in routes:
            raise BuildError(f"{where}: 未知外键 route_id {route_id!r}（routes.txt 中不存在）")
        if service_id not in service_ids:
            raise BuildError(
                f"{where}: 未知外键 service_id {service_id!r}"
                "（calendar.txt 与 calendar_dates.txt 中均不存在）"
            )
        trips[trip_id] = Trip(trip_id, route_id, service_id, rowno)
    return trips


def _load_stop_times(feed_dir: Path, trips: dict[str, Trip], stops: set[str]) -> dict[str, list[StopTime]]:
    required = ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence")
    by_trip: dict[str, list[StopTime]] = {}
    for rowno, rec in read_csv(feed_dir / "stop_times.txt", required):
        where = f"stop_times.txt 第 {rowno} 行"
        trip_id = _require(rec["trip_id"], where, "trip_id")
        stop_id = _require(rec["stop_id"], where, "stop_id")
        if trip_id not in trips:
            raise BuildError(f"{where}: 未知外键 trip_id {trip_id!r}（trips.txt 中不存在）")
        if stop_id not in stops:
            raise BuildError(f"{where}: 未知外键 stop_id {stop_id!r}（stops.txt 中不存在）")
        arr = parse_hms(_require(rec["arrival_time"], where, "arrival_time"), where)
        dep = parse_hms(_require(rec["departure_time"], where, "departure_time"), where)
        seq = parse_uint(_require(rec["stop_sequence"], where, "stop_sequence"), where)
        by_trip.setdefault(trip_id, []).append(StopTime(trip_id, arr, dep, stop_id, seq, rowno))

    for trip_id, trip in trips.items():
        sts = by_trip.get(trip_id)
        if not sts:
            raise BuildError(f"trip {trip_id!r}（trips.txt 第 {trip.row} 行）没有任何 stop_times 行")
        # 按数值 stop_sequence 排序；禁止同 trip 重复 stop_sequence；允许站点重复访问
        sts.sort(key=lambda st: st.stop_sequence)
        for prev, cur in zip(sts, sts[1:]):
            if cur.stop_sequence == prev.stop_sequence:
                raise BuildError(
                    f"trip {trip_id!r}: stop_sequence {cur.stop_sequence} 重复"
                    f"（stop_times.txt 第 {prev.row} 行与第 {cur.row} 行），拒绝静默任选"
                )
        for st in sts:
            if st.arr_secs > st.dep_secs:
                raise BuildError(
                    f"stop_times.txt 第 {st.row} 行: 同站到达 {st.arr_secs} 晚于离开 {st.dep_secs}"
                )
        for prev, cur in zip(sts, sts[1:]):
            if cur.arr_secs < prev.dep_secs:
                raise BuildError(
                    f"trip {trip_id!r}: stop_sequence {cur.stop_sequence} 的到达 {cur.arr_secs}"
                    f" 早于前一站（stop_sequence {prev.stop_sequence}）的离开 {prev.dep_secs}"
                )
    return by_trip


def _load_frequencies(feed_dir: Path, trips: dict[str, Trip]) -> dict[str, list[Frequency]]:
    path = feed_dir / "frequencies.txt"
    by_trip: dict[str, list[Frequency]] = {}
    if not path.exists():
        return by_trip
    required = ("trip_id", "start_time", "end_time", "headway_secs")
    for rowno, rec in read_csv(path, required):
        where = f"frequencies.txt 第 {rowno} 行"
        trip_id = _require(rec["trip_id"], where, "trip_id")
        if trip_id not in trips:
            raise BuildError(f"{where}: 未知外键 trip_id {trip_id!r}（trips.txt 中不存在）")
        start = parse_hms(_require(rec["start_time"], where, "start_time"), where)
        end = parse_hms(_require(rec["end_time"], where, "end_time"), where)
        headway = parse_uint(_require(rec["headway_secs"], where, "headway_secs"), where)
        exact = rec.get("exact_times", "")
        if exact != "1":
            shown = exact if exact else "(缺省，按 0 处理)"
            raise UnsupportedError(
                f"{where}: exact_times={shown} 不支持：仅支持 exact_times=1 的精确班次，"
                "不能按班距编造确定班次"
            )
        if headway <= 0:
            raise UnsupportedError(f"{where}: headway_secs 必须为正整数，得到 {headway}")
        if start >= end:
            raise BuildError(f"{where}: start_time {start} 不早于 end_time {end}（区间为空）")
        by_trip.setdefault(trip_id, []).append(Frequency(trip_id, start, end, headway, rowno))

    for trip_id, freqs in by_trip.items():
        freqs.sort(key=lambda f: (f.start_secs, f.end_secs))
        for prev, cur in zip(freqs, freqs[1:]):
            if cur.start_secs < prev.end_secs:
                raise UnsupportedError(
                    f"trip {trip_id!r}: 频率区间 "
                    f"[{_fmt(prev.start_secs)}, {_fmt(prev.end_secs)})（第 {prev.row} 行）与 "
                    f"[{_fmt(cur.start_secs)}, {_fmt(cur.end_secs)})（第 {cur.row} 行）重叠，"
                    "重叠区间不支持，不能编造确定班次"
                )
    return by_trip


def _fmt(secs: int) -> str:
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def load_feed(feed_dir: str | Path) -> Feed:
    """读取并完整校验一个 feed 目录；任何无效输入都抛 BuildError。"""
    feed_dir = Path(feed_dir)
    if not feed_dir.is_dir():
        raise BuildError(f"feed 目录不存在: {feed_dir}")
    for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
        if not (feed_dir / name).exists():
            raise BuildError(f"缺少必需文件: {feed_dir / name}")

    agency_id = _load_agency(feed_dir)
    stops = _load_stops(feed_dir)
    routes = _load_routes(feed_dir, agency_id)
    calendars = _load_calendar(feed_dir)
    exceptions = _load_calendar_dates(feed_dir)
    service_ids = set(calendars) | set(exceptions)
    trips = _load_trips(feed_dir, routes, service_ids)
    stop_times = _load_stop_times(feed_dir, trips, stops)
    frequencies = _load_frequencies(feed_dir, trips)
    return Feed(
        feed_dir=feed_dir,
        agency_id=agency_id,
        stops=stops,
        routes=routes,
        trips=trips,
        stop_times=stop_times,
        calendars=calendars,
        exceptions=exceptions,
        frequencies=frequencies,
    )
