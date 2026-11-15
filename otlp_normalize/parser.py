"""OTLP/JSON 批解析与逐点 schema 校验。

只放行两种数据：
* 单调 Sum（sum.isMonotonic == true，值必须是非负整数字符串 asInt）；
* 显式桶 Histogram（explicitBounds + bucketCounts + count）。

任何不满足 scope 规则的点都进入 unconfirmed，不参与后续归一化；
每个输入点无论合法与否都会产生一条 origin 记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .canonical import (
    METRIC_KIND_HISTOGRAM,
    METRIC_KIND_SUM,
    StreamIdentity,
    canonicalize_attrs,
)
from .model import (
    OriginRecord,
    RawPoint,
    R_BAD_VALUE_SCHEMA,
    R_HIST_BOUNDS,
    R_HIST_BUCKET_LENGTH,
    R_HIST_BUCKET_NEGATIVE,
    R_HIST_COUNT_MISMATCH,
    R_HIST_SUM_NOT_FINITE,
    R_INVALID_ATTRIBUTES,
    R_INVALID_TEMPORALITY,
    R_INVALID_TIMESTAMP,
    R_MISSING_TEMPORALITY,
    R_NON_MONOTONIC_SUM,
    R_NON_POSITIVE_INTERVAL,
    R_SUM_VALUE_NOT_NONNEG_INT,
    R_UNSUPPORTED_METRIC,
    TEMPORALITY_CUMULATIVE,
    TEMPORALITY_DELTA,
    UnconfirmedRecord,
    check_strict_increasing,
    int_from_string,
)


@dataclass
class Counters:
    ordinal: int = 0
    origin_seq: int = 0
    unconfirmed_seq: int = 0
    fact_seq: int = 0

    def next_origin(self) -> tuple[str, int]:
        self.origin_seq += 1
        self.ordinal += 1
        return f"P{self.origin_seq:04d}", self.ordinal

    def next_unconfirmed(self) -> str:
        self.unconfirmed_seq += 1
        return f"U{self.unconfirmed_seq:04d}"

    def next_fact(self) -> str:
        self.fact_seq += 1
        return f"F{self.fact_seq:04d}"


@dataclass
class ParseResult:
    points: list[RawPoint] = field(default_factory=list)
    origins: list[OriginRecord] = field(default_factory=list)
    unconfirmed: list[UnconfirmedRecord] = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.points.extend(other.points)
        self.origins.extend(other.origins)
        self.unconfirmed.extend(other.unconfirmed)


def parse_batch(batch: Any, source: str, counters: Counters) -> ParseResult:
    result = ParseResult()
    if not isinstance(batch, dict) or not isinstance(batch.get("resourceMetrics"), list):
        raise ValueError("input is not an OTLP metrics batch (missing resourceMetrics array)")

    for r_idx, rm in enumerate(batch["resourceMetrics"]):
        resource = rm.get("resource", {}) if isinstance(rm, dict) else {}
        res_attrs, res_err = canonicalize_attrs(resource.get("attributes"))
        scope_metrics = rm.get("scopeMetrics", []) if isinstance(rm, dict) else []
        for s_idx, sm in enumerate(scope_metrics):
            scope = sm.get("scope", {}) if isinstance(sm, dict) else {}
            scope_attrs, scope_err = canonicalize_attrs(scope.get("attributes"))
            # scope 属性不进入流身份，但其重复/非法仍按 scope 规则使本 scope 全部点失效
            scope_hierarchy_bad = res_err or scope_err
            for m_idx, metric in enumerate(sm.get("metrics", []) if isinstance(sm, dict) else []):
                loc_prefix = (
                    f"{source}/resourceMetrics[{r_idx}]/scopeMetrics[{s_idx}]/metrics[{m_idx}]"
                )
                _parse_metric(
                    metric,
                    source,
                    loc_prefix,
                    res_attrs,
                    scope,
                    scope_hierarchy_bad,
                    res_err,
                    scope_err,
                    counters,
                    result,
                )
    return result


def _reject(
    point_json: Any,
    source: str,
    location: str,
    counters: Counters,
    result: ParseResult,
    identity: StreamIdentity | None,
    reason: str,
    detail: str,
    start: int | None,
    end: int | None,
) -> None:
    origin_id, ordinal = counters.next_origin()
    uid = counters.next_unconfirmed()
    result.unconfirmed.append(
        UnconfirmedRecord(
            unconfirmed_id=uid,
            origin_ids=[origin_id],
            identity=identity,
            reason=reason,
            detail=detail,
            start_time=start,
            end_time=end,
            payload=point_json if isinstance(point_json, dict) else {"raw": point_json},
        )
    )
    result.origins.append(
        OriginRecord(
            origin_id=origin_id,
            source=source,
            location=location,
            ordinal=ordinal,
            identity=identity,
            start_time=start,
            end_time=end,
            status="unconfirmed",
            fact_id=None,
            collapsed_into_origin=None,
            unconfirmed_id=uid,
            reason=reason,
            detail=detail,
        )
    )


def _reject_metric_level(
    source: str,
    location: str,
    counters: Counters,
    result: ParseResult,
    *,
    reason: str,
    detail: str,
    payload: Any,
) -> None:
    """metric 级隔离（该 metric 没有可逐点处理的数据点时使用）。"""
    origin_id, ordinal = counters.next_origin()
    uid = counters.next_unconfirmed()
    result.unconfirmed.append(
        UnconfirmedRecord(
            unconfirmed_id=uid,
            origin_ids=[origin_id],
            identity=None,
            reason=reason,
            detail=detail,
            start_time=None,
            end_time=None,
            payload=payload if isinstance(payload, dict) else {"raw": payload},
        )
    )
    result.origins.append(
        OriginRecord(
            origin_id=origin_id,
            source=source,
            location=location,
            ordinal=ordinal,
            identity=None,
            start_time=None,
            end_time=None,
            status="unconfirmed",
            fact_id=None,
            collapsed_into_origin=None,
            unconfirmed_id=uid,
            reason=reason,
            detail=detail,
        )
    )


def _make_identity(
    res_attrs: Any,
    scope: dict[str, Any],
    name: str,
    unit: str,
    kind: str,
    pt_attrs: Any,
) -> StreamIdentity:
    return (
        res_attrs,
        str(scope.get("name", "")),
        str(scope.get("version", "")),
        name,
        unit,
        kind,
        pt_attrs,
    )


def _parse_metric(
    metric: Any,
    source: str,
    loc_prefix: str,
    res_attrs: Any,
    scope: dict[str, Any],
    hierarchy_bad: str | None,
    res_err: str | None,
    scope_err: str | None,
    counters: Counters,
    result: ParseResult,
) -> None:
    if not isinstance(metric, dict):
        _reject(metric, source, loc_prefix, counters, result, None,
                R_UNSUPPORTED_METRIC, "metric entry is not an object", None, None)
        return

    name = str(metric.get("name", ""))
    unit = str(metric.get("unit", ""))
    is_sum, is_hist = "sum" in metric, "histogram" in metric
    if is_sum == is_hist:
        # 都有或都没有：gauge/summary/exponentialHistogram 等不在范围内。
        # 这类 metric 可能没有 dataPoints，因此在 metric 级别产生一条隔离记录。
        _reject_metric_level(
            source, loc_prefix, counters, result,
            reason=R_UNSUPPORTED_METRIC,
            detail="metric must carry exactly one of sum/histogram; gauge/summary/exponentialHistogram unsupported",
            payload=metric,
        )
        return
    if is_sum:
        body = metric["sum"] if isinstance(metric["sum"], dict) else {}
        kind = METRIC_KIND_SUM
        points = body.get("dataPoints", [])
        temporality = body.get("aggregationTemporality")
        monotonic = body.get("isMonotonic")
    else:
        body = metric["histogram"] if isinstance(metric["histogram"], dict) else {}
        kind = METRIC_KIND_HISTOGRAM
        points = body.get("dataPoints", [])
        temporality = body.get("aggregationTemporality")
        monotonic = True

    if not isinstance(points, list):
        points = []

    for p_idx, point in enumerate(points):
        location = f"{loc_prefix}/dataPoints[{p_idx}]"
        pt_attrs, pt_err = canonicalize_attrs(
            point.get("attributes") if isinstance(point, dict) else None
        )
        identity = (
            _make_identity(res_attrs, scope, name, unit, kind, pt_attrs)
            if kind in (METRIC_KIND_SUM, METRIC_KIND_HISTOGRAM)
            else None
        )
        start, end, ts_err = _parse_interval(point)

        if hierarchy_bad:
            detail = f"resource attributes: {res_err}" if res_err else f"scope attributes: {scope_err}"
            _reject(point, source, location, counters, result, identity,
                    R_INVALID_ATTRIBUTES, detail, start, end)
            continue
        if is_sum == is_hist:
            _reject(point, source, location, counters, result, identity,
                    R_UNSUPPORTED_METRIC,
                    "metric must carry exactly one of sum/histogram; gauge/summary/exponentialHistogram unsupported",
                    start, end)
            continue
        if kind == METRIC_KIND_SUM and monotonic is not True:
            _reject(point, source, location, counters, result, identity,
                    R_NON_MONOTONIC_SUM,
                    "only isMonotonic=true Sum is in scope", start, end)
            continue
        temp_err = _check_temporality(temporality)
        if temp_err:
            reason, detail = temp_err
            _reject(point, source, location, counters, result, identity, reason, detail, start, end)
            continue
        if ts_err:
            reason, detail = ts_err
            _reject(point, source, location, counters, result, identity, reason, detail, start, end)
            continue
        if pt_err:
            _reject(point, source, location, counters, result, identity,
                    R_INVALID_ATTRIBUTES, pt_err, start, end)
            continue

        if kind == METRIC_KIND_SUM:
            value_err = _validate_sum_value(point)
            if value_err:
                reason, detail = value_err
                _reject(point, source, location, counters, result, identity, reason, detail, start, end)
                continue
            raw = RawPoint(
                origin_id="",  # 由下方统一分配
                source=source,
                location=location,
                ordinal=-1,
                identity=identity,
                kind=kind,
                temporality=int(temporality),
                start_time=start,
                end_time=end,
                as_int=int(point["asInt"]),
            )
        else:
            hist_err, parsed = _validate_histogram(point)
            if hist_err:
                reason, detail = hist_err
                _reject(point, source, location, counters, result, identity, reason, detail, start, end)
                continue
            raw = RawPoint(
                origin_id="",
                source=source,
                location=location,
                ordinal=-1,
                identity=identity,
                kind=kind,
                temporality=int(temporality),
                start_time=start,
                end_time=end,
                explicit_bounds=parsed["bounds"],
                bucket_counts=parsed["buckets"],
                count=parsed["count"],
                hist_sum=parsed["sum"],
                hist_sum_present=parsed["sum_present"],
            )

        origin_id, ordinal = counters.next_origin()
        raw.origin_id = origin_id
        raw.ordinal = ordinal
        result.points.append(raw)
        result.origins.append(
            OriginRecord(
                origin_id=origin_id,
                source=source,
                location=location,
                ordinal=ordinal,
                identity=identity,
                start_time=start,
                end_time=end,
                status="pending",
                fact_id=None,
                collapsed_into_origin=None,
                unconfirmed_id=None,
                reason=None,
                detail=None,
            )
        )


def _parse_interval(point: Any) -> tuple[int | None, int | None, tuple[str, str] | None]:
    if not isinstance(point, dict):
        return None, None, (R_BAD_VALUE_SCHEMA, "data point is not an object")
    start, err_s = int_from_string(point.get("startTimeUnixNano"), "startTimeUnixNano")
    end, err_e = int_from_string(point.get("timeUnixNano"), "timeUnixNano")
    if err_s or err_e:
        return start, end, (R_INVALID_TIMESTAMP, err_s or err_e)  # type: ignore[arg-type]
    if start >= end:  # type: ignore[operator]
        return start, end, (
            R_NON_POSITIVE_INTERVAL,
            f"half-open interval requires startTime < time, got [{start}, {end})",
        )
    return start, end, None


def _check_temporality(temporality: Any) -> tuple[str, str] | None:
    if temporality is None:
        return R_MISSING_TEMPORALITY, "aggregationTemporality is required"
    if temporality not in (TEMPORALITY_DELTA, TEMPORALITY_CUMULATIVE):
        # Decimal(1)/Decimal(2) 也要接受（直接以 JSON number 给出的非标准形态）
        if isinstance(temporality, Decimal) and temporality in (Decimal(1), Decimal(2)):
            return None
        return R_INVALID_TEMPORALITY, f"aggregationTemporality must be 1 (delta) or 2 (cumulative), got {temporality}"
    return None


def _validate_sum_value(point: dict[str, Any]) -> tuple[str, str] | None:
    if "asInt" not in point:
        return R_BAD_VALUE_SCHEMA, "monotonic Sum point must use string asInt; asDouble is not allowed"
    value, err = int_from_string(point["asInt"], "asInt")
    if err:
        return R_SUM_VALUE_NOT_NONNEG_INT, err
    if value < 0:  # type: ignore[operator]
        return R_SUM_VALUE_NOT_NONNEG_INT, f"monotonic Sum asInt must be nonnegative, got {value}"
    return None


def _validate_histogram(
    point: dict[str, Any],
) -> tuple[tuple[str, str] | None, dict[str, Any]]:
    bounds_raw = point.get("explicitBounds")
    buckets_raw = point.get("bucketCounts")
    count_raw = point.get("count")

    if not isinstance(bounds_raw, list):
        return (R_HIST_BOUNDS, "explicitBounds must be present as an array"), {}
    bounds: list[Decimal] = []
    for b in bounds_raw:
        if not isinstance(b, Decimal) or isinstance(b, bool) or not b.is_finite():
            return (R_HIST_BOUNDS, f"explicitBounds entries must be finite numbers, got {b!r}"), {}
        bounds.append(b)
    if check_strict_increasing(bounds):
        return (R_HIST_BOUNDS, f"explicitBounds must be strictly increasing, got {bounds}"), {}

    if not isinstance(buckets_raw, list):
        return (R_HIST_BUCKET_LENGTH, "bucketCounts must be an array"), {}
    if len(buckets_raw) != len(bounds) + 1:
        return (
            R_HIST_BUCKET_LENGTH,
            f"len(bucketCounts)={len(buckets_raw)} must equal len(explicitBounds)+1={len(bounds) + 1}",
        ), {}
    buckets: list[int] = []
    for raw in buckets_raw:
        value, err = int_from_string(raw, "bucketCounts[]")
        if err:
            return (R_BAD_VALUE_SCHEMA, err), {}
        if value < 0:  # type: ignore[operator]
            return (R_HIST_BUCKET_NEGATIVE, f"bucket count must be nonnegative, got {value}"), {}
        buckets.append(value)  # type: ignore[arg-type]

    count, err = int_from_string(count_raw, "count")
    if err:
        return (R_BAD_VALUE_SCHEMA, err), {}
    if count < 0:  # type: ignore[operator]
        return (R_BAD_VALUE_SCHEMA, f"count must be nonnegative, got {count}"), {}
    bucket_sum = sum(buckets)  # 整数求和，不经过 float
    if count != bucket_sum:
        return (
            R_HIST_COUNT_MISMATCH,
            f"count={count} != sum(bucketCounts)={bucket_sum}",
        ), {}

    sum_present = "sum" in point
    hist_sum: Decimal | None = None
    if sum_present:
        cand = point["sum"]
        if not isinstance(cand, Decimal) or isinstance(cand, bool) or not cand.is_finite():
            return (R_HIST_SUM_NOT_FINITE, f"sum must be a finite number when present, got {cand!r}"), {}
        hist_sum = cand

    return None, {
        "bounds": tuple(bounds),
        "buckets": tuple(buckets),
        "count": count,
        "sum": hist_sum,
        "sum_present": sum_present,
    }
