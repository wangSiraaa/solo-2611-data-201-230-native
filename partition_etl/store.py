"""Durable state: event ledger, partition store, output versions, runs."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigError, Rejected

STATE_FILE = "state.json"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )


class StateStore:
    """Single JSON document, replaced atomically after every ingest run."""

    def __init__(self, root: Path) -> None:
        self.path = root / STATE_FILE
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._fresh()
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read state {self.path}: {exc}") from None
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ConfigError(f"unsupported state schema in {self.path}")
        fresh = self._fresh()
        for key, value in fresh.items():
            data.setdefault(key, value)
        return data

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {
            "schema": 1,
            "run_seq": 0,
            "event_seq": 0,
            "events": {},
            "event_ids": {},
            "partitions": {},
            "rejections": {},
            "outputs": {},
            "runs": [],
        }

    def save(self) -> None:
        write_json(self.path, self.data)

    @property
    def events(self) -> dict[str, Any]:
        return self.data["events"]

    @property
    def event_ids(self) -> dict[str, Any]:
        return self.data["event_ids"]

    @property
    def partitions(self) -> dict[str, Any]:
        return self.data["partitions"]

    @property
    def outputs(self) -> dict[str, Any]:
        return self.data["outputs"]

    @property
    def rejections(self) -> dict[str, Any]:
        return self.data["rejections"]

    def effective(self, source: str, day: str) -> dict[str, Any] | None:
        return self.partitions.get(source, {}).get(day, {}).get("effective")

    def last_rejection(self, source: str, day: str) -> dict[str, Any] | None:
        return self.rejections.get(source, {}).get(day)

    def begin_run(self, trigger: str) -> dict[str, Any]:
        self.data["run_seq"] += 1
        run = {
            "run_id": self.data["run_seq"],
            "trigger": trigger,
            "status": "partial",
            "outputs": {},
            "created": {},
            "reused": {},
            "etl": [],
        }
        self.data["runs"].append(run)
        return run

    @staticmethod
    def finish_run(run: dict[str, Any]) -> None:
        failed = [name for name, info in run["outputs"].items() if not info.get("ok")]
        if failed:
            run["status"] = "partial"
        elif run["created"]:
            run["status"] = "ok"
        else:
            run["status"] = "unchanged"

    def record_event(
        self,
        seq: int,
        raw: Any,
        outcome: str,
        code: str,
        *,
        accepted_seq: int | None = None,
        reason: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "seq": seq,
            "outcome": outcome,
            "code": code,
            "event": raw,
        }
        if reason is not None:
            record["reason"] = reason
        if accepted_seq is not None:
            record["accepted_seq"] = accepted_seq
        self.events[str(seq)] = record


class PathResolver:
    """Maps event paths to files under the input root without escaping it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise Rejected("path_rejected", "path must be a non-empty string")
        if "\\" in raw_path:
            raise Rejected("path_rejected", "path must use POSIX separators", path=raw_path)
        pure = PurePosixPath(raw_path)
        if pure.is_absolute() or any(part == ".." for part in pure.parts):
            raise Rejected("path_rejected", "path must stay within input_root", path=raw_path)
        candidate = (self.root / pure).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise Rejected("path_rejected", "path escapes input_root", path=raw_path) from None
        return candidate
