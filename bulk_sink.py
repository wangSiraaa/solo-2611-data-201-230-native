#!/usr/bin/env python3
"""Local item-level bulk sink for an Elasticsearch-compatible endpoint.

Scope (by design): fixed ``index`` bulk operations and ``/_mget`` reconciliation
only. No search, cluster management, authentication or serving platform.

Item-level semantics:
  * records are batched in input order; records sharing the same (index, id)
    are strictly serialised -- a later version is never dispatched while an
    earlier one is still unresolved;
  * each record gets at most ``MAX_WRITE_ATTEMPTS`` bulk dispatches;
  * a top-level HTTP 200 on /_bulk is never treated as success: every item is
    checked by its request position and its own _index/_id and item status
    (200/201 accepted, 429 retryable, 400 permanent mapping error);
  * 409 or a broken connection leaves the outcome uncertain, so /_mget is used
    to classify the target: same version + same fingerprint -> confirmed,
    higher target version -> superseded, same version + different fingerprint
    -> conflict, missing/lower target -> the item may be resent;
  * confirmed and permanently failed items are never re-sent.

Everything that happened (each attempt, raw response/read-back evidence, final
receipt) is persisted under the work directory.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import json
import pathlib
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

MAX_WRITE_ATTEMPTS = 3          # hard cap on bulk dispatches per source record
MGET_TRANSPORT_TRIES = 3        # /_mget retries when the transport itself fails
REQUEST_TIMEOUT = 10.0

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"
CONFLICT = "conflict"
SUPERSEDED = "superseded"
UNRESOLVED = "unresolved"

TERMINAL_STATUSES = {CONFIRMED, REJECTED, CONFLICT, SUPERSEDED, UNRESOLVED}


class SinkError(Exception):
    """Base class for sink failures."""


class TransportError(SinkError):
    """The HTTP exchange did not produce a complete, parseable response."""


# --------------------------------------------------------------------------- #
# canonicalisation / fingerprint
# --------------------------------------------------------------------------- #
def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Canonical JSON per spec: UTF-8, no ASCII escaping, sorted keys,
    compact separators and NaN/Infinity rejected."""
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fingerprint_of(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


# --------------------------------------------------------------------------- #
# minimal HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, raw: bytes | None, timeout: float,
             content_type: str) -> tuple[int, bytes]:
    headers = {"Content-Type": content_type}
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # complete response with 4xx/5xx
        return exc.code, exc.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # RemoteDisconnected is an OSError subclass; socket timeouts surface
        # here too. The write may have landed server-side: uncertain.
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc


def http_post_json(base_url: str, path: str, payload: dict, timeout: float) -> tuple[int, bytes]:
    return _request("POST", base_url.rstrip("/") + path,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout, "application/json")


def http_post_ndjson(base_url: str, path: str, raw: bytes, timeout: float) -> tuple[int, bytes]:
    return _request("POST", base_url.rstrip("/") + path, raw,
                    timeout, "application/x-ndjson")


def http_get(base_url: str, path: str, timeout: float) -> tuple[int, bytes]:
    return _request("GET", base_url.rstrip("/") + path, None, timeout, "application/json")


# --------------------------------------------------------------------------- #
# record state
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Record:
    order: int
    source_id: str
    index: str
    id: str
    version: int
    document: dict[str, Any]
    fingerprint: str = ""
    attempts: int = 0                       # bulk dispatches already made
    status: str = PENDING
    reason: str | None = None
    history: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.index, self.id

    def receipt(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "order": self.order,
            "index": self.index,
            "id": self.id,
            "version": self.version,
            "document": self.document,           # original values preserved
            "fingerprint": self.fingerprint,
            "status": self.status,
            "reason": self.reason,
            "attempts": self.attempts,
            "history": self.history,
        }


