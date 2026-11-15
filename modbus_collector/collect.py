"""Collect one version-fenced snapshot, never splicing two device updates.

Round structure:

1. read the independent version register (``fence-before``)
2. execute the planned data requests
3. read the version register again (``fence-after``)

A round is ``coherent`` only when both version reads succeeded, both values are
even, they are equal, and every data request was answered without a protocol
error or exception. Odd/changed version values trigger a bounded number of
whole-round re-reads. When those are exhausted, the last round's real
observations are retained with ``quality = incoherent``.

The version register is only evidence about this device's in-flight update;
the collector never derives a permanent sequence number from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .decode import decode_registers
from .errors import ProtocolError, TransportError
from .modbus import Exchange, ModbusTcpClient
from .planner import DataRequest, Plan
from .points import Point

_EXCEPTION_NAMES = {
    1: "illegal_function",
    2: "illegal_data_address",
    3: "illegal_data_value",
    4: "server_device_failure",
    5: "acknowledge",
    6: "server_device_busy",
    8: "memory_parity_error",
    10: "gateway_path_unavailable",
    11: "gateway_target_failed_to_respond",
}


def _exception_name(code: int) -> str:
    return _EXCEPTION_NAMES.get(code, "unknown_exception")


@dataclass
class Attempt:
    """One whole-round read attempt."""

    number: int
    fence_before: Exchange | None = None
    fence_after: Exchange | None = None
    data: list[tuple[DataRequest, Exchange | None]] = field(default_factory=list)
    reconnect_events: list[dict[str, Any]] = field(default_factory=list)
    aborted: str | None = None

    # -- version fence ------------------------------------------------------
    @property
    def version_before(self) -> int | None:
        if self.fence_before is None or not self.fence_before.ok:
            return None
        return self.fence_before.registers[0] if self.fence_before.registers else None

    @property
    def version_after(self) -> int | None:
        if self.fence_after is None or not self.fence_after.ok:
            return None
        return self.fence_after.registers[0] if self.fence_after.registers else None

    @property
    def fences_ok(self) -> bool:
        return (
            self.fence_before is not None
            and self.fence_before.ok
            and self.fence_after is not None
            and self.fence_after.ok
        )

    @property
    def versions_even(self) -> bool:
        before, after = self.version_before, self.version_after
        return (
            before is not None
            and after is not None
            and before % 2 == 0
            and after % 2 == 0
        )

    @property
    def versions_equal(self) -> bool:
        return self.version_before is not None and self.version_before == self.version_after

    @property
    def version_coherent(self) -> bool:
        return self.fences_ok and self.versions_even and self.versions_equal

    # -- data ---------------------------------------------------------------
    @property
    def failed_data(self) -> list[tuple[DataRequest, Exchange | None]]:
        return [
            (request, exchange)
            for request, exchange in self.data
            if exchange is None or not exchange.ok
        ]

    @property
    def data_complete(self) -> bool:
        return bool(self.data) and not self.failed_data

    def data_exchange(self, request: DataRequest) -> Exchange | None:
        for item, exchange in self.data:
            if item.sequence == request.sequence:
                return exchange
        return None

    @property
    def quality(self) -> dict[str, Any]:
        if self.aborted:
            return {
                "name": "incoherent",
                "reason": f"round aborted: {self.aborted}",
                "complete_snapshot": False,
            }
        if not self.version_coherent:
            if not self.fences_ok:
                reason = "version fence read failed"
            elif not self.versions_even:
                reason = (
                    f"version is odd (before={self.version_before}, "
                    f"after={self.version_after}); a device update may be mid-flight"
                )
            else:
                reason = (
                    f"version changed within the round "
                    f"(before={self.version_before}, after={self.version_after})"
                )
            return {
                "name": "incoherent",
                "reason": reason,
                "complete_snapshot": False,
            }
        if not self.data_complete:
            failed = [request.sequence for request, _ in self.failed_data]
            return {
                "name": "degraded",
                "reason": (
                    "version fence is stable and even, but data request(s) "
                    f"{failed} failed; unrelated observations retained"
                ),
                "complete_snapshot": False,
            }
        return {
            "name": "coherent",
            "reason": "even and unchanged version fence across a complete round",
            "complete_snapshot": True,
        }

    @property
    def retryable(self) -> bool:
        # Only version uncertainty justifies another whole-round read. A data
        # exception or protocol mismatch degrades this round; re-reading the
        # whole device is not a remedy and would not recover the observation.
        return not self.version_coherent

    # -- export -------------------------------------------------------------
    def as_dict(self, *, selected: bool) -> dict[str, Any]:
        fences: list[dict[str, Any]] = []
        if self.fence_before is not None:
            fences.append(self.fence_before.as_dict(role="fence-before"))
        if self.fence_after is not None:
            fences.append(self.fence_after.as_dict(role="fence-after"))
        return {
            "attempt": self.number,
            "selected": selected,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "fences_ok": self.fences_ok,
            "versions_even": self.versions_even,
            "versions_equal": self.versions_equal,
            "version_coherent": self.version_coherent,
            "coherent": self.quality["name"] == "coherent",
            "quality": self.quality,
            "aborted": self.aborted,
            "version_fence": fences,
            "data_requests": [
                {
                    "sequence": request.sequence,
                    "start": request.start,
                    "count": request.count,
                    "point_ids": list(request.point_ids),
                    "exchange": exchange.as_dict(role="data")
                    if exchange is not None
                    else None,
                }
                for request, exchange in self.data
            ],
            "reconnect_events": self.reconnect_events,
        }


@dataclass
class Collection:
    host: str
    port: int
    plan: Plan
    attempts: list[Attempt]
    selected: Attempt
    retries: int
    mismatches: list[dict[str, Any]]
    started_at: str
    finished_at: str

    @property
    def quality(self) -> dict[str, Any]:
        return self.selected.quality

    @property
    def complete_snapshot(self) -> bool:
        return bool(self.quality["complete_snapshot"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.plan.table.unit_id,
            "host": self.host,
            "port": self.port,
            "version_register": self.plan.table.version_register,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "retries_allowed": self.retries,
            "rounds": [
                attempt.as_dict(selected=attempt is self.selected)
                for attempt in self.attempts
            ],
            "selected_round": self.selected.number,
            "version_fence": {
                "before": self.selected.version_before,
                "after": self.selected.version_after,
                "even": self.selected.versions_even,
                "equal": self.selected.versions_equal,
                "coherent": self.selected.version_coherent,
            },
            "quality": self.quality,
            "complete_snapshot": self.complete_snapshot,
            "plan": self.plan.as_dict(),
            "requests": [
                {
                    "sequence": request.sequence,
                    "role": "data",
                    "start": request.start,
                    "count": request.count,
                    "point_ids": list(request.point_ids),
                    "exchange": exchange.as_dict(role="data")
                    if exchange is not None
                    else None,
                    "points_affected": [
                        p.id
                        for p in self.plan.table.points
                        if request.start <= p.address and p.end <= request.end
                    ],
                }
                for request, exchange in self.selected.data
            ],
            "response_mismatches": self.mismatches,
            "points": self.point_records(),
        }

    # -- point records ------------------------------------------------------
    def point_records(self) -> list[dict[str, Any]]:
        mapping = self._register_map()
        records: list[dict[str, Any]] = []
        for point in self.plan.table.points:
            request = self._source_request(point)
            exchange = self.selected.data_exchange(request)
            records.append(
                _point_record(point, request, exchange, mapping, self.selected)
            )
        return records

    def _register_map(self) -> dict[int, int]:
        """Registers successfully observed in the selected round."""
        mapping: dict[int, int] = {}
        for request, exchange in self.selected.data:
            if exchange is None or not exchange.ok or exchange.registers is None:
                continue
            if len(exchange.registers) != request.count:
                continue
            for offset, value in enumerate(exchange.registers):
                mapping[request.start + offset] = value
        return mapping

    def _source_request(self, point: Point) -> DataRequest:
        for request in self.plan.data_requests:
            if request.start <= point.address and point.end <= request.end:
                return request
        raise AssertionError(f"point {point.id} has no source request")


def _point_record(
    point: Point,
    request: DataRequest,
    exchange: Exchange | None,
    mapping: dict[int, int],
    attempt: Attempt,
) -> dict[str, Any]:
    source = {
        "sequence": request.sequence,
        "start": request.start,
        "count": request.count,
        "transaction_id": exchange.transaction_id if exchange is not None else None,
    }
    fence = {
        "round": attempt.number,
        "version_before": attempt.version_before,
        "version_after": attempt.version_after,
        "coherent": attempt.version_coherent,
    }
    record: dict[str, Any] = {
        "id": point.id,
        "address": point.address,
        "end_exclusive": point.end,
        "width": point.width,
        "type": point.type,
        "byte_order": point.byte_order,
        "word_order": point.word_order,
        "scale": float(point.scale_text),
        "source_request": source,
        "version_fence": fence,
        "raw_registers": None,
        "raw_bytes_hex": None,
        "decoded": None,
        "value": None,
        "valid": False,
        "quality": None,
        "error": None,
    }

    if exchange is None:
        record["quality"] = "request_not_executed"
        record["error"] = {
            "kind": "transport",
            "message": "data request was not executed (connection failure)",
        }
        return record
    if exchange.error is not None:
        record["quality"] = f"response_{exchange.error['kind']}"
        record["error"] = dict(exchange.error)
        return record
    if exchange.exception_code is not None:
        record["quality"] = "exception"
        record["error"] = {
            "kind": "modbus_exception",
            "exception_code": exchange.exception_code,
            "exception_name": _exception_name(exchange.exception_code),
        }
        return record

    values = [mapping.get(address) for address in range(point.address, point.end)]
    if any(value is None for value in values):
        record["quality"] = "missing_registers"
        record["error"] = {
            "kind": "incomplete_response",
            "message": "response did not contain every register for this point",
        }
        return record
    registers = [int(value) for value in values]
    kind, raw, decoded = decode_registers(point, registers)
    record["raw_registers"] = registers
    record["raw_bytes_hex"] = raw.hex()
    record["decoded"] = {
        "kind": kind,
        "scale": float(point.scale_text),
        "value": decoded,
    }

    if kind == "nan-or-inf":
        record["quality"] = "invalid_nonfinite_float"
        record["value"] = None
        record["valid"] = False
        record["error"] = {
            "kind": "nonfinite_float",
            "message": (
                "raw registers decode to NaN or Infinity; bytes are preserved "
                "and no JSON number is produced"
            ),
        }
        return record

    record["value"] = decoded
    record["valid"] = True
    round_name = attempt.quality["name"]
    record["quality"] = "good" if round_name == "coherent" else f"observed_{round_name}_round"
    return record


def _last_failed_exchange(
    client: ModbusTcpClient, request: DataRequest
) -> Exchange | None:
    for exchange in reversed(client.exchanges):
        if (
            exchange.start == request.start
            and exchange.count == request.count
            and exchange.error is not None
        ):
            return exchange
    return None


def _run_attempt(plan: Plan, client: ModbusTcpClient, number: int) -> Attempt:
    attempt = Attempt(number=number)

    def reconnect(reason: str, kind: str) -> bool:
        attempt.reconnect_events.append({"kind": kind, "reason": reason})
        client.close()
        try:
            client.connect()
        except TransportError as exc:
            attempt.aborted = f"reconnect failed: {exc}"
            return False
        return True

    try:
        attempt.fence_before = client.read_holding_registers(
            plan.table.version_register, 1
        )
    except ProtocolError as exc:
        # The frame was drained; the socket is still aligned, but a fence that
        # cannot be read makes this round non-coherent regardless.
        attempt.aborted = f"fence-before failed: {exc}"
        return attempt
    except TransportError as exc:
        # A broken connection must not be reused by the next attempt.
        client.close()
        attempt.aborted = f"fence-before failed: {exc}"
        return attempt

    for request in plan.data_requests:
        try:
            exchange = client.read_holding_registers(request.start, request.count)
        except ProtocolError as exc:
            attempt.data.append((request, _last_failed_exchange(client, request)))
            # A mismatched frame has been fully drained; drop the connection so
            # neither its bytes nor any late frame can be attributed later.
            if not reconnect(
                f"data request {request.sequence} failed: {exc}", exc.kind
            ):
                return attempt
            continue
        except TransportError as exc:
            attempt.data.append((request, None))
            if not reconnect(
                f"data request {request.sequence} failed: {exc}", "transport"
            ):
                return attempt
            continue
        attempt.data.append((request, exchange))

    try:
        attempt.fence_after = client.read_holding_registers(
            plan.table.version_register, 1
        )
    except ProtocolError as exc:
        attempt.aborted = f"fence-after failed: {exc}"
    except TransportError as exc:
        client.close()
        attempt.aborted = f"fence-after failed: {exc}"
    return attempt


def collect(
    plan: Plan,
    *,
    host: str = "127.0.0.1",
    port: int = 11502,
    timeout: float = 3.0,
    retries: int = 2,
    retry_delay: float = 0.0,
    first_transaction_id: int = 1,
) -> Collection:
    """Collect one round, re-reading it whole at most ``retries`` times."""
    if retries < 0:
        raise ValueError("retries must be non-negative")
    started_at = datetime.now(timezone.utc).isoformat()
    attempts: list[Attempt] = []
    selected: Attempt | None = None
    client = ModbusTcpClient(
        host=host,
        port=port,
        unit_id=plan.table.unit_id,
        timeout=timeout,
        first_transaction_id=first_transaction_id,
    )
    try:
        try:
            client.connect()
        except TransportError:
            selected = Attempt(number=0, aborted="connect failed")
            attempts.append(selected)
        else:
            for number in range(retries + 1):
                attempt = _run_attempt(plan, client, number)
                attempts.append(attempt)
                selected = attempt
                if not attempt.retryable or number == retries:
                    break
                if retry_delay > 0:
                    time.sleep(retry_delay)
                if client.socket_closed:
                    try:
                        client.connect()
                    except TransportError:
                        break
    finally:
        client.close()
    assert selected is not None
    return Collection(
        host=host,
        port=port,
        plan=plan,
        attempts=attempts,
        selected=selected,
        retries=retries,
        mismatches=list(client.mismatches),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
