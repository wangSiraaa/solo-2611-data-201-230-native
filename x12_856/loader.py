"""Restricted X12 856 (ship notice) loader.

This module implements only the subset described in ``fixtures/scope.json``::

    ISA IEA GS GE ST SE BSN HL PRF MAN LIN SN1
    permitted extensions: TD1 REF DTM
    hierarchy: S -> O -> P -> I or S -> O -> I

It is not a general EDI mapper, format translator, or inventory system.
Quantities stay exact :class:`~decimal.Decimal` values; units are never
converted or combined.

Envelope delimiters are read from the fixed-length 106-byte ISA segment:

    element separator      byte offset 3
    repetition separator   byte offset 82  (ISA11)
    component separator    byte offset 104 (ISA16)
    segment terminator     byte offset 105

Nothing is hard-coded to ``*`` / ``:`` / ``~``.

Public entry points::

    parse(source, *, source_name="<memory>") -> ParseResult
    parse_file(path) -> ParseResult
    export(result, out_dir) -> dict[str, Path]
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "Delimiters",
    "Diagnostic",
    "HlFact",
    "Item",
    "Order",
    "Package",
    "ParseResult",
    "RawSegment",
    "ScopeStatus",
    "Shipment",
    "SourceSpan",
    "SUPPORT_BOUNDARY",
    "export",
    "parse",
    "parse_file",
]

ISA_SEGMENT_LENGTH = 106
# ISA01 .. ISA16.  Three-character tag + 16 element separators + these field
# lengths + one terminator == 106 bytes.
ISA_FIELD_LENGTHS = (2, 10, 2, 10, 2, 15, 2, 15, 6, 4, 1, 5, 9, 1, 1, 1)

KNOWN_SEGMENTS = frozenset(
    {"ISA", "IEA", "GS", "GE", "ST", "SE", "BSN", "HL", "PRF", "MAN", "LIN", "SN1"}
)
EXTRA_ALLOWED = frozenset({"TD1", "REF", "DTM"})
ALL_SEGMENTS = KNOWN_SEGMENTS | EXTRA_ALLOWED

# Only these level transitions are legal.  Prefixes of S->O->P->I and
# S->O->I are the only accepted shapes.
ALLOWED_CHILD_LEVELS: dict[str, frozenset[str]] = {
    "S": frozenset({"O"}),
    "O": frozenset({"P", "I"}),
    "P": frozenset({"I"}),
    "I": frozenset(),
}
KNOWN_LEVELS = frozenset(ALLOWED_CHILD_LEVELS)

# Known business segments are local to one HL segment group (HL up to, but not
# including, the next HL).  BSN is the only transaction-level known segment.
LOCAL_SEGMENT_LEVEL: dict[str, str] = {
    "PRF": "O",
    "MAN": "P",
    "LIN": "I",
    "SN1": "I",
}
SUPPORT_BOUNDARY = """\
Supported: the 856 subset ISA/IEA, GS/GE, ST/SE, BSN, HL, PRF, MAN, LIN,
SN1 plus TD1/REF/DTM as non-structural extensions.  Delimiters come from the
fixed 106-byte ISA.  HL01 is unique per transaction; HL02 may point forward
but only inside the same transaction.  Only S->O->P->I and S->O->I paths are
accepted.  Quantities stay exact Decimal values with their original unit and
raw text; units are never converted, normalized, or added across units.

