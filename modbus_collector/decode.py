"""Decode point values from raw 16-bit registers.

Byte order applies inside each 16-bit register; word order applies to the two
registers of a 32-bit value. Registers are arranged by word order first, then
each register's bytes are arranged by byte order, forming the decode byte
string. No unit conversion is inferred: only the point table's explicit
decimal scale is applied.
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal
from typing import Any

from .errors import ConfigError
from .points import Point


def _scale_integer(value: int, point: Point) -> int | float:
    """Apply the declared decimal scale exactly, then pick a JSON number.

    A scale of exactly 1 leaves the integer unchanged. Otherwise the product
    uses Decimal (the scale is a decimal literal from the point table) and is
    emitted as a float; no unit or exponent is inferred.
    """
    if Decimal(point.scale_text) == 1:
        return value
    exact = Decimal(value) * Decimal(point.scale_text)
    return float(exact)


def decode_registers(point: Point, registers: list[int]) -> tuple[str, bytes, Any]:
    """Return ``(kind, bytes, value)`` for a point's raw registers.

    ``kind`` is one of:

    * ``integer`` – value is an int (scaled when the table declares a scale)
    * ``float`` – value is a finite float (scaled by the declared factor)
    * ``nan-or-inf`` – value is None; raw bytes are preserved, never turned
      into a JSON number

    Registers are never rewritten: the returned ``bytes`` are exactly the
    decode string derived from the raw u16 words.
    """
    if len(registers) != point.width:
        raise ConfigError(
            f"point {point.id}: expected {point.width} register(s), "
            f"got {len(registers)}"
        )
    words = list(registers)
    if point.width == 2 and point.word_order == "little":
        words = [words[1], words[0]]

    def register_bytes(word: int) -> bytes:
        raw = struct.pack("!H", word)
        return raw if point.byte_order == "big" else raw[::-1]

    raw = b"".join(register_bytes(word) for word in words)

    if point.type == "u16":
        value = struct.unpack(">H", raw)[0]
    elif point.type == "i16":
        value = struct.unpack(">h", raw)[0]
    elif point.type == "u32":
        value = struct.unpack(">I", raw)[0]
    elif point.type == "i32":
        value = struct.unpack(">i", raw)[0]
    elif point.type == "float32":
        value = struct.unpack(">f", raw)[0]
        if not math.isfinite(value):
            return "nan-or-inf", raw, None
        return "float", raw, value * point.scale
    else:  # pragma: no cover - rejected by point table validation
        raise ConfigError(f"point {point.id}: unsupported type {point.type}")
    return "integer", raw, _scale_integer(value, point)
