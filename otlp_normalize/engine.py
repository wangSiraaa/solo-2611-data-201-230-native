"""区间归一化引擎。

输入：parser 放行的 RawPoint（已通过全部逐点校验）。
输出：FactRow 事实表 + 引擎阶段产生的 UnconfirmedRecord + 更新后的 OriginRecord。

两条路径：
* DELTA：点本身就是 [start, end) 的区间增量。精确重复折叠；部分重叠按
  scope 规则全部标冲突排除，绝不把重叠部分均匀摊开。
* CUMULATIVE：按 (time, start) 排序后跑计数代（epoch）状态机：
  - 同一 startTime 内用相邻有效点做差；首点给出从声明 startTime 到 time 的首区间；
  - startTime 改变即新计数代，新起点 >= 前一已确认结束才可衔接，否则报代重叠歧义；
  - 边界模式变化、单调倒退、schema 不一致都会使当前 epoch 基线失效，
    直到出现明确的新 startTime 才能重新形成区间；缺口不补零、不重采样。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from .canonical import (
    METRIC_KIND_HISTOGRAM,
    METRIC_KIND_SUM,
    StreamIdentity,
)
from .model import (
    FactRow,
    OriginRecord,
    Q_DELTA,
    Q_FIRST_INTERVAL,
    Q_GAP,
    Q_OK,
    Q_SUM_UNKNOWN,
    RawPoint,
    R_BASELINE_UNAVAILABLE,
    R_DELTA_OVERLAP,
    R_DUPLICATE_CONFLICT,
    R_EPOCH_OVERLAP,
    R_INCOMPARABLE_BOUNDS,
    R_MIXED_TEMPORALITY,
    R_MONOTONIC_REGRESSION,
    TEMPORALITY_DELTA,
    UnconfirmedRecord,
)
from .parser import Counters

RejectFn = Callable[[list[RawPoint], str, str, StreamIdentity | None, int | None, int | None], None]


def run_engine(
    points: list[RawPoint],
    origins: list[OriginRecord],
    unconfirmed: list[UnconfirmedRecord],
    counters: Counters,
) -> list[FactRow]:
    origin_by_id = {o.origin_id: o for o in origins}

    def reject(
        pts: list[RawPoint],
        reason: str,
        detail: str,
        identity: StreamIdentity | None,
        start: int | None,
        end: int | None,
    ) -> None:
        uid = counters.next_unconfirmed()
        rec = UnconfirmedRecord(
            unconfirmed_id=uid,
            origin_ids=[p.origin_id for p in pts],
            identity=identity,
            reason=reason,
            detail=detail,
            start_time=start,
            end_time=end,
            payload={
                "points": [
                    {
                        "origin_id": p.origin_id,
                        "startTimeUnixNano": str(p.start_time),
                        "timeUnixNano": str(p.end_time),
                        "content": _content_payload(p),
                    }
                    for p in pts
                ]
            },
        )
        unconfirmed.append(rec)
        for p in pts:
            o = origin_by_id[p.origin_id]
            o.status = "unconfirmed"
            o.unconfirmed_id = uid
            o.reason = reason
            o.detail = detail

    def publish(fact: FactRow, anchors: list[RawPoint]) -> None:
        facts.append(fact)
        anchor_ids = {a.origin_id for a in anchors}
        for anchor in anchors:
            o = origin_by_id[anchor.origin_id]
            # 累计基线点会同时参与首区间和相邻差值：保留它归属的第一个事实
            if o.status == "pending":
                o.status = "fact"
                o.fact_id = fact.fact_id
        # 折叠点（可能来自其他文件的独立解析）统一挂到锚点与事实上
        for anchor in anchors:
            for folded_id in anchor.collapsed_origin_ids:
                fo = origin_by_id[folded_id]
                fo.status = "collapsed"
                fo.fact_id = fact.fact_id
                fo.collapsed_into_origin = anchor.origin_id
        # 防御：任何未被锚点覆盖但列入事实来源的点也挂接
        for oid in fact.source_origin_ids:
            if oid not in anchor_ids:
                o = origin_by_id[oid]
                if o.status == "pending":
                    o.status = "fact"
                    o.fact_id = fact.fact_id

    # 1) 完整身份分组 --------------------------------------------------------
    groups: dict[StreamIdentity, list[RawPoint]] = defaultdict(list)
    for p in points:
        groups[p.identity].append(p)

    facts: list[FactRow] = []

    for identity, group in groups.items():
        temporalities = {p.temporality for p in group}
        if len(temporalities) > 1:
            # 整流拒绝：同一完整身份出现不同 temporality，不能拆流掩盖
            reject(
                group,
                R_MIXED_TEMPORALITY,
                "same stream identity carries both delta(1) and cumulative(2) points; stream rejected as a whole",
                identity,
                None,
                None,
            )
            continue

        survivors = _fold_group(identity, group, reject)
        if not survivors:
            continue
        temporality = survivors[0].temporality
        if temporality == TEMPORALITY_DELTA:
            _run_delta(identity, survivors, counters, facts, reject, publish)
        else:
            _run_cumulative(identity, survivors, counters, facts, reject, publish)

    # 收尾：任何仍 pending 的点（例如其同区间副本已被组级拒绝，而本点又在
    # 后续引擎阶段被排除）必须有明确归宿，统一落隔离表而不是静默丢弃。
    leftovers = [
        p for p in points
        if origin_by_id[p.origin_id].status == "pending"
    ]
    if leftovers:
        by_identity: dict[StreamIdentity, list[RawPoint]] = defaultdict(list)
        for p in leftovers:
            by_identity[p.identity].append(p)
        for identity, members in by_identity.items():
            reject(
                members,
                R_BASELINE_UNAVAILABLE,
                "point excluded after its stream was invalidated; no recoverable interval",
                identity,
                min(p.start_time for p in members),
                max(p.end_time for p in members),
            )

    return facts


# ---------------------------------------------------------------------------
# 折叠与同区间冲突
# ---------------------------------------------------------------------------
def _fold_group(
    identity: StreamIdentity,
    group: list[RawPoint],
    reject: RejectFn,
) -> list[RawPoint]:
    """同流同 [start,end)：内容完全一致则折叠，否则冲突隔离。"""
    by_interval: dict[tuple[int, int], list[RawPoint]] = defaultdict(list)
    for p in group:
        by_interval[(p.start_time, p.end_time)].append(p)

    survivors: list[RawPoint] = []
    for (start, end), bucket in sorted(by_interval.items()):
        signatures = {p.content_signature() for p in bucket}
        if len(signatures) == 1:
            anchor = min(bucket, key=lambda p: p.ordinal)
            anchor.collapsed_origin_ids = sorted(
                p.origin_id for p in bucket if p is not anchor
            )
            survivors.append(anchor)
        else:
            reject(
                bucket,
                R_DUPLICATE_CONFLICT,
                f"same stream identity and same interval [{start}, {end}) but conflicting point content",
                identity,
                start,
                end,
            )
    return survivors


# ---------------------------------------------------------------------------
# DELTA 引擎
# ---------------------------------------------------------------------------
def _run_delta(
    identity: StreamIdentity,
    points: list[RawPoint],
    counters: Counters,
    facts: list[FactRow],
    reject: RejectFn,
    publish: Callable[[FactRow, list[RawPoint]], None],
) -> None:
    ordered = sorted(points, key=lambda p: (p.start_time, p.end_time, p.ordinal))

    # 部分重叠（完全相同区间已在折叠阶段处理）：对两两相交的点求连通分量，
    # 分量内全部标冲突并排除，不做任何摊分。
    n = len(ordered)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = ordered[i], ordered[j]
            if a.start_time < b.end_time and b.start_time < a.end_time:  # 半开相交
                union(i, j)

    clusters: dict[int, list[RawPoint]] = defaultdict(list)
    for i, p in enumerate(ordered):
        clusters[find(i)].append(p)

    generation = 0
    prev_end: int | None = None
    for root in sorted(clusters, key=lambda r: min(p.start_time for p in clusters[r])):
        members = sorted(clusters[root], key=lambda p: (p.start_time, p.end_time, p.ordinal))
        if len(members) > 1:
            reject(
                members,
                R_DELTA_OVERLAP,
                "delta points have partially overlapping intervals; no proportional splitting allowed",
                identity,
                min(p.start_time for p in members),
                max(p.end_time for p in members),
            )
            continue

        p = members[0]
        if prev_end is None or p.start_time != prev_end:
            generation += 1  # 不相邻（缺口）另起计数代
        quality = [Q_DELTA]
        if prev_end is not None and p.start_time > prev_end:
            quality.append(Q_GAP)
        fact = _build_fact(
            counters, identity, p,
            start_time=p.start_time, end_time=p.end_time,
            generation=generation, generation_start=p.start_time,
            quality=quality, value=_point_value(p, quality),
            source_points=[p],
        )
        publish(fact, [p])
        prev_end = p.end_time


# ---------------------------------------------------------------------------
# CUMULATIVE 计数代状态机
# ---------------------------------------------------------------------------
@dataclass
class _EpochState:
    generation: int
    start_time: int
    baseline: RawPoint | None = None
    active: bool = True


def _run_cumulative(
    identity: StreamIdentity,
    points: list[RawPoint],
    counters: Counters,
    facts: list[FactRow],
    reject: RejectFn,
    publish: Callable[[FactRow, list[RawPoint]], None],
) -> None:
    ordered = sorted(points, key=lambda p: (p.end_time, p.start_time, p.ordinal))
    state = _EpochState(generation=0, start_time=0)
    previous_end: int | None = None

    for p in ordered:
        # 新计数代只在“首个点”或“startTime 显式改变”时打开；
        # 基线失效但 startTime 不变的点不能重开同一代。
        opening_new = state.baseline is None or p.start_time != state.start_time

        if opening_new:
            if (
                state.baseline is not None
                and p.start_time != state.start_time
                and previous_end is not None
                and p.start_time < previous_end
            ):
                # 重叠代：新起点早于前一已确认结束，无法衔接；旧代保持失效，
                # 直到出现明确且不重叠的新 startTime。
                state.active = False
                reject(
                    [p],
                    R_EPOCH_OVERLAP,
                    (
                        f"new epoch start {p.start_time} precedes previous confirmed end "
                        f"{previous_end}; overlapping generations are ambiguous"
                    ),
                    identity,
                    p.start_time,
                    p.end_time,
                )
                continue

            state = _EpochState(
                generation=state.generation + 1,
                start_time=p.start_time,
                baseline=p,
                active=True,
            )
            quality = [Q_FIRST_INTERVAL]
            if previous_end is not None and p.start_time > previous_end:
                quality.append(Q_GAP)  # 缺口只标注，不补零
            fact = _build_fact(
                counters, identity, p,
                start_time=p.start_time, end_time=p.end_time,
                generation=state.generation, generation_start=state.start_time,
                quality=quality, value=_point_value(p, quality),
                source_points=[p],
            )
            publish(fact, [p])
            previous_end = p.end_time
            continue

        if not state.active:
            reject(
                [p],
                R_BASELINE_UNAVAILABLE,
                "epoch baseline already invalidated and this point does not declare a new startTime",
                identity,
                p.start_time,
                p.end_time,
            )
            continue

        baseline = state.baseline
        assert baseline is not None

        if p.kind == METRIC_KIND_SUM:
            delta = p.as_int - baseline.as_int  # type: ignore[operator]
            if delta < 0:
                state.active = False
                reject(
                    [p], R_MONOTONIC_REGRESSION,
                    f"cumulative Sum regressed {baseline.as_int} -> {p.as_int}; epoch closed",
                    identity, p.start_time, p.end_time,
                )
                continue
            value: dict[str, Any] = {"type": "sum", "asInt": str(delta)}
        else:
            if p.explicit_bounds != baseline.explicit_bounds:
                state.active = False
                reject(
                    [p], R_INCOMPARABLE_BOUNDS,
                    (
                        "explicitBounds changed within same startTime "
                        f"{[format(b, 'f') for b in (baseline.explicit_bounds or ())]} -> "
                        f"{[format(b, 'f') for b in (p.explicit_bounds or ())]}; "
                        "subtraction by bucket index is invalid, epoch closed"
                    ),
                    identity, p.start_time, p.end_time,
                )
                continue
            bucket_delta = tuple(
                cur - base
                for cur, base in zip(p.bucket_counts, baseline.bucket_counts)  # type: ignore[arg-type]
            )
            if any(d < 0 for d in bucket_delta):
                state.active = False
                reject(
                    [p], R_MONOTONIC_REGRESSION,
                    f"cumulative Histogram bucket counts regressed {baseline.bucket_counts} -> {p.bucket_counts}",
                    identity, p.start_time, p.end_time,
                )
                continue
            count_delta = p.count - baseline.count  # type: ignore[operator]
            if count_delta < 0:
                state.active = False
                reject(
                    [p], R_MONOTONIC_REGRESSION,
                    f"cumulative Histogram count regressed {baseline.count} -> {p.count}",
                    identity, p.start_time, p.end_time,
                )
                continue
            if sum(bucket_delta) != count_delta:
                state.active = False
                reject(
                    [p], R_MONOTONIC_REGRESSION,
                    f"count delta {count_delta} != sum of bucket deltas {sum(bucket_delta)}",
                    identity, p.start_time, p.end_time,
                )
                continue

            sum_unknown = not (p.hist_sum_present and baseline.hist_sum_present)
            hist_sum_delta = None if sum_unknown else p.hist_sum - baseline.hist_sum  # type: ignore[operator]
            value = {
                "type": "histogram",
                "explicitBounds": [format(b, "f") for b in (p.explicit_bounds or ())],
                "bucketCounts": [str(d) for d in bucket_delta],
                "count": str(count_delta),
                "sum": None if sum_unknown else format(hist_sum_delta, "f"),
                "sum_known": not sum_unknown,
            }

        quality = [Q_OK]
        if p.kind == METRIC_KIND_HISTOGRAM and value.get("sum_known") is False:
            quality.append(Q_SUM_UNKNOWN)

        fact = _build_fact(
            counters, identity, p,
            start_time=baseline.end_time, end_time=p.end_time,
            generation=state.generation, generation_start=state.start_time,
            quality=quality, value=value,
            source_points=[baseline, p],
        )
        publish(fact, [baseline, p])
        state.baseline = p
        previous_end = p.end_time


# ---------------------------------------------------------------------------
# 事实与载荷构造
# ---------------------------------------------------------------------------
def _point_value(p: RawPoint, quality: list[str]) -> dict[str, Any]:
    if p.kind == METRIC_KIND_SUM:
        return {"type": "sum", "asInt": str(p.as_int)}
    if not p.hist_sum_present and Q_SUM_UNKNOWN not in quality:
        quality.append(Q_SUM_UNKNOWN)
    return {
        "type": "histogram",
        "explicitBounds": [format(b, "f") for b in (p.explicit_bounds or ())],
        "bucketCounts": [str(c) for c in (p.bucket_counts or ())],
        "count": str(p.count),
        "sum": None if not p.hist_sum_present else format(p.hist_sum, "f"),
        "sum_known": p.hist_sum_present,
    }


def _build_fact(
    counters: Counters,
    identity: StreamIdentity,
    p: RawPoint,
    *,
    start_time: int,
    end_time: int,
    generation: int,
    generation_start: int,
    quality: list[str],
    value: dict[str, Any],
    source_points: list[RawPoint],
) -> FactRow:
    origin_ids: list[str] = []
    for sp in source_points:
        origin_ids.append(sp.origin_id)
        origin_ids.extend(sp.collapsed_origin_ids)
    return FactRow(
        fact_id=counters.next_fact(),
        identity=identity,
        start_time=start_time,
        end_time=end_time,
        kind=p.kind,
        temporality=p.temporality,
        generation=generation,
        generation_start_time=generation_start,
        quality=sorted(set(quality)),
        value=value,
        source_origin_ids=sorted(set(origin_ids)),
    )


def _content_payload(p: RawPoint) -> dict[str, Any]:
    if p.kind == METRIC_KIND_SUM:
        return {"asInt": str(p.as_int)}
    return {
        "explicitBounds": [format(b, "f") for b in (p.explicit_bounds or ())],
        "bucketCounts": [str(c) for c in (p.bucket_counts or ())],
        "count": str(p.count),
        "sum": None if not p.hist_sum_present else format(p.hist_sum, "f"),
    }