Not supported: arbitrary EDI mapping, X12 release/escape processing,
repetition expansion, composite-element semantics beyond raw preservation,
inventory updates, acknowledgements, or segments outside the list above.
Envelope failures are scoped per fixtures/scope.json: ISA/IEA fails the whole
interchange, GS/GE fails the whole group, ST/SE or HL fails one transaction.
Unknown segments are preserved verbatim and never change HL hierarchy.
"""


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delimiters:
    element: str
    component: str
    segment: str
    repetition: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "element": self.element,
            "component": self.component,
            "segment": self.segment,
            "repetition": self.repetition,
        }


@dataclass(frozen=True)
class Diagnostic:
    scope: str
    code: str
    message: str
    segment_no: int | None = None
    end_segment_no: int | None = None
    interchange_no: int | None = None
    group_no: int | None = None
    transaction_no: int | None = None
    transaction_global_no: int | None = None
    st_control_number: str | None = None
    hl_id: str | None = None
    hl_level: str | None = None
    severity: str = "error"

    def as_row(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "scope": self.scope,
            "code": self.code,
            "message": self.message,
            "segment_no": _opt(self.segment_no),
            "end_segment_no": _opt(self.end_segment_no),
            "interchange_no": _opt(self.interchange_no),
            "group_no": _opt(self.group_no),
            "transaction_no": _opt(self.transaction_no),
            "transaction_global_no": _opt(self.transaction_global_no),
            "st_control_number": self.st_control_number or "",
            "hl_id": self.hl_id or "",
            "hl_level": self.hl_level or "",
        }


@dataclass(frozen=True)
class ScopeStatus:
    scope: str
    scope_id: str
    status: str
    own_status: str
    segment_start: int | None = None
    segment_end: int | None = None
    interchange_no: int | None = None
    group_no: int | None = None
    transaction_no: int | None = None
    transaction_global_no: int | None = None
    st_control_number: str | None = None
    reason: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "status": self.status,
            "own_status": self.own_status,
            "segment_start": _opt(self.segment_start),
            "segment_end": _opt(self.segment_end),
            "interchange_no": _opt(self.interchange_no),
            "group_no": _opt(self.group_no),
            "transaction_no": _opt(self.transaction_no),
            "transaction_global_no": _opt(self.transaction_global_no),
            "st_control_number": self.st_control_number or "",
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SourceSpan:
    interchange_no: int
    group_no: int
    transaction_no: int
    transaction_global_no: int
    st_control_number: str
    st_segment_no: int
    se_segment_no: int
    segment_start: int
    segment_end: int
    segment_ids: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "interchange_no": _opt(self.interchange_no),
            "group_no": _opt(self.group_no),
            "transaction_no": _opt(self.transaction_no),
            "transaction_global_no": _opt(self.transaction_global_no),
            "st_control_number": self.st_control_number,
            "st_segment_no": _opt(self.st_segment_no),
            "se_segment_no": _opt(self.se_segment_no),
            "source_segment_start": _opt(self.segment_start),
            "source_segment_end": _opt(self.segment_end),
            "source_segment_ids": self.segment_ids,
        }


@dataclass(frozen=True)
class Shipment:
    shipment_number: str
    bsn_date: str
    bsn_time: str
    bsn_segment_no: int
    source: SourceSpan


@dataclass(frozen=True)
class Order:
    hl_id: str
    parent_hl_id: str | None
    order_number: str
    shipment_number: str
    ancestor_ids: tuple[str, ...]
    ancestor_levels: tuple[str, ...]
    path_ids: tuple[str, ...]
    path_levels: tuple[str, ...]
    prf_segment_no: int
    source: SourceSpan


@dataclass(frozen=True)
class Package:
    hl_id: str
    parent_hl_id: str | None
    pack_code: str
    order_number: str
    shipment_number: str
    ancestor_ids: tuple[str, ...]
    ancestor_levels: tuple[str, ...]
    path_ids: tuple[str, ...]
    path_levels: tuple[str, ...]
    man_segment_no: int
    source: SourceSpan


@dataclass(frozen=True)
class Item:
    hl_id: str
    parent_hl_id: str | None
    product_qualifier: str
    product_code: str
    quantity: Decimal
    quantity_raw: str
    unit: str
    shipment_number: str
    order_number: str
    order_hl_id: str
    package_code: str | None
    package_hl_id: str | None
    ancestor_ids: tuple[str, ...]
    ancestor_levels: tuple[str, ...]
    path_ids: tuple[str, ...]
    path_levels: tuple[str, ...]
    lin_segment_no: int
    sn1_segment_no: int
    source: SourceSpan


@dataclass(frozen=True)
class HlFact:
    """One HL identity and its resolved chain, including invalid groups."""

    interchange_no: int
    group_no: int
    transaction_no: int
    transaction_global_no: int
    st_control_number: str
    hl_id: str
    level: str
    parent_hl_id: str | None
    segment_no: int
    group_start: int
    group_end: int
    ancestor_ids: tuple[str, ...]
    ancestor_levels: tuple[str, ...]
    path_ids: tuple[str, ...]
    path_levels: tuple[str, ...]
    valid: bool
    transaction_ok: bool
    issues: tuple[str, ...]

    def as_row(self) -> dict[str, str]:
        return {
            "interchange_no": _opt(self.interchange_no),
            "group_no": _opt(self.group_no),
            "transaction_no": _opt(self.transaction_no),
            "transaction_global_no": _opt(self.transaction_global_no),
            "st_control_number": self.st_control_number,
            "hl_id": self.hl_id,
            "level": self.level,
            "parent_hl_id": self.parent_hl_id or "",
            "segment_no": _opt(self.segment_no),
            "hl_group_start": _opt(self.group_start),
            "hl_group_end": _opt(self.group_end),
            "ancestor_ids": ">".join(self.ancestor_ids),
            "ancestor_levels": ">".join(self.ancestor_levels),
            "path_ids": ">".join(self.path_ids),
            "path_levels": ">".join(self.path_levels),
            "valid": "true" if self.valid else "false",
            "transaction_ok": "true" if self.transaction_ok else "false",
            "issues": "|".join(self.issues),
        }


@dataclass
class RawSegment:
    segment_no: int
    segment_id: str
    elements: tuple[str, ...]
    text: str
    raw: str = ""
    interchange_no: int | None = None
    group_no: int | None = None
    transaction_no: int | None = None
    transaction_global_no: int | None = None
    st_control_number: str | None = None
    owner_hl_id: str | None = None
    hl_occurrence: int | None = None
    hl_segment_no: int | None = None
    hl_group_start: int | None = None
    hl_group_end: int | None = None
    scope: str = "interchange"
    role: str = "unknown"
    status: str = "ok"
    detail: str = ""

    def element(self, index: int) -> str:
        return self.elements[index] if 0 <= index < len(self.elements) else ""

    def as_row(self) -> dict[str, str]:
        return {
            "segment_no": str(self.segment_no),
            "segment_id": self.segment_id,
            "element_count": str(len(self.elements)),
            "elements_json": json.dumps(list(self.elements), ensure_ascii=False),
            "raw_segment": self.text,
            "scope": self.scope,
            "role": self.role,
            "status": self.status,
            "detail": self.detail,
            "interchange_no": _opt(self.interchange_no),
            "group_no": _opt(self.group_no),
            "transaction_no": _opt(self.transaction_no),
            "transaction_global_no": _opt(self.transaction_global_no),
            "st_control_number": self.st_control_number or "",
            "owner_hl_id": self.owner_hl_id or "",
            "hl_occurrence": _opt(self.hl_occurrence),
            "hl_segment_no": _opt(self.hl_segment_no),
            "hl_group_start": _opt(self.hl_group_start),
            "hl_group_end": _opt(self.hl_group_end),
        }


@dataclass
class ParseResult:
    source_name: str
    delimiters: Delimiters | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    scope_statuses: list[ScopeStatus] = field(default_factory=list)
    shipments: list[Shipment] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    hl_facts: list[HlFact] = field(default_factory=list)
    envelope_checks: list[dict[str, Any]] = field(default_factory=list)
    segments: list[RawSegment] = field(default_factory=list)
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def status_for(self, scope: str, scope_id: str) -> ScopeStatus | None:
        for status in self.scope_statuses:
            if status.scope == scope and scope_id == status.scope_id:
                return status
        return None

    def diagnostics_for(self, code: str) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.code == code]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt(value: Any) -> str:
    return "" if value is None else str(value)


def _control_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


def _positive_decimal(text: str) -> Decimal | None:
    value = text.strip()
    if not value:
        return None
    # X12 numeric values use an optional leading sign and decimal point, not
    # exponent notation.  Rejecting exponents keeps the raw value faithful.
    if any(ch not in "0123456789+-." for ch in value):
        return None
    if value.count(".") > 1:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _ids(segments: Sequence[RawSegment], start: int, end: int) -> str:
    return ",".join(seg.segment_id for seg in segments[start:end])


def _mark(seg: RawSegment, code: str, detail: str) -> None:
    seg.status = "failed"
    entry = f"{code}:{detail}"
    if seg.detail:
        existing = seg.detail.split("|")
        if entry not in existing:
            seg.detail = f"{seg.detail}|{entry}"
    else:
        seg.detail = entry


SOURCE_FIELDS = list(
    SourceSpan(
        interchange_no=0,
        group_no=0,
        transaction_no=0,
        transaction_global_no=0,
        st_control_number="",
        st_segment_no=0,
        se_segment_no=0,
        segment_start=0,
        segment_end=0,
    ).as_row().keys()
)


# ---------------------------------------------------------------------------
# ISA / delimiter discovery
# ---------------------------------------------------------------------------


def _read_isa(
    text: str,
    *,
    segment_base: int = 0,
) -> tuple[Delimiters | None, RawSegment | None, list[Diagnostic]]:
    """Read delimiters and ISA fields at their fixed byte offsets.

    ``text`` starts at the ISA segment.  ``segment_base`` offsets diagnostic
    and ISA segment numbers when several interchanges share a stream.
    """
    isa_no = segment_base + 1
    errors: list[Diagnostic] = []
    if text[:3].upper() != "ISA":
        errors.append(
            Diagnostic(
                scope="interchange",
                code="ISA_MISSING",
                message="stream does not begin with ISA; no interchange can be established",
                segment_no=isa_no,
            )
        )
        return None, None, errors
    if len(text) < ISA_SEGMENT_LENGTH:
        errors.append(
            Diagnostic(
                scope="interchange",
                code="ISA_FORMAT",
                message=(
                    f"ISA is {len(text)} characters; the fixed ISA is "
                    f"{ISA_SEGMENT_LENGTH} bytes including its terminator"
                ),
                segment_no=isa_no,
            )
        )
        return None, None, errors

    element = text[3]
    repetition = text[82]
    component = text[104]
    terminator = text[105]

    def usable(ch: str, role: str) -> bool:
        # Control characters (e.g. the classic X12 0x1C segment terminator) are
        # legal delimiters; only blanks and alphanumerics are rejected.
        if ch in {" ", "\t", "\r", "\n"} or ch.isalnum():
            errors.append(
                Diagnostic(
                    scope="interchange",
                    code="ISA_DELIMITER",
                    message=(
                        f"{role} at its fixed ISA offset is not a usable delimiter: {ch!r}"
                    ),
                    segment_no=isa_no,
                )
            )
            return False
        return True

    if not usable(element, "element separator (offset 3)"):
        return None, None, errors
    if not usable(component, "component separator (offset 104)"):
        return None, None, errors
    if not usable(terminator, "segment terminator (offset 105)"):
        return None, None, errors
    if terminator == element or component == element:
        errors.append(
            Diagnostic(
                scope="interchange",
                code="ISA_DELIMITER",
                message="ISA element, component, and segment delimiters must be distinct",
                segment_no=isa_no,
            )
        )
        return None, None, errors

    repetition_value: str | None
    if repetition in {" ", "\t", "\r", "\n"} or repetition.isalnum() or repetition in {
        element,
        component,
        terminator,
    }:
        # Not expanded by this loader; a padding space is common in historical
        # exports.  It is recorded as absent when it is not a real delimiter.
        repetition_value = None
    else:
        repetition_value = repetition

    body = text[:105]
    fields = body.split(element)
    if len(fields) != 17 or fields[0].upper() != "ISA":
        errors.append(
            Diagnostic(
                scope="interchange",
                code="ISA_FORMAT",
                message=f"ISA contains {max(len(fields) - 1, 0)} elements, expected 16",
                segment_no=isa_no,
            )
        )
        return None, None, errors
    isa_fields = fields[1:]
    for position, (value, expected) in enumerate(
        zip(isa_fields, ISA_FIELD_LENGTHS), start=1
    ):
        if len(value) != expected:
            errors.append(
                Diagnostic(
                    scope="interchange",
                    code="ISA_FIELD_LENGTH",
                    message=(
                        f"ISA{position:02d} is {len(value)} characters, expected "
                        f"{expected}: {value!r}"
                    ),
                    segment_no=isa_no,
                )
            )

    isa = RawSegment(
        segment_no=isa_no,
        segment_id="ISA",
        elements=tuple(isa_fields),
        text=body,
        raw=text[:ISA_SEGMENT_LENGTH],
        role="interchange_open",
    )
    delimiters = Delimiters(
        element=element,
        component=component,
        segment=terminator,
        repetition=repetition_value,
    )
    return delimiters, isa, errors


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def _tokenize(
    text: str, delimiters: Delimiters, isa: RawSegment | None
) -> tuple[list[RawSegment], list[Diagnostic]]:
    """Tokenize the remainder of one interchange after its ISA.

    ``isa`` already carries the correct absolute segment number, so all
    following segments continue from it.  Different interchanges may use
    different delimiters because each ISA is tokenized independently.
    """
    diagnostics: list[Diagnostic] = []
    segments: list[RawSegment] = [isa] if isa is not None else []
    segment_no = isa.segment_no if isa is not None else 0
    chunk = ""
    body = text[ISA_SEGMENT_LENGTH:]
    for ch in body:
        if ch == delimiters.segment:
            raw_piece = chunk
            chunk = ""
            stripped = raw_piece.lstrip("\r\n")
            if not stripped:
                continue
            segment_no += 1
            parts = stripped.split(delimiters.element)
            segments.append(
                RawSegment(
                    segment_no=segment_no,
                    segment_id=parts[0].strip().upper(),
                    elements=tuple(parts[1:]),
                    text=stripped,
                    raw=raw_piece,
                )
            )
        else:
            chunk += ch
    trailing = chunk.strip("\r\n")
    if trailing:
        segment_no += 1
        parts = trailing.split(delimiters.element)
        segments.append(
            RawSegment(
                segment_no=segment_no,
                segment_id=parts[0].strip().upper(),
                elements=tuple(parts[1:]),
                text=trailing,
                raw=chunk,
            )
        )
        diagnostics.append(
            Diagnostic(
                scope="interchange",
                code="SEGMENT_UNTERMINATED",
                message=(
                    f"final segment is not terminated by the ISA-declared "
                    f"segment terminator {delimiters.segment!r}"
                ),
                segment_no=segment_no,
            )
        )
    return segments, diagnostics


# ---------------------------------------------------------------------------
# Envelope outline
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    open_idx: int
    close_idx: int | None
    body_end: int

    @property
    def end_idx(self) -> int:
        return self.close_idx + 1 if self.close_idx is not None else self.body_end

    @property
    def start_no(self) -> int:
        return self.open_idx + 1

    @property
    def end_no(self) -> int:
        return self.end_idx


def _outline(
    segments: list[RawSegment], start: int, end: int, open_id: str, close_id: str
) -> tuple[list[_Block], list[int]]:
    """Pair open/close segments inside ``[start, end)``.

    An unclosed opener extends to the next opener or to ``end``.  Segments
    that are outside every pair are returned as stray indices.
    """
    blocks: list[_Block] = []
    stray: list[int] = []
    i = start
    while i < end:
        sid = segments[i].segment_id
        if sid == open_id:
            j = i + 1
            while j < end and segments[j].segment_id not in (open_id, close_id):
                j += 1
            if j < end and segments[j].segment_id == close_id:
                blocks.append(_Block(i, j, j))
                i = j + 1
            else:
                blocks.append(_Block(i, None, j))
                i = j
        else:
            stray.append(i)
            i += 1
    return blocks, stray


def _envelope_diagnostic(
    scope: str,
    code: str,
    message: str,
    *,
    segment_no: int,
    end_segment_no: int | None = None,
    interchange_no: int | None = None,
    group_no: int | None = None,
    transaction_no: int | None = None,
    transaction_global_no: int | None = None,
    st_control_number: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        scope=scope,
        code=code,
        message=message,
        segment_no=segment_no,
        end_segment_no=end_segment_no,
        interchange_no=interchange_no,
        group_no=group_no,
        transaction_no=transaction_no,
        transaction_global_no=transaction_global_no,
        st_control_number=st_control_number,
    )


# ---------------------------------------------------------------------------
# HL identity graph
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    occurrence: int
    hl_id: str
    level: str
    parent_ref: str | None
    segment_no: int
    start: int
    end: int = 0
    issues: list[Diagnostic] = field(default_factory=list)
    chain: list[int] = field(default_factory=list)
    chain_ok: bool = False


@dataclass
class _Graph:
    # Occurrence-keyed: duplicate HL01s remain separate factual groups.
    nodes: dict[int, _Node] = field(default_factory=dict)
    by_identity: dict[str, list[int]] = field(default_factory=dict)
    roots: list[int] = field(default_factory=list)
    ok: bool = True


def _tx_context(tx: "_Tx") -> dict[str, Any]:
    return {
        "interchange_no": tx.interchange_no,
        "group_no": tx.group_no,
        "transaction_no": tx.index,
        "transaction_global_no": tx.global_index,
        "st_control_number": tx.st_control,
    }


def _tx_issue(
    tx: "_Tx",
    code: str,
    message: str,
    *,
    segment_no: int,
    end_segment_no: int | None = None,
    hl_id: str | None = None,
    hl_level: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        scope="transaction",
        code=code,
        message=message,
        segment_no=segment_no,
        end_segment_no=end_segment_no,
        hl_id=hl_id,
        hl_level=hl_level,
        **_tx_context(tx),
    )


def _build_graph(segments: list[RawSegment], tx: "_Tx") -> _Graph:
    graph = _Graph()
    body_start = tx.open_idx + 1
    body_end = tx.body_end
    hl_indices = [
        i for i in range(body_start, body_end) if segments[i].segment_id == "HL"
    ]

    for occurrence, idx in enumerate(hl_indices, start=1):
        seg = segments[idx]
        graph.nodes[occurrence] = _Node(
            occurrence=occurrence,
            hl_id=seg.element(0).strip(),
            level=seg.element(2).strip().upper(),
            parent_ref=seg.element(1).strip() or None,
            segment_no=seg.segment_no,
            start=idx,
        )
        if graph.nodes[occurrence].hl_id:
            graph.by_identity.setdefault(
                graph.nodes[occurrence].hl_id, []
            ).append(occurrence)

    # Local field ownership: the HL segment through the segment before the
    # next HL segment.  Nothing outside this window belongs to the group.
    ordered = list(graph.nodes.values())
    for position, node in enumerate(ordered):
        node.end = ordered[position + 1].start if position + 1 < len(ordered) else body_end
    first_hl = ordered[0].start if ordered else body_end
    for i in range(body_start, first_hl):
        segments[i].scope = "transaction"
        segments[i].role = "transaction_preamble"
    for node in ordered:
        for i in range(node.start, node.end):
            seg = segments[i]
            seg.scope = "transaction"
            seg.role = "hl_group"
            seg.owner_hl_id = node.hl_id or None
            seg.hl_occurrence = node.occurrence
            seg.hl_segment_no = node.segment_no
            seg.hl_group_start = segments[node.start].segment_no
            seg.hl_group_end = segments[node.end - 1].segment_no

    def fail(node: _Node, code: str, message: str) -> None:
        graph.ok = False
        diagnostic = _tx_issue(
            tx,
            code,
            message,
            segment_no=node.segment_no,
            hl_id=node.hl_id or None,
            hl_level=node.level or None,
        )
        node.issues.append(diagnostic)
        tx.issues.append(diagnostic)
        _mark(segments[node.start], code, message)

    # Identity and level checks.
    for node in ordered:
        if not node.hl_id:
            fail(
                node,
                "HL_ID_MISSING",
                f"HL at segment {node.segment_no} has an empty HL01; its local "
                "fields remain owned by this group but cannot join the identity graph",
            )
        else:
            occurrences = graph.by_identity[node.hl_id]
            if occurrences[0] != node.occurrence:
                first = graph.nodes[occurrences[0]]
                fail(
                    node,
                    "HL_ID_DUPLICATE",
                    f"HL01 {node.hl_id!r} at segment {node.segment_no} repeats "
                    f"the identity first seen at segment {first.segment_no}; "
                    "duplicate packaging codes and repeated product data never "
                    "merge these HL groups",
                )
                first.issues.append(
                    _tx_issue(
                        tx,
                        "HL_ID_DUPLICATE_OTHER",
                        f"identity {node.hl_id!r} is also used at segment "
                        f"{node.segment_no}",
                        segment_no=first.segment_no,
                        hl_id=first.hl_id,
                        hl_level=first.level or None,
                    )
                )
                graph.ok = False
        if node.level not in KNOWN_LEVELS:
            fail(
                node,
                "HL_LEVEL_INVALID",
                f"HL03 {node.level!r} at segment {node.segment_no} is not one of S/O/P/I",
            )

    # Parent resolution inside this transaction only.  The first occurrence of
    # a duplicated identity is the link target; forward references are fine.
    for node in ordered:
        if node.parent_ref is None:
            graph.roots.append(node.occurrence)
            continue
        occurrences = graph.by_identity.get(node.parent_ref)
        if not occurrences:
            fail(
                node,
                "HL_PARENT_MISSING",
                f"HL {node.hl_id or '<empty>'!r} at segment {node.segment_no} "
                f"names parent HL02 {node.parent_ref!r}, which is absent from "
                "this transaction; it is not reattached to the previous valid "
                "parent, the nearest preceding HL, or an order",
            )
            continue
        parent = graph.nodes[occurrences[0]]
        if (
            parent.level in KNOWN_LEVELS
            and node.level in KNOWN_LEVELS
            and node.level not in ALLOWED_CHILD_LEVELS[parent.level]
        ):
            fail(
                node,
                "HL_LEVEL_PATH",
                f"HL {node.hl_id!r} ({node.level}) at segment {node.segment_no} "
                f"is a child of HL {parent.hl_id!r} ({parent.level}); only "
                "S->O->P->I and S->O->I are supported",
            )

    # Exactly one HL02-empty S root per transaction.
    if not graph.roots:
        graph.ok = False
        tx.issues.append(
            _tx_issue(
                tx,
                "HL_ROOT_MISSING",
                "transaction has no HL02-empty root",
                segment_no=tx.open_idx + 1,
                end_segment_no=tx.end_no,
            )
        )
    elif len(graph.roots) > 1:
        graph.ok = False
        names = ", ".join(
            f"HL {graph.nodes[o].hl_id!r}@seg{graph.nodes[o].segment_no}"
            for o in graph.roots
        )
        diagnostic = _tx_issue(
            tx,
            "HL_MULTIPLE_ROOTS",
            f"transaction has {len(graph.roots)} HL02-empty roots: {names}",
            segment_no=graph.nodes[graph.roots[1]].segment_no,
        )
        tx.issues.append(diagnostic)
        for occurrence in graph.roots[1:]:
            graph.nodes[occurrence].issues.append(diagnostic)
    for occurrence in graph.roots:
        node = graph.nodes[occurrence]
        if node.level != "S":
            fail(
                node,
                "HL_ROOT_LEVEL",
                f"root HL {node.hl_id!r} at segment {node.segment_no} has level "
                f"{node.level!r}; a root must be level S",
            )

    # Cycles (HL02 -> first matching HL01).
    state: dict[int, int] = {}
    reported: set[frozenset[int]] = set()

    def parent_of(node: _Node) -> _Node | None:
        if node.parent_ref is None:
            return None
        occurrences = graph.by_identity.get(node.parent_ref)
        return graph.nodes[occurrences[0]] if occurrences else None

    def visit(occurrence: int, path: list[int]) -> None:
        state[occurrence] = 1
        path.append(occurrence)
        node = graph.nodes[occurrence]
        parent = parent_of(node)
        if parent is not None:
            if state.get(parent.occurrence) == 1:
                loop = path[path.index(parent.occurrence) :] + [parent.occurrence]
                if frozenset(loop) not in reported:
                    reported.add(frozenset(loop))
                    names = " -> ".join(
                        f"{graph.nodes[o].hl_id or '<empty>'}@seg"
                        f"{graph.nodes[o].segment_no}"
                        for o in loop
                    )
                    diagnostic = _tx_issue(
                        tx,
                        "HL_CYCLE",
                        f"HL parent cycle: {names}",
                        segment_no=parent.segment_no,
                        hl_id=parent.hl_id or None,
                        hl_level=parent.level or None,
                    )
                    tx.issues.append(diagnostic)
                    graph.ok = False
                    for member in loop[:-1]:
                        graph.nodes[member].issues.append(diagnostic)
                        _mark(
                            segments[graph.nodes[member].start],
                            "HL_CYCLE",
                            f"member of cycle {names}",
                        )
            elif state.get(parent.occurrence, 0) == 0:
                visit(parent.occurrence, path)
        path.pop()
        state[occurrence] = 2

    for occurrence in graph.nodes:
        if state.get(occurrence, 0) == 0:
            visit(occurrence, [])

    # Ancestor chains, root first and self last.  A chain is usable only if it
    # reaches the single valid S root through legal transitions.
    single_root = len(graph.roots) == 1
    for node in ordered:
        chain: list[int] = []
        seen: set[int] = set()
        cursor: _Node | None = node
        ok = True
        while cursor is not None:
            if cursor.occurrence in seen:
                ok = False
                break
            seen.add(cursor.occurrence)
            chain.append(cursor.occurrence)
            if cursor.parent_ref is None:
                break
            cursor = parent_of(cursor)
            if cursor is None or cursor.occurrence in seen:
                ok = False
                break
        chain.reverse()
        if ok:
            root = graph.nodes[chain[0]]
            if not single_root or root.parent_ref is not None or root.level != "S":
                ok = False
            for parent_occ, child_occ in zip(chain, chain[1:]):
                parent_level = graph.nodes[parent_occ].level
                child_level = graph.nodes[child_occ].level
                if (
                    parent_level not in KNOWN_LEVELS
                    or child_level not in KNOWN_LEVELS
                    or child_level not in ALLOWED_CHILD_LEVELS[parent_level]
                ):
                    ok = False
                    break
        node.chain = chain if ok else []
        node.chain_ok = ok
        if not ok:
            graph.ok = False
            if not node.issues:
                diagnostic = _tx_issue(
                    tx,
                    "HL_CHAIN_INCOMPLETE",
                    f"HL {node.hl_id or '<empty>'!r} at segment "
                    f"{node.segment_no} cannot be traced through valid parent "
                    "links to the S root",
                    segment_no=node.segment_no,
                    hl_id=node.hl_id or None,
                    hl_level=node.level or None,
                )
                tx.issues.append(diagnostic)
                node.issues.append(diagnostic)
                _mark(
                    segments[node.start],
                    "HL_CHAIN_INCOMPLETE",
                    "chain is incomplete",
                )

    return graph


# ---------------------------------------------------------------------------
# Transaction extraction
# ---------------------------------------------------------------------------


@dataclass
class _Tx:
    index: int
    global_index: int
    interchange_no: int
    group_no: int
    open_idx: int
    close_idx: int | None
    body_end: int
    st: RawSegment
    st_control: str
    issues: list[Diagnostic] = field(default_factory=list)
    envelope_issues: list[Diagnostic] = field(default_factory=list)
    shipment: Shipment | None = None
    orders: list[Order] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    graph: _Graph | None = None

    @property
    def end_no(self) -> int:
        return self.close_idx + 1 if self.close_idx is not None else self.body_end

    @property
    def data_ok(self) -> bool:
        return (
            not self.envelope_issues
            and not self.issues
            and self.graph is not None
            and self.graph.ok
        )


def _span(
    tx: _Tx, segments: list[RawSegment], start_idx: int, end_idx: int
) -> SourceSpan:
    return SourceSpan(
        interchange_no=tx.interchange_no,
        group_no=tx.group_no,
        transaction_no=tx.index,
        transaction_global_no=tx.global_index,
        st_control_number=tx.st_control,
        st_segment_no=tx.open_idx + 1,
        se_segment_no=tx.close_idx + 1 if tx.close_idx is not None else tx.body_end,
        segment_start=start_idx + 1,
        segment_end=end_idx,
        segment_ids=_ids(segments, start_idx, end_idx),
    )


def _find(
    segments: list[RawSegment], node: _Node, segment_id: str
) -> list[RawSegment]:
    return [
        segments[i]
        for i in range(node.start, node.end)
        if segments[i].segment_id == segment_id
    ]


def _require_single(
    tx: _Tx,
    segments: list[RawSegment],
    node: _Node,
    found: list[RawSegment],
    code: str,
    segment_id: str,
) -> RawSegment | None:
    if not found:
        detail = f"{segment_id} is missing from its own HL segment group"
        diagnostic = _tx_issue(
            tx,
            code,
            f"HL {node.hl_id or '<empty>'!r} ({node.level}) at segment "
            f"{node.segment_no}: {detail}",
            segment_no=node.segment_no,
            hl_id=node.hl_id or None,
            hl_level=node.level or None,
        )
        tx.issues.append(diagnostic)
        node.issues.append(diagnostic)
        _mark(segments[node.start], code, detail)
        return None
    if len(found) > 1:
        detail = (
            f"{len(found)} {segment_id} segments occur inside one HL segment "
            "group; field ownership is ambiguous"
        )
        diagnostic = _tx_issue(
            tx,
            f"{code.split('_MISSING')[0]}_MULTIPLE",
            f"HL {node.hl_id or '<empty>'!r} at segment {node.segment_no}: {detail}",
            segment_no=node.segment_no,
            hl_id=node.hl_id or None,
            hl_level=node.level or None,
        )
        tx.issues.append(diagnostic)
        node.issues.append(diagnostic)
        for seg in found:
            _mark(seg, f"{code.split('_MISSING')[0]}_MULTIPLE", detail)
        return None
    return found[0]


def _extract(segments: list[RawSegment], tx: _Tx, graph: _Graph) -> None:
    body_start = tx.open_idx + 1
    body_end = tx.body_end
    first_hl = min((n.start for n in graph.nodes.values()), default=body_end)

    # BSN binds the shipment and is transaction-level, not HL-local.
    bsn_segments = [
        segments[i]
        for i in range(body_start, body_end)
        if segments[i].segment_id == "BSN"
    ]
    bsn: RawSegment | None = None
    if not bsn_segments:
        tx.issues.append(
            _tx_issue(
                tx,
                "BSN_MISSING",
                "transaction has no BSN segment; shipment number cannot be bound",
                segment_no=tx.open_idx + 1,
                end_segment_no=tx.end_no,
            )
        )
    else:
        if len(bsn_segments) > 1:
            for seg in bsn_segments:
                _mark(seg, "BSN_MULTIPLE", "more than one BSN in the transaction")
            tx.issues.append(
                _tx_issue(
                    tx,
                    "BSN_MULTIPLE",
                    f"{len(bsn_segments)} BSN segments occur in one transaction; "
                    "shipment binding is ambiguous",
                    segment_no=bsn_segments[1].segment_no,
                )
            )
        bsn = bsn_segments[0]
        if bsn.segment_no - 1 >= first_hl:
            _mark(bsn, "BSN_NOT_TRANSACTION_LEVEL", "BSN appears inside an HL group")
            tx.issues.append(
                _tx_issue(
                    tx,
                    "BSN_NOT_TRANSACTION_LEVEL",
                    f"BSN at segment {bsn.segment_no} appears after the first HL; "
                    "BSN is transaction-level and cannot belong to an HL group",
                    segment_no=bsn.segment_no,
                )
            )
        if not bsn.element(1).strip():
            _mark(bsn, "BSN_SHIPMENT_MISSING", "BSN02 is empty")
            tx.issues.append(
                _tx_issue(
                    tx,
                    "BSN_SHIPMENT_MISSING",
                    f"BSN02 at segment {bsn.segment_no} is empty; no shipment "
                    "number can be bound",
                    segment_no=bsn.segment_no,
                )
            )

    # Known local segments must sit under a node of the matching level.  A
    # PRF under a P group is not an order, and a MAN under an O group is not
    # a package.
    for i in range(body_start, body_end):
        seg = segments[i]
        expected = LOCAL_SEGMENT_LEVEL.get(seg.segment_id)
        if expected is None:
            continue
        occurrence = seg.hl_occurrence
        owner_level = graph.nodes[occurrence].level if occurrence is not None else "none"
        if occurrence is None or owner_level != expected:
            detail = (
                f"{seg.segment_id} is only valid at level {expected}, "
                f"owner level is {owner_level}"
            )
            _mark(seg, "LEVEL_MISMATCH", detail)
            tx.issues.append(
                _tx_issue(
                    tx,
                    "LEVEL_MISMATCH",
                    f"{seg.segment_id} at segment {seg.segment_no} is not inside "
                    f"a level-{expected} HL group: {detail}",
                    segment_no=seg.segment_no,
                    hl_id=seg.owner_hl_id,
                    hl_level=None if occurrence is None else owner_level,
                )
            )

    # Required fields per level, looked up only inside the owning HL group.
    for node in graph.nodes.values():
        if node.level == "O":
            prf = _require_single(
                tx, segments, node, _find(segments, node, "PRF"),
                "PRF_MISSING", "PRF"
            )
            if prf is not None and not prf.element(0).strip():
                _mark(prf, "PRF_ORDER_MISSING", "PRF01 is empty")
                tx.issues.append(
                    _tx_issue(
                        tx,
                        "PRF_ORDER_MISSING",
                        f"PRF01 at segment {prf.segment_no} is empty; order number "
                        f"cannot be bound for HL {node.hl_id!r}",
                        segment_no=prf.segment_no,
                        hl_id=node.hl_id,
                        hl_level="O",
                    )
                )
        elif node.level == "P":
            man = _require_single(
                tx, segments, node, _find(segments, node, "MAN"),
                "MAN_MISSING", "MAN"
            )
            if man is not None and not man.element(1).strip():
                _mark(man, "MAN_PACKAGE_MISSING", "MAN02 is empty")
                tx.issues.append(
                    _tx_issue(
                        tx,
                        "MAN_PACKAGE_MISSING",
                        f"MAN02 at segment {man.segment_no} is empty; packaging "
                        f"code cannot be bound for HL {node.hl_id!r}",
                        segment_no=man.segment_no,
                        hl_id=node.hl_id,
                        hl_level="P",
                    )
                )
        elif node.level == "I":
            lin = _require_single(
                tx, segments, node, _find(segments, node, "LIN"),
                "LIN_MISSING", "LIN"
            )
            if lin is not None and not lin.element(2).strip():
                _mark(lin, "LIN_PRODUCT_MISSING", "LIN03 is empty")
                tx.issues.append(
                    _tx_issue(
                        tx,
                        "LIN_PRODUCT_MISSING",
                        f"LIN03 at segment {lin.segment_no} is empty; product code "
                        f"cannot be bound for HL {node.hl_id!r}",
                        segment_no=lin.segment_no,
                        hl_id=node.hl_id,
                        hl_level="I",
                    )
                )
            sn1 = _require_single(
                tx, segments, node, _find(segments, node, "SN1"),
                "SN1_MISSING", "SN1"
            )
            if sn1 is not None:
                if _positive_decimal(sn1.element(1)) is None:
                    _mark(
                        sn1,
                        "SN1_QUANTITY_INVALID",
                        f"SN102={sn1.element(1)!r}",
                    )
                    tx.issues.append(
                        _tx_issue(
                            tx,
                            "SN1_QUANTITY_INVALID",
                            f"SN102 at segment {sn1.segment_no} is "
                            f"{sn1.element(1)!r}; a positive decimal quantity is required",
                            segment_no=sn1.segment_no,
                            hl_id=node.hl_id,
                            hl_level="I",
                        )
                    )
                if not sn1.element(2).strip():
                    _mark(sn1, "SN1_UNIT_MISSING", "SN103 is empty")
                    tx.issues.append(
                        _tx_issue(
                            tx,
                            "SN1_UNIT_MISSING",
                            f"SN103 at segment {sn1.segment_no} is empty; the unit "
                            "cannot be left implicit",
                            segment_no=sn1.segment_no,
                            hl_id=node.hl_id,
                            hl_level="I",
                        )
                    )

    if not tx.data_ok:
        return
    shipment_number = bsn.element(1).strip()  # type: ignore[union-attr]
    tx.shipment = Shipment(
        shipment_number=shipment_number,
        bsn_date=bsn.element(2).strip(),  # type: ignore[union-attr]
        bsn_time=bsn.element(3).strip(),  # type: ignore[union-attr]
        bsn_segment_no=bsn.segment_no,  # type: ignore[union-attr]
        source=_span(tx, segments, tx.open_idx, tx.end_no),
    )

    def chain_nodes(node: _Node) -> list[_Node]:
        return [graph.nodes[o] for o in node.chain]

    orders: list[Order] = []
    for node in graph.nodes.values():
        if node.level != "O" or not node.chain_ok or node.issues:
            continue
        prf = _find(segments, node, "PRF")[0]
        orders.append(
            Order(
                hl_id=node.hl_id,
                parent_hl_id=node.parent_ref,
                order_number=prf.element(0).strip(),
                shipment_number=shipment_number,
                ancestor_ids=tuple(n.hl_id for n in chain_nodes(node)[:-1]),
                ancestor_levels=tuple(n.level for n in chain_nodes(node)[:-1]),
                path_ids=tuple(n.hl_id for n in chain_nodes(node)),
                path_levels=tuple(n.level for n in chain_nodes(node)),
                prf_segment_no=prf.segment_no,
                source=_span(tx, segments, node.start, node.end),
            )
        )
    orders_by_hl = {order.hl_id: order for order in orders}

    packages: list[Package] = []
    packages_by_hl: dict[str, Package] = {}
    for node in graph.nodes.values():
        if node.level != "P" or not node.chain_ok or node.issues:
            continue
        man = _find(segments, node, "MAN")[0]
        order_node = next(
            (n for n in reversed(chain_nodes(node)) if n.level == "O"), None
        )
        order = orders_by_hl.get(order_node.hl_id) if order_node else None
        if order is None:
            continue
        package = Package(
            hl_id=node.hl_id,
            parent_hl_id=node.parent_ref,
            pack_code=man.element(1).strip(),
            order_number=order.order_number,
            shipment_number=shipment_number,
            ancestor_ids=tuple(n.hl_id for n in chain_nodes(node)[:-1]),
            ancestor_levels=tuple(n.level for n in chain_nodes(node)[:-1]),
            path_ids=tuple(n.hl_id for n in chain_nodes(node)),
            path_levels=tuple(n.level for n in chain_nodes(node)),
            man_segment_no=man.segment_no,
            source=_span(tx, segments, node.start, node.end),
        )
        packages.append(package)
        packages_by_hl[node.hl_id] = package

    items: list[Item] = []
    for node in graph.nodes.values():
        if node.level != "I" or not node.chain_ok or node.issues:
            continue
        lin = _find(segments, node, "LIN")[0]
        sn1 = _find(segments, node, "SN1")[0]
        quantity = _positive_decimal(sn1.element(1))
        if quantity is None:
            continue
        chain = chain_nodes(node)
        order_node = next((n for n in reversed(chain) if n.level == "O"), None)
        package_node = next((n for n in reversed(chain) if n.level == "P"), None)
        order = orders_by_hl.get(order_node.hl_id) if order_node else None
        if order is None or order_node is None:
            continue
        package = packages_by_hl.get(package_node.hl_id) if package_node else None
        items.append(
            Item(
                hl_id=node.hl_id,
                parent_hl_id=node.parent_ref,
                product_qualifier=lin.element(1).strip(),
                product_code=lin.element(2).strip(),
                quantity=quantity,
                quantity_raw=sn1.element(1),
                unit=sn1.element(2).strip(),
                shipment_number=shipment_number,
                order_number=order.order_number,
                order_hl_id=order.hl_id,
                package_code=package.pack_code if package else None,
                package_hl_id=package.hl_id if package else None,
                ancestor_ids=tuple(n.hl_id for n in chain[:-1]),
                ancestor_levels=tuple(n.level for n in chain[:-1]),
                path_ids=tuple(n.hl_id for n in chain),
                path_levels=tuple(n.level for n in chain),
                lin_segment_no=lin.segment_no,
                sn1_segment_no=sn1.segment_no,
                source=_span(tx, segments, node.start, node.end),
            )
        )
        # A second item under the same package stays a separate fact.

    tx.orders = orders
    tx.packages = packages
    tx.items = items


# ---------------------------------------------------------------------------
# Envelope checks
# ---------------------------------------------------------------------------


def _envelope_issues(
    *,
    scope: str,
    open_seg: RawSegment,
    close_seg: RawSegment | None,
    declared: str,
    actual: int | None,
    count_code: str,
    count_label: str,
    close_id: str,
    control_pair: tuple[str, str, str, str] | None,
    control_code: str,
    context: dict[str, Any],
    end_segment_no: int | None = None,
) -> list[Diagnostic]:
    issues: list[Diagnostic] = []
    ctx = {key: context.get(key) for key in (
        "interchange_no", "group_no", "transaction_no",
        "transaction_global_no", "st_control_number",
    )}
    if close_seg is None:
        issues.append(
            _envelope_diagnostic(
                scope,
                f"{close_id}_MISSING",
                f"{open_seg.segment_id} at segment {open_seg.segment_no} is not "
                f"closed by {close_id}; its declared scope cannot be established",
                segment_no=open_seg.segment_no,
                end_segment_no=end_segment_no,
                **ctx,
            )
        )
        return issues
    declared_int = _control_int(declared)
    if declared_int is None:
        issues.append(
            _envelope_diagnostic(
                scope,
                count_code.replace("_COUNT", "_FORMAT"),
                f"{close_seg.segment_id}01 {declared!r} is not an integer",
                segment_no=close_seg.segment_no,
                **ctx,
            )
        )
    elif actual is not None and declared_int != actual:
        issues.append(
            _envelope_diagnostic(
                scope,
                count_code,
                f"{close_seg.segment_id}01 declares {declared_int} {count_label} "
                f"but the inclusive segment scan finds {actual} "
                f"({open_seg.segment_id} at {open_seg.segment_no}, "
                f"{close_seg.segment_id} at {close_seg.segment_no})",
                segment_no=close_seg.segment_no,
                end_segment_no=open_seg.segment_no,
                **ctx,
            )
        )
    if control_pair is not None:
        open_label, open_value, close_label, close_value = control_pair
        if open_value != close_value:
            issues.append(
                _envelope_diagnostic(
                    scope,
                    control_code,
                    f"{open_label} {open_value!r} does not match "
                    f"{close_label} {close_value!r}",
                    segment_no=close_seg.segment_no,
                    end_segment_no=open_seg.segment_no,
                    **ctx,
                )
            )
    return issues


def _envelope_mark_segments(
    segments: list[RawSegment],
    issues: list[Diagnostic],
    *,
    open_idx: int | None = None,
    close_idx: int | None = None,
) -> None:
    """Mark the opening or closing segment whose own contract failed.

    `segment_no` is the segment whose declared value is wrong; the
    `end_segment_no`, when present, names the other envelope boundary.  A
    mismatch marks both boundaries; a missing close marks the opener.
    """
    for issue in issues:
        targets = []
        if issue.segment_no is not None:
            targets.append(issue.segment_no - 1)
        if (
            issue.end_segment_no is not None
            and issue.end_segment_no != issue.segment_no
        ):
            targets.append(issue.end_segment_no - 1)
        if issue.code.endswith("_MISSING") and open_idx is not None:
            targets.append(open_idx)
        for target in targets:
            if 0 <= target < len(segments):
                _mark(segments[target], issue.code, issue.message)


def _verify_envelope_counts(
    segments: list[RawSegment], statuses: list[ScopeStatus]
) -> list[dict[str, Any]]:
    """Rescan emitted segments and compare declared and actual counts.

    This re-counts from the segment table rather than reusing status flags so
    callers can independently confirm each envelope by segment number.
    """
    checks: list[dict[str, Any]] = []
    for status in statuses:
        if status.segment_start is None or status.segment_end is None:
            continue
        if status.scope_id.startswith("stray:"):
            continue
        window = [
            seg
            for seg in segments
            if status.segment_start <= seg.segment_no <= status.segment_end
        ]
        open_id = {"interchange": "ISA", "group": "GS", "transaction": "ST"}[
            status.scope
        ]
        close_id = {"interchange": "IEA", "group": "GE", "transaction": "SE"}[
            status.scope
        ]
        open_seg = next((s for s in window if s.segment_id == open_id), None)
        close_seg = next((s for s in reversed(window) if s.segment_id == close_id), None)
        if status.scope == "interchange":
            actual = sum(1 for s in window if s.segment_id == "GS")
            basis = "GS groups between ISA and IEA"
            control_match = (
                open_seg is not None
                and close_seg is not None
                and open_seg.element(12) == close_seg.element(1)
            )
        elif status.scope == "group":
            actual = sum(1 for s in window if s.segment_id == "ST")
            basis = "ST transaction sets between GS and GE"
            control_match = (
                open_seg is not None
                and close_seg is not None
                and open_seg.element(5) == close_seg.element(1)
            )
        else:
            actual = (
                close_seg.segment_no - open_seg.segment_no + 1
                if open_seg is not None and close_seg is not None
                else None
            )
            basis = "all segments from ST through SE inclusive"
            control_match = (
                open_seg is not None
                and close_seg is not None
                and open_seg.element(1) == close_seg.element(1)
            )
        declared_int = (
            _control_int(close_seg.element(0)) if close_seg is not None else None
        )
        checks.append(
            {
                "scope": status.scope,
                "scope_id": status.scope_id,
                "status": status.status,
                "segment_start": status.segment_start,
                "segment_end": status.segment_end,
                "declared_count": declared_int,
                "actual_count": actual,
                "count_basis": basis,
                "count_matches": (
                    declared_int is not None
                    and actual is not None
                    and declared_int == actual
                ),
                "control_matches": control_match,
            }
        )
    return checks


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _dedupe(issues: list[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[Any, ...]] = set()
    output: list[Diagnostic] = []
    for issue in issues:
        key = (issue.scope, issue.code, issue.segment_no, issue.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
    return output


def _close_issues(
    segments: list[RawSegment],
    *,
    scope: str,
    open_seg: RawSegment,
    close_seg: RawSegment | None,
    declared: str,
    actual: int | None,
    count_code: str,
    count_label: str,
    close_id: str,
    control_pair: tuple[str, str, str, str] | None,
    control_code: str,
    context: dict[str, Any],
    end_segment_no: int | None,
) -> list[Diagnostic]:
    issues = _envelope_issues(
        scope=scope,
        open_seg=open_seg,
        close_seg=close_seg,
        declared=declared,
        actual=actual,
        count_code=count_code,
        count_label=count_label,
        close_id=close_id,
        control_pair=control_pair,
        control_code=control_code,
        context=context,
        end_segment_no=end_segment_no,
    )
    _envelope_mark_segments(segments, issues, open_idx=open_seg.segment_no - 1)
    return issues


def _parse_interchange(
    result: ParseResult,
    segments: list[RawSegment],
    i_index: int,
    i_block: _Block,
    open_issues: list[Diagnostic],
    global_start: int,
) -> tuple[int, bool]:
    isa_seg = segments[i_block.open_idx]
    iea_seg = segments[i_block.close_idx] if i_block.close_idx is not None else None
    for i in range(i_block.open_idx + 1, i_block.body_end):
        seg = segments[i]
        seg.interchange_no = i_index
        seg.scope = "interchange"
        if seg.role == "unknown":
            seg.role = "interchange_member"
    isa_seg.interchange_no = i_index
    isa_seg.scope = "interchange"
    if iea_seg is not None:
        iea_seg.interchange_no = i_index
        iea_seg.scope = "interchange"
        iea_seg.role = "interchange_close"

    # Envelope/lexical issues found before pairing groups, plus any ISA
    # delimiter/field-length problems that still left a readable ISA.
    i_issues: list[Diagnostic] = list(open_issues)
    g_blocks, g_stray = _outline(
        segments, i_block.open_idx + 1, i_block.body_end, "GS", "GE"
    )
    for idx in g_stray:
        seg = segments[idx]
        seg.scope = "interchange"
        seg.role = "stray_segment"
        _mark(seg, "STRAY_SEGMENT", "segment outside any GS/GE group")
        i_issues.append(
            _envelope_diagnostic(
                "interchange",
                "STRAY_SEGMENT",
                f"{seg.segment_id} at segment {seg.segment_no} is outside any "
                "GS/GE functional group",
                segment_no=seg.segment_no,
                interchange_no=i_index,
            )
        )
    if iea_seg is None:
        i_issues.append(
            _envelope_diagnostic(
                "interchange",
                "IEA_MISSING",
                f"interchange starting at segment {isa_seg.segment_no} has no IEA",
                segment_no=isa_seg.segment_no,
                end_segment_no=i_block.end_no,
                interchange_no=i_index,
            )
        )
        _mark(isa_seg, "IEA_MISSING", "interchange has no IEA")
    else:
        i_issues.extend(
            _close_issues(
                segments,
                scope="interchange",
                open_seg=isa_seg,
                close_seg=iea_seg,
                declared=iea_seg.element(0),
                actual=len(g_blocks),
                count_code="IEA_COUNT",
                count_label="functional groups",
                close_id="IEA",
                control_pair=(
                    "ISA13", isa_seg.element(12),
                    "IEA02", iea_seg.element(1),
                ),
                control_code="ISA_IEA_CONTROL",
                context={"interchange_no": i_index},
                end_segment_no=i_block.end_no,
            )
        )
    i_issues = _dedupe(i_issues)
    result.diagnostics.extend(i_issues)
    i_ok = not i_issues
    result.scope_statuses.append(
        ScopeStatus(
            scope="interchange",
            scope_id=str(i_index),
            status="ok" if i_ok else "failed",
            own_status="ok" if i_ok else "failed",
            segment_start=i_block.start_no,
            segment_end=i_block.end_no,
            interchange_no=i_index,
            reason="" if i_ok else i_issues[0].code,
        )
    )

    global_no = global_start
    for g_index, g_block in enumerate(g_blocks, start=1):
        gs_seg = segments[g_block.open_idx]
        ge_seg = (
            segments[g_block.close_idx] if g_block.close_idx is not None else None
        )
        for i in range(g_block.open_idx, g_block.end_idx):
            segments[i].group_no = g_index
            segments[i].scope = "group"
        gs_seg.role = "group_open"
        if ge_seg is not None:
            ge_seg.role = "group_close"

        g_issues: list[Diagnostic] = []
        t_blocks, t_stray = _outline(
            segments, g_block.open_idx + 1, g_block.body_end, "ST", "SE"
        )
        for idx in t_stray:
            seg = segments[idx]
            seg.scope = "group"
            seg.role = "stray_segment"
            _mark(seg, "STRAY_SEGMENT", "segment outside any ST/SE transaction")
            g_issues.append(
                _envelope_diagnostic(
                    "group",
                    "STRAY_SEGMENT",
                    f"{seg.segment_id} at segment {seg.segment_no} is outside "
                    "any ST/SE transaction set",
                    segment_no=seg.segment_no,
                    interchange_no=i_index,
                    group_no=g_index,
                )
            )
        if ge_seg is None:
            g_issues.append(
                _envelope_diagnostic(
                    "group",
                    "GE_MISSING",
                    f"group starting at segment {gs_seg.segment_no} has no GE",
                    segment_no=gs_seg.segment_no,
                    end_segment_no=g_block.end_no,
                    interchange_no=i_index,
                    group_no=g_index,
                )
            )
            _mark(gs_seg, "GE_MISSING", "group has no GE")
        else:
            g_issues.extend(
                _close_issues(
                    segments,
                    scope="group",
                    open_seg=gs_seg,
                    close_seg=ge_seg,
                    declared=ge_seg.element(0),
                    actual=len(t_blocks),
                    count_code="GE_COUNT",
                    count_label="transaction sets",
                    close_id="GE",
                    control_pair=(
                        "GS06", gs_seg.element(5),
                        "GE02", ge_seg.element(1),
                    ),
                    control_code="GS_GE_CONTROL",
                    context={"interchange_no": i_index, "group_no": g_index},
                    end_segment_no=g_block.end_no,
                )
            )
        g_issues = _dedupe(g_issues)
        result.diagnostics.extend(g_issues)
        g_ok = not g_issues
        result.scope_statuses.append(
            ScopeStatus(
                scope="group",
                scope_id=f"{i_index}.{g_index}",
                status="ok" if (i_ok and g_ok) else "failed",
                own_status="ok" if g_ok else "failed",
                segment_start=g_block.start_no,
                segment_end=g_block.end_no,
                interchange_no=i_index,
                group_no=g_index,
                reason=(
                    ""
                    if i_ok and g_ok
                    else ("INTERCHANGE_FAILED" if not i_ok else g_issues[0].code)
                ),
            )
        )

        for t_index, t_block in enumerate(t_blocks, start=1):
            global_no += 1
            st_seg = segments[t_block.open_idx]
            se_seg = (
                segments[t_block.close_idx]
                if t_block.close_idx is not None
                else None
            )
            for i in range(t_block.open_idx, t_block.end_idx):
                seg = segments[i]
                seg.transaction_no = t_index
                seg.transaction_global_no = global_no
                seg.scope = "transaction"
                seg.st_control_number = st_seg.element(1)
            st_seg.role = "transaction_open"
            if se_seg is not None:
                se_seg.role = "transaction_close"
            tx = _Tx(
                index=t_index,
                global_index=global_no,
                interchange_no=i_index,
                group_no=g_index,
                open_idx=t_block.open_idx,
                close_idx=t_block.close_idx,
                body_end=t_block.body_end,
                st=st_seg,
                st_control=st_seg.element(1),
            )

            # SE01 counts every segment from ST through SE inclusive.
            actual = (
                se_seg.segment_no - st_seg.segment_no + 1
                if se_seg is not None
                else None
            )
            tx.envelope_issues.extend(
                _close_issues(
                    segments,
                    scope="transaction",
                    open_seg=st_seg,
                    close_seg=se_seg,
                    declared=se_seg.element(0) if se_seg is not None else "",
                    actual=actual,
                    count_code="SE_COUNT",
                    count_label="segments from ST through SE inclusive",
                    close_id="SE",
                    control_pair=(
                        "ST02", st_seg.element(1),
                        "SE02", se_seg.element(1) if se_seg is not None else "",
                    ) if se_seg is not None else None,
                    control_code="ST_SE_CONTROL",
                    context={
                        "interchange_no": i_index,
                        "group_no": g_index,
                        "transaction_no": t_index,
                        "transaction_global_no": global_no,
                        "st_control_number": tx.st_control,
                    },
                    end_segment_no=t_block.end_no,
                )
            )
            if len(st_seg.elements) < 2:
                tx.envelope_issues.append(
                    _tx_issue(
                        tx,
                        "ST_FORMAT",
                        f"ST at segment {st_seg.segment_no} has "
                        f"{len(st_seg.elements)} elements, expected at least 2",
                        segment_no=st_seg.segment_no,
                    )
                )
                _mark(st_seg, "ST_FORMAT", "ST has too few elements")
            if st_seg.element(0).strip() != "856":
                if not any(d.code == "ST_SET_UNSUPPORTED" for d in tx.envelope_issues):
                    tx.envelope_issues.append(
                        _tx_issue(
                            tx,
                            "ST_SET_UNSUPPORTED",
                            f"ST01 {st_seg.element(0)!r} is outside the supported "
                            "856 subset",
                            segment_no=st_seg.segment_no,
                        )
                    )
                _mark(st_seg, "ST_SET_UNSUPPORTED", "not an 856 transaction")
            tx.envelope_issues = _dedupe(tx.envelope_issues)
            result.diagnostics.extend(tx.envelope_issues)

            if st_seg.element(0).strip() == "856":
                tx.graph = _build_graph(segments, tx)
                _extract(segments, tx, tx.graph)
                result.diagnostics.extend(tx.issues)

            t_ok = not tx.envelope_issues and not tx.issues and bool(
                tx.graph and tx.graph.ok
            )
            effective_ok = i_ok and g_ok and t_ok
            reason = ""
            if not i_ok:
                reason = "INTERCHANGE_FAILED"
            elif not g_ok:
                reason = "GROUP_FAILED"
            elif tx.envelope_issues:
                reason = tx.envelope_issues[0].code
            elif tx.issues:
                reason = tx.issues[0].code
            result.scope_statuses.append(
                ScopeStatus(
                    scope="transaction",
                    scope_id=f"{i_index}.{g_index}.{t_index}",
                    status="ok" if effective_ok else "failed",
                    own_status="ok" if t_ok else "failed",
                    segment_start=t_block.start_no,
                    segment_end=t_block.end_no,
                    interchange_no=i_index,
                    group_no=g_index,
                    transaction_no=t_index,
                    transaction_global_no=global_no,
                    st_control_number=tx.st_control,
                    reason=reason,
                )
            )

            if tx.graph is not None:
                invalid_occurrences = {
                    occurrence
                    for occurrence, node in tx.graph.nodes.items()
                    if node.issues or not node.chain_ok
                }
                # A failed HL group poisons its own local segment range and all
                # descendants' groups, without moving anything to another parent.
                for node in tx.graph.nodes.values():
                    if not (set(node.chain) & invalid_occurrences):
                        continue
                    if not node.chain and node.occurrence not in invalid_occurrences:
                        continue
                    for i in range(node.start, node.end):
                        seg = segments[i]
                        if seg.status != "failed":
                            _mark(
                                seg,
                                "HL_GROUP_INVALID",
                                f"owned by or descending from invalid HL "
                                f"{node.hl_id or '<empty>'} at segment {node.segment_no}",
                            )
                for node in tx.graph.nodes.values():
                    ancestors = set(node.chain)
                    inherited = bool(ancestors & invalid_occurrences)
                    result.hl_facts.append(
                        HlFact(
                            interchange_no=i_index,
                            group_no=g_index,
                            transaction_no=t_index,
                            transaction_global_no=global_no,
                            st_control_number=tx.st_control,
                            hl_id=node.hl_id,
                            level=node.level,
                            parent_hl_id=node.parent_ref,
                            segment_no=node.segment_no,
                            group_start=segments[node.start].segment_no,
                            group_end=segments[node.end - 1].segment_no,
                            ancestor_ids=tuple(
                                tx.graph.nodes[o].hl_id for o in node.chain[:-1]
                            ),
                            ancestor_levels=tuple(
                                tx.graph.nodes[o].level for o in node.chain[:-1]
                            ),
                            path_ids=tuple(
                                tx.graph.nodes[o].hl_id for o in node.chain
                            ),
                            path_levels=tuple(
                                tx.graph.nodes[o].level for o in node.chain
                            ),
                            valid=(
                                node.chain_ok
                                and not node.issues
                                and not inherited
                                and node.level in KNOWN_LEVELS
                            ),
                            transaction_ok=effective_ok,
                            issues=tuple(sorted({d.code for d in node.issues}))
                            + (
                                ("INHERITED_ANCESTOR_FAILURE",)
                                if inherited
                                else ()
                            ),
                        )
                    )

            if effective_ok:
                assert tx.shipment is not None
                result.shipments.append(tx.shipment)
                result.orders.extend(tx.orders)
                result.packages.extend(tx.packages)
                result.items.extend(tx.items)

    return global_no, i_ok


def _split_stream(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Split raw text into interchange chunks plus leading/trailing strays.

    Each chunk starts at its own ISA and ends immediately after its IEA.  The
    ISA is fixed length, so its element separator and declared segment
    terminator identify the chunk boundary without any global delimiter
    assumption.  Segment numbers in the returned chunks are 1-based within the
    whole stream.
    """
    chunks: list[tuple[int, str]] = []
    strays: list[tuple[int, str]] = []
    position = 0
    base = 0  # number of complete segments before ``position``
    length = len(text)
    while position < length:
        while position < length and text[position] in "\r\n":
            position += 1
        if position >= length:
            break
        if text[position : position + 3].upper() != "ISA":
            next_isa = text.find("ISA", position + 1)
            if next_isa < 0:
                next_isa = length
            strays.append((base + 1, text[position:next_isa]))
            position = next_isa
            continue
        if position + ISA_SEGMENT_LENGTH > length:
            strays.append((base + 1, text[position:]))
            break
        element = text[position + 3]
        terminator = text[position + 105]
        if (
            terminator in {"\r", "\n", " ", "\t"}
            or terminator.isalnum()
            or element in {"\r", "\n", " ", "\t"}
            or element.isalnum()
        ):
            strays.append((base + 1, text[position:]))
            break

        # The first segment is ISA; look for the segment immediately after
        # each terminator until its tag is IEA.
        count = 1
        cursor = position + ISA_SEGMENT_LENGTH
        end = length
        while True:
            candidate = text.find(terminator, cursor)
            if candidate < 0:
                break
            segment_text = text[cursor:candidate].lstrip("\r\n")
            tag = segment_text.split(element, 1)[0].strip().upper()
            count += 1
            cursor = candidate + 1
            if tag == "IEA":
                end = cursor
                break
        chunk = text[position:end]
        chunks.append((base + 1, chunk))
        base += count
        position = end
    return chunks, strays


