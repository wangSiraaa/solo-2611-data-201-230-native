"""结果渲染：事实表 / 原点对应 / 隔离记录的 JSON 化与 inspect 文本报告。

所有整数以 JSON number 输出（Python int 任意精度，无 2^53 截断）；
十进制测量值（bounds、histogram sum）以字符串输出，保证不经过 float。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .canonical import identity_label, render_identity
from .model import FactRow, OriginRecord, UnconfirmedRecord


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, (FactRow, OriginRecord, UnconfirmedRecord)):
        data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        return to_jsonable(data)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def fact_sort_key(row: FactRow) -> tuple[Any, ...]:
    # 身份优先、再按半开区间起点/终点，使乱序批与属性换序都得到稳定排序
    return (
        row.identity,
        row.start_time,
        row.end_time,
        row.generation,
        row.fact_id,
    )


def render_facts(facts: list[FactRow]) -> list[dict[str, Any]]:
    rows = []
    for row in sorted(facts, key=fact_sort_key):
        rows.append(
            {
                "fact_id": row.fact_id,
                "identity": render_identity(row.identity),
                "interval": {
                    "start": row.start_time,
                    "end": row.end_time,
                    "half_open": "[start,end)",
                },
                "kind": row.kind,
                "aggregationTemporality": row.temporality,
                "generation": {
                    "index": row.generation,
                    "startTimeUnixNano": row.generation_start_time,
                },
                "quality": row.quality,
                "value": to_jsonable(row.value),
                "source_origin_ids": row.source_origin_ids,
            }
        )
    return rows


def render_origins(origins: list[OriginRecord]) -> list[dict[str, Any]]:
    rows = []
    for o in sorted(origins, key=lambda x: x.ordinal):
        rows.append(
            {
                "origin_id": o.origin_id,
                "source": o.source,
                "location": o.location,
                "identity": render_identity(o.identity) if o.identity else None,
                "startTimeUnixNano": o.start_time,
                "timeUnixNano": o.end_time,
                "status": o.status,
                "fact_id": o.fact_id,
                "collapsed_into_origin": o.collapsed_into_origin,
                "unconfirmed_id": o.unconfirmed_id,
                "reason": o.reason,
                "detail": o.detail,
            }
        )
    return rows


def render_unconfirmed(records: list[UnconfirmedRecord]) -> list[dict[str, Any]]:
    rows = []
    for u in sorted(records, key=lambda x: x.unconfirmed_id):
        rows.append(
            {
                "unconfirmed_id": u.unconfirmed_id,
                "origin_ids": u.origin_ids,
                "identity": render_identity(u.identity) if u.identity else None,
                "startTimeUnixNano": u.start_time,
                "timeUnixNano": u.end_time,
                "reason": u.reason,
                "detail": u.detail,
                "payload": to_jsonable(u.payload),
            }
        )
    return rows


def render_inspect(
    sources: list[str],
    facts: list[FactRow],
    origins: list[OriginRecord],
    unconfirmed: list[UnconfirmedRecord],
) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("OTLP 离线归一化 inspect 报告（仅单调 Sum 与显式桶 Histogram）")
    lines.append("=" * 78)
    lines.append(f"输入批次: {', '.join(sources)}")
    lines.append(
        f"源点总数={len(origins)}  事实区间={len(facts)}  隔离记录={len(unconfirmed)}"
    )
    status_count: dict[str, int] = {}
    for o in origins:
        status_count[o.status] = status_count.get(o.status, 0) + 1
    lines.append(
        "原点状态: " + ", ".join(f"{k}={v}" for k, v in sorted(status_count.items()))
    )
    lines.append("")

    lines.append(f"[1] 半开区间事实表（{len(facts)} 行，按身份与区间排序）")
    lines.append("-" * 78)
    for row in sorted(facts, key=fact_sort_key):
        value = _short_value(row)
        lines.append(
            f"  {row.fact_id} [{row.start_time}, {row.end_time}) gen={row.generation}"
            f" temp={row.temporality} q={','.join(row.quality)}"
        )
        lines.append(f"      {identity_label(row.identity)}")
        lines.append(f"      {value}")
        lines.append(f"      origins={row.source_origin_ids}")
    lines.append("")

    lines.append(f"[2] 不能确认的记录（{len(unconfirmed)} 条）")
    lines.append("-" * 78)
    reason_count: dict[str, int] = {}
    for u in sorted(unconfirmed, key=lambda x: x.unconfirmed_id):
        reason_count[u.reason] = reason_count.get(u.reason, 0) + 1
        label = identity_label(u.identity) if u.identity else "<identity unavailable>"
        lines.append(f"  {u.unconfirmed_id} reason={u.reason} origins={u.origin_ids}")
        lines.append(f"      {label}")
        lines.append(f"      {u.detail}")
    if reason_count:
        lines.append("  原因汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(reason_count.items())))
    lines.append("")

    lines.append("[3] 原点对应关系（每个输入点恰好一条）")
    lines.append("-" * 78)
    for o in sorted(origins, key=lambda x: x.ordinal):
        target = {
            "fact": f"-> {o.fact_id}",
            "collapsed": f"-> collapsed into {o.collapsed_into_origin} (fact {o.fact_id})",
            "unconfirmed": f"-> {o.unconfirmed_id} [{o.reason}]",
            "pending": "-> PENDING (internal error)",
        }[o.status]
        lines.append(f"  {o.origin_id} {o.source} {o.location} {target}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _short_value(row: FactRow) -> str:
    v = row.value
    if v["type"] == "sum":
        return f"sum delta asInt={v['asInt']}"
    parts = [
        f"bounds={v['explicitBounds']}",
        f"bucketDelta={v['bucketCounts']}",
        f"countDelta={v['count']}",
        f"sumDelta={'<unknown>' if not v['sum_known'] else v['sum']}",
    ]
    return "histogram " + " ".join(parts)