class BulkSink:
    """Drives JSONL records into the downstream bulk endpoint."""

    def __init__(self, base_url: str, work_dir: str | pathlib.Path,
                 batch_size: int, retry_interval: float,
                 timeout: float = REQUEST_TIMEOUT):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.base_url = base_url.rstrip("/")
        self.work_dir = pathlib.Path(work_dir)
        self.batch_size = batch_size
        self.retry_interval = retry_interval
        self.timeout = timeout
        self.records: list[Record] = []
        self._round = 0
        for sub in ("bulk", "mget", "receipts"):
            (self.work_dir / sub).mkdir(parents=True, exist_ok=True)
        (self.work_dir / "config.json").write_text(json.dumps({
            "base_url": self.base_url,
            "batch_size": batch_size,
            "retry_interval": retry_interval,
            "timeout": timeout,
            "max_write_attempts": MAX_WRITE_ATTEMPTS,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ #
    # input / local validation
    # ------------------------------------------------------------------ #
    def load(self, input_path: str | pathlib.Path) -> None:
        for order, line in enumerate(pathlib.Path(input_path).read_text(
                encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            rec = Record(
                order=order,
                source_id=row["source_id"],
                index=row["index"],
                id=row["id"],
                version=row["version"],
                document=row["document"],
            )
            # Local validation only covers things that cannot possibly be
            # submitted. Type errors inside the document (e.g. amount_cents
            # being a string) are deliberately left untouched so the real
            # downstream mapping error comes back from the server.
            if not isinstance(rec.version, int) or isinstance(rec.version, bool) \
                    or rec.version <= 0:
                rec.status = REJECTED
                rec.reason = "local_invalid_version: must be a positive integer"
            else:
                try:
                    rec.fingerprint = fingerprint_of(rec.document)
                except (ValueError, TypeError) as exc:
                    rec.status = REJECTED
                    rec.reason = f"local_unserializable_document: {exc}"
            self.records.append(rec)
        ids = [r.source_id for r in self.records]
        if len(ids) != len(set(ids)):
            raise SinkError("source_id must be unique in the input")
        self._flush()

    # ------------------------------------------------------------------ #
    # batching: input order, same (index,id) serialised
    # ------------------------------------------------------------------ #
    def _build_batch(self) -> list[Record]:
        batch: list[Record] = []
        keys: set[tuple[str, str]] = set()
        for rec in self.records:
            if rec.status != PENDING:
                continue
            if rec.attempts >= MAX_WRITE_ATTEMPTS:
                rec.status = UNRESOLVED
                rec.reason = "write_attempt_budget_exhausted"
                continue
            if rec.key in keys:
                # An earlier version of the same business id is already in
                # this bulk and still in flight: stop, do not overtake it.
                break
            batch.append(rec)
            keys.add(rec.key)
            if len(batch) == self.batch_size:
                break
        return batch

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def send(self) -> dict[str, Any]:
        if not self.records:
            raise SinkError("no records loaded")
        while True:
            batch = self._build_batch()
            self._flush()
            if not batch:
                break
            self._round += 1
            self._dispatch(self._round, batch)
            self._flush()
            if self._work_remains():
                time.sleep(self.retry_interval)
        # Defensive safety net: nothing should still be pending here.
        for rec in self.records:
            if rec.status == PENDING:
                rec.status = UNRESOLVED
                rec.reason = "unresolved_after_send_loop"
        self._flush()
        return status(self.work_dir)

    def _work_remains(self) -> bool:
        return any(r.status == PENDING and r.attempts < MAX_WRITE_ATTEMPTS
                   for r in self.records)

    # ------------------------------------------------------------------ #
    # one bulk round
    # ------------------------------------------------------------------ #
    def _dispatch(self, round_no: int, batch: list[Record]) -> None:
        round_dir = self.work_dir / "bulk" / f"round-{round_no}"
        round_dir.mkdir(parents=True, exist_ok=True)
        payload, request_view = b"", []
        for position, rec in enumerate(batch):
            meta = {"index": {
                "_index": rec.index,
                "_id": rec.id,
                "version": rec.version,
                "version_type": "external",
            }}
            body = dict(rec.document)
            body["ingest_fingerprint"] = rec.fingerprint
            payload += canonical_bytes(meta) + b"\n" + canonical_bytes(body) + b"\n"
            request_view.append({
                "position": position,
                "source_id": rec.source_id,
                "index": rec.index,
                "id": rec.id,
                "version": rec.version,
                "submitted_source": body,
            })
        (round_dir / "request.ndjson").write_bytes(payload)
        (round_dir / "request.json").write_text(
            json.dumps(request_view, ensure_ascii=False, indent=2), encoding="utf-8")
        for rec in batch:
            rec.attempts += 1

        try:
            http_status, raw = http_post_ndjson(
                self.base_url, "/_bulk", payload, self.timeout)
        except TransportError as exc:
            (round_dir / "transport_error.txt").write_text(str(exc), encoding="utf-8")
            for position, rec in enumerate(batch):
                rec.history.append({
                    "round": round_no, "kind": "bulk", "position": position,
                    "http_status": None,
                    "outcome": "transport_uncertain",
                    "detail": str(exc),
                    "evidence": f"bulk/round-{round_no}/transport_error.txt",
                })
            # The server may have persisted a processed prefix. Every item is
            # uncertain until /_mget says otherwise.
            self._reconcile(round_no, batch)
            return

        (round_dir / "response.json").write_bytes(raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            items = parsed["items"]
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            (round_dir / "malformed_response.txt").write_text(
                f"http={http_status} error={exc}", encoding="utf-8")
            for position, rec in enumerate(batch):
                rec.history.append({
                    "round": round_no, "kind": "bulk", "position": position,
                    "http_status": http_status,
                    "outcome": "transport_uncertain",
                    "detail": f"unparseable bulk response: {exc}",
                    "evidence": f"bulk/round-{round_no}/response.json",
                })
            self._reconcile(round_no, batch)
            return

        # Position/ID cross-check: the response must line up item by item.
        aligned = (isinstance(items, list) and len(items) == len(batch)
                   and all(
                       isinstance(it.get("index"), dict)
                       and it["index"].get("_index") == rec.index
                       and it["index"].get("_id") == rec.id
                       for it, rec in zip(items, batch)))
        if not aligned:
            for position, rec in enumerate(batch):
                rec.history.append({
                    "round": round_no, "kind": "bulk", "position": position,
                    "http_status": http_status,
                    "outcome": "transport_uncertain",
                    "detail": "bulk response missing items or position/id mismatch",
                    "evidence": f"bulk/round-{round_no}/response.json",
                })
            self._reconcile(round_no, batch)
            return

        uncertain: list[Record] = []
        for position, (rec, item) in enumerate(zip(batch, items)):
            entry = item["index"]
            item_status = entry.get("status")
            base = {"round": round_no, "kind": "bulk", "position": position,
                    "http_status": http_status, "item_status": item_status,
                    "evidence": f"bulk/round-{round_no}/response.json"}
            if item_status in (200, 201):  # item-level acceptance only
                rec.history.append({**base, "outcome": "accepted",
                                    "detail": entry.get("result", "created/indexed")})
                rec.status = CONFIRMED
                rec.reason = f"bulk_item_status_{item_status}_accepted"
            elif item_status == 429:      # definite non-write, retryable
                rec.history.append({**base, "outcome": "retryable",
                                    "detail": entry.get("error", {})})
                rec.reason = "retryable_429"
            elif item_status == 400:      # definite permanent mapping failure
                rec.history.append({**base, "outcome": "mapping_error",
                                    "detail": entry.get("error", {})})
                rec.status = REJECTED
                rec.reason = f"mapping_error_400: {entry.get('error')}"
            elif item_status == 409:
                # External version conflict: our version did not get written
                # by this call, but the target may already hold identical
                # content (replay), newer content, or same-version different
                # content. /_mget decides; never blindly resend.
                rec.history.append({**base, "outcome": "uncertain_409",
                                    "detail": entry.get("error", {})})
                uncertain.append(rec)
            else:
                rec.history.append({**base, "outcome": "unknown_status",
                                    "detail": entry.get("error", {})})
                uncertain.append(rec)
        if uncertain:
            self._reconcile(round_no, uncertain)

    # ------------------------------------------------------------------ #
    # /_mget reconciliation
    # ------------------------------------------------------------------ #
    def _reconcile(self, round_no: int, records: list[Record]) -> None:
        docs = [{"_index": rec.index, "_id": rec.id} for rec in records]
        mget_path = self.work_dir / "mget" / f"round-{round_no}.json"
        response: dict[str, Any] | None = None
        last_error = ""
        for mget_try in range(1, MGET_TRANSPORT_TRIES + 1):
            evidence = {"round": round_no, "mget_try": mget_try,
                        "request": {"docs": docs}}
            try:
                http_status, raw = http_post_json(
                    self.base_url, "/_mget", {"docs": docs}, self.timeout)
                parsed = json.loads(raw.decode("utf-8"))
                evidence["http_status"] = http_status
                evidence["response"] = parsed
                if http_status == 200 and isinstance(parsed.get("docs"), list) \
                        and len(parsed["docs"]) == len(records):
                    response = parsed
                    break
                last_error = f"bad mget response http={http_status}"
                evidence["transport_error"] = last_error
            except TransportError as exc:
                last_error = str(exc)
                evidence["transport_error"] = last_error
            self._append_mget_evidence(mget_path, evidence)
            if mget_try < MGET_TRANSPORT_TRIES:
                time.sleep(self.retry_interval)

        if response is None:
            # Cannot prove anything. Records stay pending and consume their
            # write budget on later rounds; exhaustion keeps them unresolved.
            for rec in records:
                rec.history.append({
                    "round": round_no, "kind": "mget",
                    "outcome": "mget_unavailable", "detail": last_error,
                    "evidence": f"mget/round-{round_no}.json",
                })
                rec.reason = "mget_unavailable"
            return

        # Persist the successful read-back alongside any failed transport tries.
        self._append_mget_evidence(mget_path, {
            "round": round_no, "mget_try": mget_try,
            "request": {"docs": docs}, "http_status": http_status,
            "response": response,
        })

        by_pos = {
            (d.get("_index"), d.get("_id")): d for d in response["docs"]
        }
        for rec in records:
            doc = by_pos.get(rec.key)
            entry = {"round": round_no, "kind": "mget",
                     "evidence": f"mget/round-{round_no}.json"}
            if doc is None or doc.get("_index") != rec.index or doc.get("_id") != rec.id:
                rec.history.append({**entry, "outcome": "mget_misaligned",
                                    "detail": "mget doc missing or id mismatch"})
                rec.reason = "mget_misaligned"
                continue
            if not doc.get("found"):
                # Target absent: the write demonstrably did not land, resend.
                rec.history.append({**entry, "outcome": "target_missing_resendable",
                                    "target_version": None,
                                    "target_fingerprint": None})
                rec.reason = "target_missing_after_uncertain_write"
                continue
            target_version = doc.get("_version")
            target_fp = doc.get("_source", {}).get("ingest_fingerprint")
            entry.update(target_version=target_version, target_fingerprint=target_fp)
            if target_version == rec.version and target_fp == rec.fingerprint:
                rec.history.append({**entry, "outcome": "confirmed"})
                rec.status = CONFIRMED
                rec.reason = "mget_confirmed_same_version_same_fingerprint"
            elif isinstance(target_version, int) and target_version > rec.version:
                rec.history.append({**entry, "outcome": "superseded"})
                rec.status = SUPERSEDED
                rec.reason = "mget_superseded_target_version_higher"
            elif target_version == rec.version and target_fp != rec.fingerprint:
                rec.history.append({**entry, "outcome": "conflict"})
                rec.status = CONFLICT
                rec.reason = "mget_conflict_same_version_different_fingerprint"
            else:
                # Lower target version (or unusable payload): our write is not
                # there, so the item remains resendable.
                rec.history.append({**entry, "outcome": "target_lower_resendable",
                                    "detail": "target version below ours"})
                rec.reason = "target_lower_after_uncertain_write"

    @staticmethod
    def _append_mget_evidence(path: pathlib.Path, evidence: dict) -> None:
        existing = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
        existing.append(evidence)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _flush(self) -> None:
        for rec in self.records:
            (self.work_dir / "receipts" / f"{rec.source_id}.json").write_text(
                json.dumps(rec.receipt(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        summary = status(self.work_dir)
        (self.work_dir / "status.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# status / verify
# --------------------------------------------------------------------------- #
def _load_receipts(work_dir: pathlib.Path) -> list[dict[str, Any]]:
    receipts = []
    for path in sorted((work_dir / "receipts").glob("*.json")):
        receipts.append(json.loads(path.read_text(encoding="utf-8")))
    receipts.sort(key=lambda r: r["order"])
    return receipts


def status(work_dir: str | pathlib.Path) -> dict[str, Any]:
    """Aggregate the per-source receipts into a run summary."""
    work_dir = pathlib.Path(work_dir)
    receipts = _load_receipts(work_dir)
    counts: Counter[str] = Counter(r["status"] for r in receipts)
    return {
        "work_dir": str(work_dir),
        "total": len(receipts),
        "counts": {s: counts.get(s, 0) for s in
                   (CONFIRMED, REJECTED, CONFLICT, SUPERSEDED, UNRESOLVED, PENDING)},
        "items": [{
            "source_id": r["source_id"],
            "index": r["index"],
            "id": r["id"],
            "version": r["version"],
            "status": r["status"],
            "reason": r["reason"],
            "attempts": r["attempts"],
        } for r in receipts],
    }


def _is_terminal_history_entry(entry: dict[str, Any]) -> bool:
    if entry["kind"] == "bulk" and entry["outcome"] in ("accepted", "mapping_error"):
        return True
    if entry["kind"] == "mget" and entry["outcome"] in (
            "confirmed", "superseded", "conflict"):
        return True
    return False


def verify(base_url: str, work_dir: str | pathlib.Path,
           timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
    """Cross-check local receipts against the mock's /__inspect history.

    Checks:
      * every confirmed item has a real item-level success or a matching
        /_mget read-back as its deciding evidence, and was not retried after;
      * superseded/conflict items carry the /_mget basis described above;
      * rejected items have a 400 (or local) basis and no retry after the
        permanent outcome;
      * unresolved items never show a success and stay within the attempt
        budget;
      * the number of write attempts that reached the server matches the
        local receipts (a disconnected tail may only make the server count
        lower, never higher);
      * same-key records were dispatched in source order with no overlap;
      * the current index content equals the final effective version of each
        id (older confirmed versions may have been superseded and need not be
        current);
      * every stored ingest_fingerprint is the canonical fingerprint of the
        source document.
    """
    work_dir = pathlib.Path(work_dir)
    receipts = _load_receipts(work_dir)
    errors: list[str] = []
    checks: list[str] = []

    _, raw = http_get(base_url, "/__inspect", timeout)
    inspect = json.loads(raw.decode("utf-8"))
    server_attempts = inspect["attempts"]
    server_docs = inspect["docs"]

    def err(msg: str) -> None:
        errors.append(msg)

    # Server-side bulk attempts keyed by exact (index, id, version, body).
    def body_key(index: str, doc_id: str, version: int, source: dict) -> tuple:
        return index, doc_id, version, canonical_bytes(source).decode("utf-8")

    server_counter: Counter[tuple] = Counter(
        body_key(a["index"], a["id"], a["version"], a["source"])
        for a in server_attempts)
    local_counter: Counter[tuple] = Counter()
    uncertain_counter: Counter[tuple] = Counter()

    # Local invariant + per-receipt history checks.
    last_round_by_key: dict[tuple[str, str], int] = {}
    for r in receipts:
        sid = r["source_id"]
        if r.get("fingerprint") and r["fingerprint"] != fingerprint_of(r["document"]):
            err(f"{sid}: stored fingerprint is not the canonical document hash")
        bulk_entries = [h for h in r["history"] if h["kind"] == "bulk"]
        if len(bulk_entries) > MAX_WRITE_ATTEMPTS:
            err(f"{sid}: {len(bulk_entries)} bulk attempts exceeds budget")
        if r["status"] == PENDING:
            err(f"{sid}: still pending in final status")

        # No dispatch after a terminal outcome (the actual "no mistaken retry"
        # rule: uncertain/429 entries before the decision are legitimate).
        terminal_seen = False
        for h in r["history"]:
            if terminal_seen and h["kind"] == "bulk":
                err(f"{sid}: bulk dispatched after a terminal outcome")
            if _is_terminal_history_entry(h):
                terminal_seen = True

        # Same-key serialisation: first dispatch of this record must come
        # after the last round of the preceding record for that key.
        rounds = [h["round"] for h in bulk_entries]
        if rounds:
            first_round = min(rounds)
            prev = last_round_by_key.get((r["index"], r["id"]))
            if prev is not None and first_round <= prev:
                err(f"{sid}: same-key record dispatched before prior version settled")
            last_round_by_key[(r["index"], r["id"])] = max(rounds)

        # Evidence files referenced must exist.
        for h in r["history"]:
            if not (work_dir / h["evidence"]).exists():
                err(f"{sid}: missing evidence file {h['evidence']}")

        outcomes = {h["outcome"] for h in r["history"]}
        mget_entries = [h for h in r["history"] if h["kind"] == "mget"]
        if r["status"] == CONFIRMED:
            if "accepted" not in outcomes and not any(
                    h["outcome"] == "confirmed" for h in mget_entries):
                err(f"{sid}: confirmed without item success or mget basis")
        elif r["status"] == SUPERSEDED:
            basis = [h for h in mget_entries if h["outcome"] == "superseded"]
            if not basis or not all(h["target_version"] > r["version"] for h in basis):
                err(f"{sid}: superseded without higher target version on mget")
            if "accepted" in outcomes:
                err(f"{sid}: superseded but a local accepted entry exists")
        elif r["status"] == CONFLICT:
            basis = [h for h in mget_entries if h["outcome"] == "conflict"]
            if not basis or not all(
                    h["target_version"] == r["version"]
                    and h["target_fingerprint"] != r["fingerprint"]
                    for h in basis):
                err(f"{sid}: conflict without same-version/different-fp mget basis")
        elif r["status"] == REJECTED:
            local = bool(r["reason"] and r["reason"].startswith("local_"))
            if not local and "mapping_error" not in outcomes:
                err(f"{sid}: rejected without 400 mapping basis")
            if "accepted" in outcomes:
                err(f"{sid}: rejected but a local accepted entry exists")
        elif r["status"] == UNRESOLVED:
            if "accepted" in outcomes:
                err(f"{sid}: unresolved despite an accepted entry")

        # Replays (s5 resubmits s1's exact body) share one submitted-body key,
        # so attempt counts are compared per body across all receipts. A
        # disconnected tail may make the server count lower than ours; it can
        # never be higher. Additionally, every dispatch that got a complete
        # (non-uncertain) bulk response must be present on the server.
        submitted_body = dict(r["document"])
        if r["fingerprint"]:
            submitted_body["ingest_fingerprint"] = r["fingerprint"]
        key = body_key(r["index"], r["id"], r["version"], submitted_body)
        local_counter[key] += len(bulk_entries)
        uncertain_counter[key] += sum(
            1 for h in bulk_entries if h["outcome"] == "transport_uncertain")
        checks.append(f"{sid}: local_bulk={len(bulk_entries)} "
                      f"status={r['status']}")

    for key in set(local_counter) | set(server_counter):
        local_n, server_n, lost_n = (local_counter.get(key, 0),
                                     server_counter.get(key, 0),
                                     uncertain_counter.get(key, 0))
        if server_n > local_n:
            err(f"{key[:2]} v{key[2]}: server saw {server_n} attempts but "
                f"local receipts only record {local_n}")
        if server_n < local_n - lost_n:
            err(f"{key[:2]} v{key[2]}: {local_n} completed dispatches claimed "
                f"but only {server_n} reached the server (uncertain tail {lost_n})")
    if len(server_attempts) > sum(local_counter.values()):
        err("server holds bulk attempts absent from every local receipt")

    # Current index must equal the final effective version per business id.
    expected_current: dict[tuple[str, str], dict[str, Any]] = {}
    for r in receipts:
        if r["status"] != CONFIRMED:
            continue
        cur = expected_current.get((r["index"], r["id"]))
        if cur is None or r["version"] > cur["version"]:
            expected_current[(r["index"], r["id"])] = r
    actual = {(d["index"], d["id"]): d for d in server_docs}
    for key, r in expected_current.items():
        d = actual.get(key)
        if d is None:
            err(f"{key}: confirmed content missing from current index")
            continue
        if d["version"] != r["version"]:
            err(f"{key}: current version {d['version']} != expected {r['version']}")
        if d["source"].get("ingest_fingerprint") != r["fingerprint"]:
            err(f"{key}: current fingerprint does not match final confirmed record")

    # Fingerprint invariant on everything the server currently holds and on
    # every entry of its write history.
    for d in server_docs + [
            {"index": w["index"], "id": w["id"], "version": w["version"],
             "source": w["source"]} for w in inspect["writes"]]:
        source = d["source"]
        stored = source.get("ingest_fingerprint")
        original = {k: v for k, v in source.items() if k != "ingest_fingerprint"}
        if stored != fingerprint_of(original):
            err(f"{(d['index'], d['id'])} v{d['version']}: "
                f"server-side ingest_fingerprint mismatch")

    return {
        "work_dir": str(work_dir),
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "server_attempt_count": len(server_attempts),
        "server_write_count": len(inspect["writes"]),
        "drop_done": inspect["drop_done"],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="run the bulk sink over a JSONL input")
    p_send.add_argument("--base-url", required=True)
    p_send.add_argument("--input", required=True)
    p_send.add_argument("--work-dir", required=True)
    p_send.add_argument("--batch-size", type=int, required=True)
    p_send.add_argument("--retry-interval", type=float, default=0.2)
    p_send.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT)

    p_status = sub.add_parser("status", help="summarise the final item statuses")
    p_status.add_argument("--work-dir", required=True)

    p_verify = sub.add_parser("verify", help="verify receipts against /__inspect")
    p_verify.add_argument("--base-url")
    p_verify.add_argument("--work-dir", required=True)
    p_verify.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT)

    args = parser.parse_args(argv)
    if args.command == "send":
        sink = BulkSink(args.base_url, args.work_dir, args.batch_size,
                        args.retry_interval, args.timeout)
        sink.load(args.input)
        summary = sink.send()
    elif args.command == "status":
        summary = status(args.work_dir)
    else:
        base_url = args.base_url
        if base_url is None:
            base_url = json.loads((pathlib.Path(args.work_dir) / "config.json")
                                  .read_text(encoding="utf-8"))["base_url"]
        report = verify(base_url, args.work_dir, args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
