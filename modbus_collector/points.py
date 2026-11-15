"""Load and validate the point table.

All addresses in the table are zero-based holding-register addresses.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigError

TYPE_WIDTHS: dict[str, int] = {
    "u16": 1,
    "i16": 1,
    "u32": 2,
    "i32": 2,
    "float32": 2,
}
BYTE_ORDERS = ("big", "little")
WORD_ORDERS = ("big", "little")
# Modbus addresses are 16-bit and one request may name at most 125 registers.
MAX_ADDRESS_SPACE = 0x10000


@dataclass(frozen=True)
class Point:
    id: str
    address: int
    type: str
    byte_order: str
    word_order: str
    scale: float
    scale_text: str

    @property
    def width(self) -> int:
        return TYPE_WIDTHS[self.type]

    @property
    def end(self) -> int:
        """Exclusive end of the register range occupied by this point."""
        return self.address + self.width


@dataclass(frozen=True)
class PointTable:
    unit_id: int
    version_register: int
    max_registers: int
    forbidden_ranges: tuple[tuple[int, int], ...]
    points: tuple[Point, ...]

    def points_for_range(self, start: int, count: int) -> list[Point]:
        """Points fully or partly intersecting ``[start, start+count)``."""
        end = start + count
        return [p for p in self.points if p.address < end and start < p.end]


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer, got {value!r}")
    return value


def _parse_scale(value: Any, point_id: str) -> tuple[float, str]:
    if isinstance(value, bool):
        raise ConfigError(f"point {point_id}: scale must be a decimal number")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"point {point_id}: scale must be finite")
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ConfigError(f"point {point_id}: scale must be a decimal number")
    try:
        decimal = Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"point {point_id}: invalid scale {value!r}") from exc
    if not decimal.is_finite():
        raise ConfigError(f"point {point_id}: scale must be finite")
    # The task allows only an explicitly declared decimal factor; never infer
    # exponents or units. Scientific notation is still an explicit decimal.
    try:
        scale = float(decimal)
    except (OverflowError, ValueError) as exc:
        raise ConfigError(f"point {point_id}: scale out of range") from exc
    if not math.isfinite(scale):
        raise ConfigError(f"point {point_id}: scale out of range")
    return scale, text


def _validate_ranges(
    raw_ranges: Any,
    *,
    forbidden: bool,
    known: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    if raw_ranges is None:
        return ()
    if not isinstance(raw_ranges, list):
        raise ConfigError("forbidden_ranges must be a list of [start, end) pairs")
    ranges: list[tuple[int, int]] = []
    for item in raw_ranges:
        if not isinstance(item, list) or len(item) != 2:
            raise ConfigError(f"invalid forbidden range {item!r}")
        start = _require_int(item[0], "forbidden range start")
        end = _require_int(item[1], "forbidden range end")
        if start < 0 or end > MAX_ADDRESS_SPACE or start >= end:
            raise ConfigError(
                f"forbidden range [{start}, {end}) is not a valid half-open range"
            )
        for p_start, p_end in known:
            if p_start < end and start < p_end:
                raise ConfigError(
                    f"forbidden range [{start}, {end}) overlaps a point at "
                    f"[{p_start}, {p_end})"
                )
        ranges.append((start, end))
    ranges.sort()
    for (a_start, a_end), (b_start, b_end) in zip(ranges, ranges[1:]):
        if b_start < a_end:
            raise ConfigError(
                f"forbidden ranges [{a_start}, {a_end}) and [{b_start}, {b_end}) "
                "overlap"
            )
    return tuple(ranges)


def point_table_from_dict(raw: dict[str, Any]) -> PointTable:
    if not isinstance(raw, dict):
        raise ConfigError("point table must be a JSON object")

    unit_id = _require_int(raw.get("unit_id"), "unit_id")
    if not 0 <= unit_id <= 255:
        raise ConfigError("unit_id must be in 0..255")

    version_register = _require_int(raw.get("version_register"), "version_register")
    if not 0 <= version_register < MAX_ADDRESS_SPACE:
        raise ConfigError("version_register must be a 16-bit address")

    max_registers = _require_int(raw.get("max_registers"), "max_registers")
    if not 1 <= max_registers <= 125:
        raise ConfigError("max_registers must be in 1..125")

    raw_points = raw.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ConfigError("points must be a non-empty list")

    points: list[Point] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_points):
        if not isinstance(item, dict):
            raise ConfigError(f"point #{index} must be an object")
        point_id = item.get("id")
        if not isinstance(point_id, str) or not point_id:
            raise ConfigError(f"point #{index}: id must be a non-empty string")
        if point_id in seen_ids:
            raise ConfigError(f"duplicate point id {point_id!r}")
        seen_ids.add(point_id)

        address = _require_int(item.get("address"), f"point {point_id}: address")
        if not 0 <= address < MAX_ADDRESS_SPACE:
            raise ConfigError(f"point {point_id}: address out of range")

        point_type = item.get("type")
        if point_type not in TYPE_WIDTHS:
            raise ConfigError(
                f"point {point_id}: unsupported type {point_type!r}; "
                f"expected one of {sorted(TYPE_WIDTHS)}"
            )

        byte_order = item.get("byte_order", "big")
        if byte_order not in BYTE_ORDERS:
            raise ConfigError(
                f"point {point_id}: byte_order must be one of {BYTE_ORDERS}"
            )
        word_order = item.get("word_order", "big")
        if word_order not in WORD_ORDERS:
            raise ConfigError(
                f"point {point_id}: word_order must be one of {WORD_ORDERS}"
            )
        if word_order != "big" and TYPE_WIDTHS[point_type] != 2:
            raise ConfigError(
                f"point {point_id}: word_order applies only to 32-bit types"
            )

        scale = item.get("scale", "1")
        scale_value, scale_text = _parse_scale(scale, point_id)

        point = Point(
            id=point_id,
            address=address,
            type=point_type,
            byte_order=byte_order,
            word_order=word_order,
            scale=scale_value,
            scale_text=scale_text,
        )
        if point.end > MAX_ADDRESS_SPACE:
            raise ConfigError(
                f"point {point_id}: range [{point.address}, {point.end}) exceeds "
                "the 16-bit address space"
            )
        points.append(point)

    known = [(p.address, p.end) for p in points]
    forbidden = _validate_ranges(
        raw.get("forbidden_ranges", []), forbidden=True, known=known
    )
    for p in points:
        if p.address <= version_register < p.end:
            raise ConfigError(
                f"point {p.id}: overlaps the version register {version_register}"
            )

    points.sort(key=lambda p: (p.address, p.end, p.id))
    # Point ranges may overlap (the device exposes several metrics over the
    # same registers), so each point is decoded independently from the same
    # raw words. Forbidden ranges must still not cut through any point; that
    # is checked above.
    return PointTable(
        unit_id=unit_id,
        version_register=version_register,
        max_registers=max_registers,
        forbidden_ranges=forbidden,
        points=tuple(points),
    )


def load_point_table(path: str | Path) -> PointTable:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read point table {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"point table {path} is not valid JSON: {exc}") from exc
    return point_table_from_dict(raw)
