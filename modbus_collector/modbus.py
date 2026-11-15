"""Minimal Modbus TCP client for function code 03, read-only.

Only holding-register reads are exposed. Every response is checked against
the request it claims to answer; a mismatched frame is rejected rather than
interpreted as data for any request.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError, TransportError

MAX_PDU_BYTES = 253


@dataclass
class Exchange:
    """A validated request/response pair, as observed on the wire."""

    requested_transaction_id: int
    transaction_id: int
    protocol_id: int
    unit_id: int
    function: int
    start: int
    count: int
    registers: list[int] | None
    exception_code: int | None
    error: dict[str, Any] | None = None
    raw_request_mbap: bytes = field(repr=False, default=b"")
    raw_request_pdu: bytes = field(repr=False, default=b"")
    raw_mbap: bytes = field(repr=False, default=b"")
    raw_pdu: bytes = field(repr=False, default=b"")

    @property
    def ok(self) -> bool:
        return self.error is None and self.exception_code is None

    def as_dict(self, role: str = "data") -> dict[str, Any]:
        return {
            "role": role,
            "requested_transaction_id": self.requested_transaction_id,
            "transaction_id": self.transaction_id,
            "protocol_id": self.protocol_id,
            "unit_id": self.unit_id,
            "function": self.function,
            "start": self.start,
            "count": self.count,
            "ok": self.ok,
            "exception_code": self.exception_code,
            "error": self.error,
            "registers": list(self.registers) if self.registers is not None else None,
            "raw_request_hex": (self.raw_request_mbap + self.raw_request_pdu).hex(),
            "raw_response_hex": (self.raw_mbap + self.raw_pdu).hex(),
        }


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout as exc:
            raise TransportError(
                f"timed out after {len(chunks)} of {size} response bytes"
            ) from exc
        except OSError as exc:
            raise TransportError(f"socket error: {exc}") from exc
        if not chunk:
            raise TransportError(
                f"connection closed after {len(chunks)} of {size} response bytes"
            )
        chunks.extend(chunk)
    return bytes(chunks)


class ModbusTcpClient:
    """One-connection read-only Modbus TCP client.

    Transaction ids start at ``first_transaction_id`` and increment by one for
    each request. They exist to match responses, not to order device updates.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11502,
        unit_id: int = 1,
        timeout: float = 3.0,
        first_transaction_id: int = 1,
    ) -> None:
        if not 0 <= unit_id <= 255:
            raise ValueError("unit_id must be in 0..255")
        if not 0 <= first_transaction_id <= 0xFFFF:
            raise ValueError("first_transaction_id must be a 16-bit integer")
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._next_transaction_id = first_transaction_id
        self._socket: socket.socket | None = None
        self.exchanges: list[Exchange] = []
        self.mismatches: list[dict[str, Any]] = []

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise TransportError(
                f"cannot connect to {self.host}:{self.port}: {exc}"
            ) from exc
        sock.settimeout(self.timeout)
        self._socket = sock

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def __enter__(self) -> "ModbusTcpClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def socket_closed(self) -> bool:
        """True when no live connection is held (e.g. dropped after an error)."""
        return self._socket is None

    # -- request ------------------------------------------------------------
    def _take_transaction_id(self) -> int:
        tid = self._next_transaction_id
        self._next_transaction_id = (tid + 1) & 0xFFFF
        return tid

    def read_holding_registers(self, start: int, count: int) -> Exchange:
        """Issue one 0x03 request; return the validated exchange.

        Protocol-level problems (bad transaction id, protocol id, unit id,
        length, function or byte count) raise ``ProtocolError`` and are also
        recorded on the client. Function exceptions are returned in the
        exchange with ``exception_code`` set.
        """
        if self._socket is None:
            raise TransportError("client is not connected")
        if not 0 <= start <= 0xFFFF:
            raise ValueError("start must be a 16-bit address")
        if not 1 <= count <= 125:
            raise ValueError("count must be in 1..125")

        transaction_id = self._take_transaction_id()
        pdu = struct.pack("!BHH", 3, start, count)
        mbap = struct.pack("!HHHB", transaction_id, 0, len(pdu) + 1, self.unit_id)
        try:
            self._socket.sendall(mbap + pdu)
            header = _recv_exact(self._socket, 7)
        except TransportError as exc:
            mismatch = {
                "kind": "transport",
                "request": {
                    "transaction_id": transaction_id,
                    "start": start,
                    "count": count,
                },
                "message": str(exc),
            }
            self.mismatches.append(mismatch)
            raise

        try:
            resp_tid, resp_proto, resp_length, resp_unit = struct.unpack(
                "!HHHB", header
            )
            if resp_length < 2:
                raise ProtocolError(
                    "length",
                    f"MBAP length {resp_length} is below the 2-byte minimum",
                    expected=">=2",
                    actual=resp_length,
                )
            if resp_length > MAX_PDU_BYTES:
                raise ProtocolError(
                    "length",
                    f"MBAP length {resp_length} exceeds {MAX_PDU_BYTES}",
                    expected=f"<= {MAX_PDU_BYTES}",
                    actual=resp_length,
                )
            # Read the advertised frame first; structural checks happen against
            # the request afterwards. That keeps the socket aligned for
            # diagnostics even when the frame is rejected.
            payload = _recv_exact(self._socket, resp_length - 1)
        except ProtocolError as exc:
            mismatch = {
                "kind": exc.kind,
                "request": {
                    "transaction_id": transaction_id,
                    "start": start,
                    "count": count,
                },
                "expected": exc.expected,
                "actual": exc.actual,
                "message": str(exc),
            }
            self.mismatches.append(mismatch)
            raise
        except TransportError as exc:
            self.mismatches.append(
                {
                    "kind": "transport",
                    "request": {
                        "transaction_id": transaction_id,
                        "start": start,
                        "count": count,
                    },
                    "message": str(exc),
                }
            )
            raise

        raw_mbap = header
        raw_pdu = payload
        exchange = Exchange(
            requested_transaction_id=transaction_id,
            transaction_id=resp_tid,
            protocol_id=resp_proto,
            unit_id=resp_unit,
            function=payload[0] if payload else -1,
            start=start,
            count=count,
            registers=None,
            exception_code=None,
            raw_request_mbap=mbap,
            raw_request_pdu=pdu,
            raw_mbap=raw_mbap,
            raw_pdu=raw_pdu,
        )
        self.exchanges.append(exchange)
        try:
            self._validate_response(
                exchange,
                expected_tid=transaction_id,
                expected_unit=self.unit_id,
                expected_function=3,
                expected_count=count,
                protocol_id=resp_proto,
                length=resp_length,
            )
        except ProtocolError as exc:
            self.mismatches.append(
                {
                    "kind": exc.kind,
                    "request": {
                        "transaction_id": transaction_id,
                        "start": start,
                        "count": count,
                    },
                    "expected": exc.expected,
                    "actual": exc.actual,
                    "message": str(exc),
                    "response_transaction_id": resp_tid,
                    "response_unit_id": resp_unit,
                    "response_protocol_id": resp_proto,
                    "response_function": exchange.function,
                }
            )
            exchange.error = {
                "kind": exc.kind,
                "message": str(exc),
                "expected": exc.expected,
                "actual": exc.actual,
            }
            # The frame was fully drained above, and the caller discards the
            # connection so no stale bytes can be read as a later response.
            raise
        return exchange

    def _validate_response(
        self,
        exchange: Exchange,
        *,
        expected_tid: int,
        expected_unit: int,
        expected_function: int,
        expected_count: int,
        protocol_id: int,
        length: int,
    ) -> None:
        if exchange.transaction_id != expected_tid:
            raise ProtocolError(
                "transaction_id",
                f"transaction id {exchange.transaction_id} does not match "
                f"request {expected_tid}",
                expected=expected_tid,
                actual=exchange.transaction_id,
            )
        if protocol_id != 0:
            raise ProtocolError(
                "protocol_id",
                f"protocol id {protocol_id} is not Modbus (0)",
                expected=0,
                actual=protocol_id,
            )
        if exchange.unit_id != expected_unit:
            raise ProtocolError(
                "unit_id",
                f"unit id {exchange.unit_id} does not match {expected_unit}",
                expected=expected_unit,
                actual=exchange.unit_id,
            )
        pdu = exchange.raw_pdu
        if not pdu:
            raise ProtocolError("function", "empty PDU", expected=3, actual=None)
        function = pdu[0]
        if function == (expected_function | 0x80):
            if len(pdu) != 2:
                raise ProtocolError(
                    "exception_length",
                    f"exception PDU has {len(pdu)} bytes, expected 2",
                    expected=2,
                    actual=len(pdu),
                )
            if length != 3:
                raise ProtocolError(
                    "length",
                    f"exception response MBAP length {length} is not 3",
                    expected=3,
                    actual=length,
                )
            exchange.exception_code = pdu[1]
            return
        if function != expected_function:
            raise ProtocolError(
                "function",
                f"function {function} does not match request {expected_function}",
                expected=expected_function,
                actual=function,
            )
        expected_bytes = expected_count * 2
        # For function 03 the MBAP length field must be unit + function +
        # byte-count + data = 3 + 2*count. Checking it against the request's
        # count (not merely against bytes already read) rejects a short or long
        # frame instead of quietly accepting its payload.
        expected_length = 3 + expected_bytes
        if length != expected_length:
            raise ProtocolError(
                "length",
                f"MBAP length {length} does not match 0x03 response for "
                f"{expected_count} register(s), expected {expected_length}",
                expected=expected_length,
                actual=length,
            )
        if len(pdu) < 2:
            raise ProtocolError(
                "byte_count", "response PDU is missing its byte count",
                expected=expected_bytes,
                actual=None,
            )
        byte_count = pdu[1]
        if byte_count != expected_bytes:
            raise ProtocolError(
                "byte_count",
                f"byte count {byte_count} does not match {expected_count} "
                "registers",
                expected=expected_bytes,
                actual=byte_count,
            )
        if len(pdu) != 2 + byte_count:
            raise ProtocolError(
                "length",
                f"PDU carries {len(pdu) - 2} data bytes, byte count says "
                f"{byte_count}",
                expected=2 + byte_count,
                actual=len(pdu),
            )
        data = pdu[2:]
        exchange.registers = list(struct.unpack(f"!{expected_count}H", data))
