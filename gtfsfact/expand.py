"""把校验过的 Feed 按服务日期闭区间展开为 trip 实例与站点到发事实。

规则：
- 服务日 = calendar 星期标志 ∩ [start_date, end_date]，再应用 calendar_dates 例外
  （exception_type=1 新增、=2 删除，例外优先于星期标志）；允许仅由例外定义的 service_id。
- 无 frequencies 的 trip：每个服务日生成 1 个实例，模板时刻即实际时刻。
- 有 frequencies 的 trip：按 [start_time, end_time) 以 headway_secs 生成实际首站发车秒，
  其余各站到发按相对首站 departure_time 的偏移平移；模板本身不输出。
- 实例身份 = (trip_id, service_date, 实际首站发车秒)；HH>23 的秒数仍属原服务日，
  跨午夜不另建实例。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .gtfs import Feed


@dataclass(frozen=True)
class Call:
    stop_sequence: int
    stop_id: str
    arr_secs: int  # 自服务日午夜起的秒数，可 >= 86400
    dep_secs: int
    stop_times_row: int  # stop_times.txt 逻辑行号


@dataclass(frozen=True)
class Instance:
    trip_id: str
    service_id: str
    service_date: date
    start_dep_secs: int  # 实际首站发车秒（身份组成部分）
    source: str  # "schedule" | "frequency"
    trips_row: int  # trips.txt 逻辑行号
    frequencies_row: int | None  # frequencies.txt 逻辑行号（仅 frequency 实例）
    calls: tuple[Call, ...]  # 已按 stop_sequence 升序


def active_dates(feed: Feed, range_start: date, range_end: date) -> dict[str, set[date]]:
    """每个 service_id 在闭区间 [range_start, range_end] 内的有效服务日集合。"""
    result: dict[str, set[date]] = {}
    for sid, cal in feed.calendars.items():
        lo = max(cal.start, range_start)
        hi = min(cal.end, range_end)
        days: set[date] = set()
        d = lo
        while d <= hi:
            if cal.weekdays[d.weekday()]:
                days.add(d)
            d += timedelta(days=1)
        result[sid] = days
    # 例外优先：在星期标志展开结果之上增删
    for sid, by_date in feed.exceptions.items():
        days = result.setdefault(sid, set())
        for day, etype in by_date.items():
            if not (range_start <= day <= range_end):
                continue
            if etype == 1:
                days.add(day)
            else:
                days.discard(day)
    return result


def expand(feed: Feed, range_start: date, range_end: date) -> list[Instance]:
    """展开全部实例；确定性输出顺序（service_date, trip_id, start_dep_secs）。"""
    dates_by_service = active_dates(feed, range_start, range_end)
    instances: list[Instance] = []
    for trip_id in sorted(feed.trips):
        trip = feed.trips[trip_id]
        template = feed.stop_times[trip_id]  # load 阶段已按 stop_sequence 排序
        first_dep = template[0].dep_secs
        freqs = feed.frequencies.get(trip_id, [])
        for svc_date in sorted(dates_by_service.get(trip.service_id, ())):
            if freqs:
                for freq in freqs:
                    dep = freq.start_secs
                    while dep < freq.end_secs:  # 半开区间 [start, end)
                        calls = tuple(
                            Call(
                                st.stop_sequence,
                                st.stop_id,
                                st.arr_secs - first_dep + dep,
                                st.dep_secs - first_dep + dep,
                                st.row,
                            )
                            for st in template
                        )
                        instances.append(
                            Instance(
                                trip_id,
                                trip.service_id,
                                svc_date,
                                dep,
                                "frequency",
                                trip.row,
                                freq.row,
                                calls,
                            )
                        )
                        dep += freq.headway_secs
            else:
                calls = tuple(
                    Call(st.stop_sequence, st.stop_id, st.arr_secs, st.dep_secs, st.row)
                    for st in template
                )
                instances.append(
                    Instance(
                        trip_id,
                        trip.service_id,
                        svc_date,
                        first_dep,
                        "schedule",
                        trip.row,
                        None,
                        calls,
                    )
                )
    instances.sort(key=lambda i: (i.service_date, i.trip_id, i.start_dep_secs))
    return instances
