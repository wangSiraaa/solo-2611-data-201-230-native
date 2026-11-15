"""核心数据结构、原因码与不经过 float 的 JSON 解码。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .canonical import (
    METRIC_KIND_SUM,
    StreamIdentity,
    parse_strict_int,
)

# ---------------------------------------------------------------------------
# 诊断原因码
# ---------------------------------------------------------------------------
R_INVALID_ATTRIBUTES = "invalid_attributes"
R_UNSUPPORTED_METRIC = "unsupported_metric_kind"
R_NON_MONOTONIC_SUM = "non_monotonic_sum"
R_MISSING_TEMPORALITY = "missing_temporality"
R_INVALID_TEMPORALITY = "invalid_temporality"
R_INVALID_TIMESTAMP = "invalid_timestamp"
R_NON_POSITIVE_INTERVAL = "non_positive_interval"
R_BAD_VALUE_SCHEMA = "bad_value_schema"
R_HIST_BOUNDS = "hist_bounds_not_strict_increasing"
R_HIST_BUCKET_LENGTH = "hist_bucket_length_mismatch"
R_HIST_BUCKET_NEGATIVE = "hist_bucket_count_negative"
R_HIST_COUNT_MISMATCH = "hist_count_not_equal_bucket_sum"
R_HIST_SUM_NOT_FINITE = "hist_sum_not_finite"
R_SUM_VALUE_NOT_NONNEG_INT = "sum_value_not_nonnegative_integer"
R_MIXED_TEMPORALITY = "mixed_temporality"
R_DUPLICATE_CONFLICT = "duplicate_identity_interval_conflict"
R_DELTA_OVERLAP = "delta_partial_overlap_conflict"
R_EPOCH_OVERLAP = "cumulative_epoch_overlap_ambiguous"
R_MONOTONIC_REGRESSION = "monotonic_regression"
R_INCOMPARABLE_BOUNDS = "incomparable_bucket_bounds"
R_BASELINE_UNAVAILABLE = "baseline_unavailable"

# 事实表质量标记（quality，可叠加）
Q_OK = "ok"
Q_GAP = "gap_before_interval"
Q_FIRST_INTERVAL = "first_interval_of_epoch"
Q_DELTA = "delta_point"
Q_SUM_UNKNOWN = "sum_unknown"

TEMPORALITY_DELTA = 1
TEMPORALITY_CUMULATIVE = 2


class ExactJSONDecoder(json.JSONDecoder):
    """JSON 数字一律解析为 Decimal，杜绝二进制 float 参与归一化。

    纳秒时间戳与 asInt/count/bucketCounts 在 OTLP JSON 里是字符串，
    会再由 parse_strict_int 转 int；explicitBounds / histogram sum 是
    JSON number，保留为 Decimal，比较和减法都是精确十进制。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["parse_float"] = Decimal
        kwargs["parse_int"] = Decimal
        super().__init__(*args, **kwargs)


def load_exact(text: str) -> Any:
    return json.loads(text, cls=ExactJSONDecoder)


# ---------------------------------------------------------------------------
# 解析阶段的原始点（仅承载已通过 schema 校验的点）
# ---------------------------------------------------------------------------
@dataclass
class RawPoint:
    origin_id: str
    source: str            # 文件名或 "<str>"
    location: str          # 人类可读位置
    ordinal: int           # 全局读取序号
    identity: StreamIdentity
    kind: str
    temporality: int
    start_time: int
    end_time: int
    # Sum
    as_int: int | None = None
    # Histogram
    explicit_bounds: tuple[Decimal, ...] | None = None
    bucket_counts: tuple[int, ...] | None = None
    count: int | None = None
    hist_sum: Decimal | None = None
    hist_sum_present: bool = False
    # 折叠进本点的其他精确重复点的 origin id
    collapsed_origin_ids: list[str] = field(default_factory=list)

    def content_signature(self) -> tuple[Any, ...]:
        """同流同时间点之间判定“内容完全相同/冲突”的指纹。"""
        if self.kind == METRIC_KIND_SUM:
            return ("sum", self.as_int)
        return (
            "hist",
            self.explicit_bounds,
            self.bucket_counts,
            self.count,
            self.hist_sum_present,
            self.hist_sum,
        )


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------
@dataclass
class FactRow:
    fact_id: str
    identity: StreamIdentity
    start_time: int
    end_time: int
    kind: str
    temporality: int
    generation: int
    generation_start_time: int
    quality: list[str]
    value: dict[str, Any]
    source_origin_ids: list[str] = field(default_factory=list)


@dataclass
class OriginRecord:
    origin_id: str
    source: str
    location: str
    ordinal: int
    identity: StreamIdentity | None
    start_time: int | None
    end_time: int | None
    status: str                       # fact / collapsed / unconfirmed
    fact_id: str | None
    collapsed_into_origin: str | None
    unconfirmed_id: str | None
    reason: str | None
    detail: str | None


@dataclass
class UnconfirmedRecord:
    unconfirmed_id: str
    origin_ids: list[str]
    identity: StreamIdentity | None
    reason: str
    detail: str
    start_time: int | None
    end_time: int | None
    payload: dict[str, Any]


def check_strict_increasing(bounds: list[Decimal]) -> str | None:
    if any(not b.is_finite() for b in bounds):
        return R_HIST_BOUNDS
    for left, right in zip(bounds, bounds[1:]):
        if not left < right:
            return R_HIST_BOUNDS
    return None


def int_from_string(raw: Any, field_name: str) -> tuple[int | None, str | None]:
    if not isinstance(raw, str):
        return None, f"{field_name} must be an integer encoded as JSON string"
    try:
        return parse_strict_int(raw, field_name), None
    except ValueError as exc:
        return None, str(exc)
