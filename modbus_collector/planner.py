"""Merge overlapping point ranges and plan bounded 0x03 requests.

The planner never reads a register twice and never lets a request run across
a configured forbidden range. A multi-register point is always wholly inside
one request, so a 32-bit value cannot be split across two sampling rounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import PlanError
from .points import Point, PointTable


@dataclass(frozen=True)
class Interval:
    """Half-open register interval, [start, end)."""

    start: int
    end: int

    @property
    def count(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class DataRequest:
    sequence: int
    start: int
    count: int
    point_ids: tuple[str, ...]

    @property
    def end(self) -> int:
        return self.start + self.count

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "role": "data",
            "function": 3,
            "start": self.start,
            "count": self.count,
            "end_exclusive": self.end,
            "point_ids": list(self.point_ids),
        }


@dataclass(frozen=True)
class Plan:
    table: PointTable
    data_requests: tuple[DataRequest, ...]
    segments: tuple[Interval, ...]
    declared_point_registers: int
    unique_point_registers: int
    requested_registers: int

    @property
    def request_count(self) -> int:
        """Wire requests per round, including both version fences."""
        return len(self.data_requests) + 2

    def request_order(self) -> tuple[dict[str, Any], ...]:
        """The exact request sequence one round issues, in wire order.

        The data request ``sequence`` numbers already account for the leading
        version read (they start at 1), so they are reused unchanged.
        """
        order: list[dict[str, Any]] = [
            {
                "sequence": 0,
                "role": "fence-before",
                "function": 3,
                "start": self.table.version_register,
                "count": 1,
                "end_exclusive": self.table.version_register + 1,
                "point_ids": [],
            }
        ]
        order.extend(request.as_dict() for request in self.data_requests)
        order.append(
            {
                "sequence": len(self.data_requests) + 1,
                "role": "fence-after",
                "function": 3,
                "start": self.table.version_register,
                "count": 1,
                "end_exclusive": self.table.version_register + 1,
                "point_ids": [],
            }
        )
        return tuple(order)

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.table.unit_id,
            "version_register": self.table.version_register,
            "max_registers": self.table.max_registers,
            "forbidden_ranges": [list(r) for r in self.table.forbidden_ranges],
            "requests_per_round": [dict(item) for item in self.request_order()],
            "merge": {
                "segments": [
                    {"start": s.start, "end_exclusive": s.end, "count": s.count}
                    for s in self.segments
                ],
                "declared_point_registers": self.declared_point_registers,
                "unique_point_registers": self.unique_point_registers,
                "requested_registers": self.requested_registers,
                "duplicate_registers_read": 0,
                "alias_registers_saved": (
                    self.declared_point_registers - self.unique_point_registers
                ),
                "register_interpretations": _register_interpretations(
                    self.table, self.data_requests
                ),
            },
            "points": [
                {
                    "id": p.id,
                    "address": p.address,
                    "end_exclusive": p.end,
                    "width": p.width,
                    "type": p.type,
                    "byte_order": p.byte_order,
                    "word_order": p.word_order,
                    "scale": p.scale_text,
                }
                for p in self.table.points
            ],
            "completeness": _coverage_report(self.table, self.data_requests),
        }


def _register_interpretations(
    table: PointTable, requests: tuple[DataRequest, ...]
) -> dict[str, list[str]]:
    """Map every requested register to the point ids decoded from it."""
    index: dict[str, list[str]] = {}
    for request in requests:
        for address in range(request.start, request.end):
            index[str(address)] = [
                point.id for point in table.points if point.address <= address < point.end
            ]
    return index


def _coverage_report(
    table: PointTable, requests: tuple[DataRequest, ...]
) -> dict[str, Any]:
    uncovered: list[str] = []
    duplicated: list[str] = []
    for point in table.points:
        covering = [
            request
            for request in requests
            if request.start <= point.address and point.end <= request.end
        ]
        if not covering:
            uncovered.append(point.id)
        if len(covering) > 1:
            duplicated.append(point.id)
    return {
        "all_points_covered_each_once": not uncovered and not duplicated,
        "uncovered_points": uncovered,
        "multiply_read_points": duplicated,
        "request_ranges_disjoint": _ranges_disjoint(requests),
    }


def _ranges_disjoint(requests: tuple[DataRequest, ...]) -> bool:
    previous_end = -1
    for request in sorted(requests, key=lambda r: r.start):
        if request.start < previous_end:
            return False
        previous_end = request.end
    return True


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[Interval]:
    segments: list[Interval] = []
    for start, end in sorted(set(intervals)):
        if segments and start <= segments[-1].end:
            segments[-1] = Interval(segments[-1].start, max(segments[-1].end, end))
        else:
            segments.append(Interval(start, end))
    return segments


def _split_by_forbidden(
    segments: list[Interval], forbidden: tuple[tuple[int, int], ...]
) -> list[Interval]:
    """Cut segments at forbidden ranges (left-closed, right-open)."""
    result: list[Interval] = []
    for segment in segments:
        pieces = [segment]
        for f_start, f_end in forbidden:
            next_pieces: list[Interval] = []
            for piece in pieces:
                if piece.start < f_end and f_start < piece.end:
                    if piece.start < f_start:
                        next_pieces.append(Interval(piece.start, f_start))
                    if f_end < piece.end:
                        next_pieces.append(Interval(f_end, piece.end))
                else:
                    next_pieces.append(piece)
            pieces = next_pieces
        result.extend(piece for piece in pieces if piece.end > piece.start)
    return result


def build_plan(table: PointTable) -> Plan:
    if not table.points:
        raise PlanError("point table has no points")

    # 1. Union of each point's range. Registers shared by several points are
    #    merged into one segment, so each physical register is read once.
    point_intervals = sorted({(p.address, p.end) for p in table.points})
    segments = _merge_intervals(point_intervals)
    declared_point_registers = sum(point.width for point in table.points)
    unique_point_registers = sum(segment.count for segment in segments)

    # 2. Forbidden ranges partition each segment. Point table validation has
    #    already rejected points that straddle a forbidden range.
    pieces = _split_by_forbidden(segments, table.forbidden_ranges)

    # 3. Greedily start at the lowest unread address and include every point
    #    whose end still fits within max_registers measured from that start.
    #    The cursor then jumps to the furthest included end, so request ranges
    #    stay disjoint and never cut a point in two. A point that overlaps the
    #    block and reaches past it is not "split": the block simply stops where
    #    that point begins, and the next request starts there.
    requests: list[DataRequest] = []
    for piece in pieces:
        piece_points = sorted(
            (
                point
                for point in table.points
                if piece.start <= point.address and point.end <= piece.end
            ),
            key=lambda p: (p.address, p.end, p.id),
        )
        cursor = piece.start
        while cursor < piece.end:
            cover_end = cursor
            blocked: Point | None = None
            for point in piece_points:
                if point.end <= cursor:
                    continue  # already read in an earlier request
                if point.address < cursor:
                    # Ranges never split a point, so a point seen before the
                    # request starts must have been fully covered above.
                    raise PlanError(
                        f"point {point.id} [{point.address}, {point.end}) straddles "
                        f"request boundary {cursor}"
                    )
                if point.end - cursor > table.max_registers:
                    blocked = point
                    break
                # Growing the block to this point's end keeps one request
                # contiguous; overlapping/declared-alias points are simply
                # decoded from the same words afterwards.
                cover_end = max(cover_end, point.end)
            if cover_end <= cursor:
                assert blocked is not None
                raise PlanError(
                    f"point {blocked.id} [{blocked.address}, {blocked.end}) cannot "
                    f"fit in a {table.max_registers}-register request starting at "
                    f"{cursor} without splitting it"
                )
            point_ids = [
                point.id
                for point in piece_points
                if cursor <= point.address and point.end <= cover_end
            ]
            requests.append(
                DataRequest(
                    sequence=len(requests) + 1,  # wire order; fence-before is 0
                    start=cursor,
                    count=cover_end - cursor,
                    point_ids=tuple(point_ids),
                )
            )
            cursor = cover_end

    plan = Plan(
        table=table,
        data_requests=tuple(requests),
        segments=tuple(pieces),
        declared_point_registers=declared_point_registers,
        unique_point_registers=unique_point_registers,
        requested_registers=sum(request.count for request in requests),
    )
    _assert_plan_invariants(plan)
    return plan


def _assert_plan_invariants(plan: Plan) -> None:
    table = plan.table
    if not _ranges_disjoint(plan.data_requests):
        raise PlanError("planner produced overlapping request ranges")
    coverage = _coverage_report(table, plan.data_requests)
    if coverage["uncovered_points"]:
        raise PlanError(f"planner left points uncovered: {coverage['uncovered_points']}")
    if coverage["multiply_read_points"]:
        raise PlanError(
            f"planner read points more than once: {coverage['multiply_read_points']}"
        )
    for request in plan.data_requests:
        if not 1 <= request.count <= table.max_registers:
            raise PlanError(
                f"request at {request.start} has {request.count} registers, "
                f"outside 1..{table.max_registers}"
            )
        for f_start, f_end in table.forbidden_ranges:
            if request.start < f_end and f_start < request.end:
                raise PlanError(
                    f"request [{request.start}, {request.end}) crosses "
                    f"forbidden range [{f_start}, {f_end})"
                )