def parse(source: str | bytes, *, source_name: str = "<memory>") -> ParseResult:
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig").lstrip("\ufeff")
    else:
        text = source.lstrip("\ufeff")
    result = ParseResult(source_name=source_name, raw_text=text)

    chunks, strays = _split_stream(text)
    if not chunks:
        delimiters, isa, isa_issues = _read_isa(text)
        if delimiters is not None:
            result.delimiters = delimiters
        result.diagnostics.extend(isa_issues)
        result.diagnostics.extend(
            _envelope_diagnostic(
                "interchange",
                "STRAY_SEGMENT",
                f"{segment.strip()[:20]!r} at source position {segment_no} is "
                "not inside an ISA/IEA interchange"
                if segment.strip()
                else "stream contains no ISA",
                segment_no=segment_no,
            )
            for segment_no, segment in strays
        )
        result.scope_statuses.append(
            ScopeStatus(
                scope="interchange",
                scope_id="0",
                status="failed",
                own_status="failed",
                segment_start=None,
                segment_end=None,
                reason=isa_issues[0].code if isa_issues else "ISA_MISSING",
            )
        )
        return result

    segments: list[RawSegment] = []
    global_no = 1
    for i_index, (base_no, chunk) in enumerate(chunks, start=1):
        delimiters, isa, isa_issues = _read_isa(chunk, segment_base=len(segments))
        if delimiters is None:
            # The ISA failed before any other segment of this chunk could be
            # read.  Record the chunk as one failed interchange.
            result.diagnostics.extend(isa_issues)
            if isa is not None:
                isa.interchange_no = i_index
                segments.append(isa)
            result.scope_statuses.append(
                ScopeStatus(
                    scope="interchange",
                    scope_id=str(i_index),
                    status="failed",
                    own_status="failed",
                    segment_start=(segments[-1].segment_no if segments else None),
                    segment_end=(segments[-1].segment_no if segments else None),
                    interchange_no=i_index,
                    reason=isa_issues[0].code if isa_issues else "ISA_FORMAT",
                )
            )
            continue
        if i_index == 1:
            result.delimiters = delimiters
        chunk_segments, token_issues = _tokenize(chunk, delimiters, isa)
        for seg in chunk_segments:
            seg.interchange_no = i_index
        if isa is not None and isa_issues:
            _mark(isa, isa_issues[0].code, isa_issues[0].message)
        segments.extend(chunk_segments)
        chunk_start = isa.segment_no - 1
        chunk_end = len(segments)
        close_idx = None
        if chunk_end > chunk_start + 1:
            last = segments[chunk_end - 1]
            if last.segment_id == "IEA":
                close_idx = chunk_end - 1
        global_no, _ = _parse_interchange(
            result,
            segments,
            i_index,
            _Block(
                open_idx=chunk_start,
                close_idx=close_idx,
                body_end=close_idx if close_idx is not None else chunk_end,
            ),
            _dedupe(list(isa_issues) + list(token_issues)),
            global_no,
        )

    result.segments = segments
    for segment_no, chunk in strays:
        result.diagnostics.append(
            _envelope_diagnostic(
                "interchange",
                "STRAY_SEGMENT",
                f"{chunk.strip()[:20]!r} at source position {segment_no} is "
                "outside any ISA/IEA interchange",
                segment_no=segment_no,
            )
        )
        result.scope_statuses.append(
            ScopeStatus(
                scope="interchange",
                scope_id=f"stray:{segment_no}",
                status="failed",
                own_status="failed",
                segment_start=segment_no,
                segment_end=segment_no,
                reason="STRAY_SEGMENT",
            )
        )

    # Unknown segments are preserved verbatim and never alter hierarchy.
    for seg in segments:
        if seg.segment_id in ALL_SEGMENTS:
            continue
        if seg.status != "failed":
            seg.status = "warn"
        if seg.role in ("", "unknown"):
            seg.role = "unknown_extension"
        note = "SEGMENT_UNKNOWN: preserved verbatim; hierarchy unchanged"
        seg.detail = f"{seg.detail}|{note}" if seg.detail else note
        result.diagnostics.append(
            Diagnostic(
                scope=seg.scope,
                code="SEGMENT_UNKNOWN",
                message=(
                    f"segment {seg.segment_id!r} at source segment {seg.segment_no} "
                    "is outside the supported 856 subset; preserved verbatim without "
                    "changing hierarchy"
                ),
                segment_no=seg.segment_no,
                interchange_no=seg.interchange_no,
                group_no=seg.group_no,
                transaction_no=seg.transaction_no,
                transaction_global_no=seg.transaction_global_no,
                st_control_number=seg.st_control_number,
                severity="warning",
            )
        )

    result.envelope_checks = _verify_envelope_counts(
        result.segments, result.scope_statuses
    )
    result.diagnostics.sort(
        key=lambda d: (
            d.segment_no if d.segment_no is not None else 10**9,
            d.code,
        )
    )
    return result


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _write_csv(
    path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def export(result: ParseResult, out_dir: str | Path) -> dict[str, Path]:
    """Write the relationship tables and per-segment provenance."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    shipment_rows: list[dict[str, Any]] = []
    for shipment in result.shipments:
        row = {
            "shipment_number": shipment.shipment_number,
            "bsn_date": shipment.bsn_date,
            "bsn_time": shipment.bsn_time,
            "bsn_segment_no": shipment.bsn_segment_no,
        }
        row.update(shipment.source.as_row())
        shipment_rows.append(row)
    fields = ["shipment_number", "bsn_date", "bsn_time", "bsn_segment_no", *SOURCE_FIELDS]
    paths["shipments"] = directory / "shipments.csv"
    _write_csv(paths["shipments"], shipment_rows, fields)

    order_rows: list[dict[str, Any]] = []
    for order in result.orders:
        row = {
            "order_number": order.order_number,
            "shipment_number": order.shipment_number,
            "hl_id": order.hl_id,
            "parent_hl_id": order.parent_hl_id or "",
            "ancestor_ids": ">".join(order.ancestor_ids),
            "ancestor_levels": ">".join(order.ancestor_levels),
            "path_ids": ">".join(order.path_ids),
            "path_levels": ">".join(order.path_levels),
            "prf_segment_no": order.prf_segment_no,
        }
        row.update(order.source.as_row())
        order_rows.append(row)
    fields = [
        "order_number", "shipment_number", "hl_id", "parent_hl_id",
        "ancestor_ids", "ancestor_levels", "path_ids", "path_levels",
        "prf_segment_no", *SOURCE_FIELDS,
    ]
    paths["orders"] = directory / "orders.csv"
    _write_csv(paths["orders"], order_rows, fields)

    package_rows: list[dict[str, Any]] = []
    for package in result.packages:
        row = {
            "pack_code": package.pack_code,
            "order_number": package.order_number,
            "shipment_number": package.shipment_number,
            "hl_id": package.hl_id,
            "parent_hl_id": package.parent_hl_id or "",
            "ancestor_ids": ">".join(package.ancestor_ids),
            "ancestor_levels": ">".join(package.ancestor_levels),
            "path_ids": ">".join(package.path_ids),
            "path_levels": ">".join(package.path_levels),
            "man_segment_no": package.man_segment_no,
        }
        row.update(package.source.as_row())
        package_rows.append(row)
    fields = [
        "pack_code", "order_number", "shipment_number", "hl_id",
        "parent_hl_id", "ancestor_ids", "ancestor_levels", "path_ids",
        "path_levels", "man_segment_no", *SOURCE_FIELDS,
    ]
    paths["packages"] = directory / "packages.csv"
    _write_csv(paths["packages"], package_rows, fields)

    item_rows: list[dict[str, Any]] = []
    for item in result.items:
        row = {
            "product_code": item.product_code,
            "product_qualifier": item.product_qualifier,
            "quantity_raw": item.quantity_raw,
            "quantity_decimal": str(item.quantity),
            "unit": item.unit,
            "shipment_number": item.shipment_number,
            "order_number": item.order_number,
            "order_hl_id": item.order_hl_id,
            "package_code": item.package_code or "",
            "package_hl_id": item.package_hl_id or "",
            "hl_id": item.hl_id,
            "parent_hl_id": item.parent_hl_id or "",
            "ancestor_ids": ">".join(item.ancestor_ids),
            "ancestor_levels": ">".join(item.ancestor_levels),
            "path_ids": ">".join(item.path_ids),
            "path_levels": ">".join(item.path_levels),
            "lin_segment_no": item.lin_segment_no,
            "sn1_segment_no": item.sn1_segment_no,
        }
        row.update(item.source.as_row())
        item_rows.append(row)
    fields = [
        "product_code", "product_qualifier", "quantity_raw",
        "quantity_decimal", "unit", "shipment_number", "order_number",
        "order_hl_id", "package_code", "package_hl_id", "hl_id",
        "parent_hl_id", "ancestor_ids", "ancestor_levels", "path_ids",
        "path_levels", "lin_segment_no", "sn1_segment_no", *SOURCE_FIELDS,
    ]
    paths["items"] = directory / "items.csv"
    _write_csv(paths["items"], item_rows, fields)

    hl_fields = [
        "interchange_no", "group_no", "transaction_no", "transaction_global_no",
        "st_control_number", "hl_id", "level", "parent_hl_id", "segment_no",
        "hl_group_start", "hl_group_end", "ancestor_ids", "ancestor_levels",
        "path_ids", "path_levels", "valid", "transaction_ok", "issues",
    ]
    paths["hl_groups"] = directory / "hl_groups.csv"
    _write_csv(
        paths["hl_groups"],
        (fact.as_row() for fact in result.hl_facts),
        hl_fields,
    )

    segment_fields = list(
        RawSegment(segment_no=0, segment_id="", elements=(), text="").as_row().keys()
    )
    paths["segments"] = directory / "segments.csv"
    _write_csv(
        paths["segments"],
        (segment.as_row() for segment in result.segments),
        segment_fields,
    )

    scope_fields = list(
        ScopeStatus(scope="", scope_id="", status="", own_status="").as_row().keys()
    )
    paths["scopes"] = directory / "scopes.csv"
    _write_csv(
        paths["scopes"],
        (status.as_row() for status in result.scope_statuses),
        scope_fields,
    )

    diagnostic_fields = list(
        Diagnostic(scope="", code="", message="").as_row().keys()
    )
    paths["diagnostics"] = directory / "diagnostics.csv"
    _write_csv(
        paths["diagnostics"],
        (diagnostic.as_row() for diagnostic in result.diagnostics),
        diagnostic_fields,
    )

    check_fields = [
        "scope", "scope_id", "status", "segment_start", "segment_end",
        "declared_count", "actual_count", "count_basis", "count_matches",
        "control_matches",
    ]
    paths["envelope_checks"] = directory / "envelope_checks.csv"
    _write_csv(
        paths["envelope_checks"], result.envelope_checks, check_fields
    )

    paths["delimiters"] = directory / "delimiters.json"
    paths["delimiters"].write_text(
        json.dumps(
            {
                "delimiters": (
                    result.delimiters.as_dict() if result.delimiters else None
                ),
                "support_boundary": SUPPORT_BOUNDARY,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    paths["summary"] = directory / "summary.json"
    paths["summary"].write_text(
        json.dumps(
            {
                "source": result.source_name,
                "delimiters": (
                    result.delimiters.as_dict() if result.delimiters else None
                ),
                "ok": result.ok,
                "counts": {
                    "interchanges": sum(
                        1
                        for s in result.scope_statuses
                        if s.scope == "interchange"
                        and not s.scope_id.startswith("stray:")
                    ),
                    "groups": sum(
                        1 for s in result.scope_statuses if s.scope == "group"
                    ),
                    "transactions": sum(
                        1 for s in result.scope_statuses if s.scope == "transaction"
                    ),
                    "failed_transactions": sum(
                        1
                        for s in result.scope_statuses
                        if s.scope == "transaction" and s.status != "ok"
                    ),
                    "shipments": len(result.shipments),
                    "orders": len(result.orders),
                    "packages": len(result.packages),
                    "items": len(result.items),
                    "hl_groups": len(result.hl_facts),
                    "segments": len(result.segments),
                },
                "support_boundary": SUPPORT_BOUNDARY,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return paths


def parse_file(path: str | Path) -> ParseResult:
    source_path = Path(path)
    return parse(
        source_path.read_bytes().decode("utf-8-sig"),
        source_name=str(source_path),
    )
