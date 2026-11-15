"""Event and completion-notice validation."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import Rejected

SOURCES = ("sales", "fx")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
MAX_VERSION = 2_147_483_647


def parse_date(value: Any, what: str = "partition") -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise Rejected("invalid_partition", f"{what} must be YYYY-MM-DD", value=repr(value))
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise Rejected("invalid_partition", f"{what} is not a real date", value=value) from None
    if parsed.isoformat() != value:
        raise Rejected("invalid_partition", f"{what} is not canonical", value=value)
    return value


def previous_day(day: str) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class Event:
    event_id: str
    source: str
    partition: str
    version: int
    path: str
    sha256: str
    index: int

    def fingerprint(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "partition": self.partition,
            "version": self.version,
            "sha256": self.sha256,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "partition": self.partition,
            "version": self.version,
            "path": self.path,
            "sha256": self.sha256,
        }


def parse_event(raw: Any, index: int) -> Event:
    if not isinstance(raw, Mapping):
        raise Rejected("malformed_event", "event must be a JSON object", index=index)
    missing = [
        field
        for field in ("event_id", "source", "partition", "version", "path", "sha256")
        if field not in raw
    ]
    if missing:
        raise Rejected("malformed_event", "event is missing fields", index=index, missing=missing)

    event_id = raw["event_id"]
    if not isinstance(event_id, str) or not event_id:
        raise Rejected("malformed_event", "event_id must be a non-empty string", index=index)

    source = raw["source"]
    if source not in SOURCES:
        raise Rejected(
            "malformed_event", "source must be sales or fx", event_id=event_id, source=source
        )

    partition = parse_date(raw["partition"])

    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise Rejected(
            "malformed_event", "version must be a positive integer", event_id=event_id, version=version
        )
    if version <= 0 or version > MAX_VERSION:
        raise Rejected(
            "malformed_event", "version must be a positive integer", event_id=event_id, version=version
        )

    path = raw["path"]
    if not isinstance(path, str) or not path:
        raise Rejected(
            "malformed_event", "path must be a non-empty string", event_id=event_id, path=repr(path)
        )

    sha = raw["sha256"]
    if not isinstance(sha, str) or not HEX64_RE.fullmatch(sha):
        raise Rejected(
            "malformed_event", "sha256 must be 64 lowercase hex characters", event_id=event_id
        )

    return Event(event_id, source, partition, version, path, sha, index)


def read_jsonl(text: str) -> list[tuple[str, Any]]:
    """Return ordered items: ("event", Event) or ("rejected", detail mapping)."""
    items: list[tuple[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue  # blank separator lines are not completion notices
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            items.append(
                (
                    "rejected",
                    {"index": index, "code": "malformed_event", "reason": f"invalid JSON: {exc}"},
                )
            )
            continue
        try:
            items.append(("event", parse_event(raw, index)))
        except Rejected as exc:
            detail = {"index": index, "code": exc.code, "reason": exc.message, **exc.detail}
            if isinstance(raw, Mapping):
                detail["event"] = {key: raw.get(key) for key in raw}
            items.append(("rejected", detail))
    return items
