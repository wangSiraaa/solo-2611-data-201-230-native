"""gtfsfact — 把小型 GTFS 班次包展开为按服务日的事实表（trip_instances / stop_calls）。

只做时刻事实展开，不做路线规划；不推断缺失站时，不预测随机班距。
"""

from .errors import BuildError, UnsupportedError
from .gtfs import Feed, load_feed
from .expand import Call, Instance, active_dates, expand
from .store import query_stop_calls, verify_db, write_db

__version__ = "1.0.0"

__all__ = [
    "BuildError",
    "UnsupportedError",
    "Feed",
    "load_feed",
    "Call",
    "Instance",
    "active_dates",
    "expand",
    "write_db",
    "query_stop_calls",
    "verify_db",
]
