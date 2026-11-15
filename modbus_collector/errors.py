"""Exceptions used by the read-only Modbus TCP collector."""

from __future__ import annotations


class CollectorError(Exception):
    """Base class for all collector errors."""


class ConfigError(CollectorError):
    """The point table or an option is invalid."""


class PlanError(CollectorError):
    """A valid request plan cannot be built from the point table."""


class TransportError(CollectorError):
    """TCP connection failed or ended before a response was complete."""


class ProtocolError(CollectorError):
    """A response failed MBAP/PDU validation.

    ``kind`` is machine-readable (for example ``transaction_id``); ``expected``
    and ``actual`` hold the offending values when relevant. A caller must not
    use register data from a response that raised this error.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        expected: object = None,
        actual: object = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.expected = expected
        self.actual = actual
