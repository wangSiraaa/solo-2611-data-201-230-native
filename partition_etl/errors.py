"""Exception types used by the partition assembly pipeline."""

from __future__ import annotations

from typing import Any


class AssemblyError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(AssemblyError):
    """Configuration or persisted-state problem."""


class Rejected(AssemblyError):
    """An event or its referenced content was rejected.

    The code is a stable machine-readable identifier; detail is JSONable and
    is copied into the audit ledger.
    """

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class BuildError(AssemblyError):
    """A resolved input vector could not be turned into an output version."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
