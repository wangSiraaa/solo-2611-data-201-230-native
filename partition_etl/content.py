"""Strict source-file reading, content verification and ETL maths."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

from .errors import BuildError, Rejected

CENT = Decimal("0.01")
REQUIRED_COLUMNS = {
    "sales": ["order_id", "currency", "amount"],
    "fx": ["currency", "rate"],
}
DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
DETAIL_COLUMNS = ["order_id", "currency", "amount", "rate", "cny_amount"]


@dataclass(frozen=True)
class Record:
    values: tuple[str, ...]


def _decimal(text: str, what: str) -> Decimal:
    if not DECIMAL_TEXT.fullmatch(text):
        raise ValueError(f"{what} is not a plain decimal string: {text!r}")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"invalid {what}: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"non-finite {what}: {text!r}")
    return value


def parse_records(source: str, data: bytes) -> list[Record]:
    """Parse and validate a partition file; return records in file order.

    Every field is checked before the caller may register the partition: the
    header must match exactly, primary keys must be unique, amounts must be
    non-negative decimals and rates positive decimals.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Rejected("invalid_content", "file is not valid UTF-8", source=source) from None

    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise Rejected("invalid_content", f"CSV parse error: {exc}", source=source) from None
    if not rows:
        raise Rejected("invalid_content", "file is empty", source=source)

    header = [column.strip() for column in rows[0]]
    expected = REQUIRED_COLUMNS[source]
    if header != expected:
        raise Rejected(
            "invalid_content",
            f"{source} header must be {expected}",
            source=source,
            header=header,
        )

    body = rows[1:]
    while body and len(body[-1]) == 0:  # tolerate trailing newline(s)
        body.pop()

    records: list[Record] = []
    seen: set[str] = set()
    for line_number, row in enumerate(body, start=2):
        if len(row) != len(expected):
            raise Rejected(
                "invalid_content",
                f"row {line_number} has {len(row)} fields, expected {len(expected)}",
                source=source,
                line=line_number,
            )
        values = tuple(field.strip() for field in row)
        if any(not value for value in values):
            raise Rejected(
                "invalid_content",
                f"row {line_number} has an empty field",
                source=source,
                line=line_number,
            )

        key = values[0].upper() if source == "fx" else values[0]
        if key in seen:
            raise Rejected(
                "invalid_content",
                f"duplicate primary key {key!r}",
                source=source,
                line=line_number,
                primary_key=key,
            )
        seen.add(key)

        try:
            if source == "sales":
                if _decimal(values[2], "amount") < 0:
                    raise ValueError("amount must not be negative")
            else:
                if _decimal(values[1], "rate") <= 0:
                    raise ValueError("rate must be positive")
        except ValueError as exc:
            raise Rejected(
                "invalid_content",
                f"row {line_number}: {exc}",
                source=source,
                line=line_number,
                primary_key=key,
            ) from None

        if source == "fx":
            values = (key, values[1])
        records.append(Record(values))

    return records


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def perform_etl(sales: list[Record], fx: list[Record]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Convert sales amounts to CNY with rates from the fx partition.

    Returns (detail, unresolved). A missing rate for any sold currency makes the
    sales partition unusable; the caller turns that into a failed attempt
    without replacing the last successful output version.
    """
    rates = {record.values[0]: record.values[1] for record in fx}
    detail: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for record in sales:
        order_id, currency, amount = record.values
        rate = rates.get(currency.upper())
        if rate is None:
            unresolved.append(
                {"order_id": order_id, "currency": currency, "amount": amount, "reason": "missing_rate"}
            )
            continue
        try:
            cny = (_decimal(amount, "amount") * _decimal(rate, "rate")).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
        except ValueError as exc:
            raise BuildError("invalid_content", f"cannot convert {order_id}: {exc}") from None
        detail.append(
            {
                "order_id": order_id,
                "currency": currency,
                "amount": amount,
                "rate": rate,
                "cny_amount": format(cny, "f"),
            }
        )
    return detail, unresolved


def render_csv(header: Iterable[str], rows: Iterable[Iterable[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    return buffer.getvalue().encode("utf-8")
