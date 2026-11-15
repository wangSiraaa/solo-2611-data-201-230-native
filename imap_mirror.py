#!/usr/bin/env python3
"""
imap_mirror.py -- read-only IMAP mailbox mirror collector (Python 3.11).

Persistent message identity is (account, mailbox, UIDVALIDITY, UID).
Message sequence numbers are interpreted only inside the connection that
reported them and are never persisted as identity.

Protocol surface is restricted to: LOGIN, EXAMINE, UID SEARCH, UID FETCH,
NOOP, LOGOUT (see ReadOnlySession). Fetch uses BODY.PEEK[]; nothing is
ever stored, flagged, expunged or deleted on the server. Message bodies
are treated as opaque byte strings (no MIME parsing).

A generation (one UIDVALIDITY domain) is published as a snapshot only
after the full current UID set -- re-checked with a second UID SEARCH
after fetching -- has been matched with byte-exact originals. A sync
that hits a truncated literal, a FETCH response without UID, or an
unconfirmable UID publishes nothing: the previous snapshot stays
current, the error is recorded, and any fetched originals are staged
for later verification. A UIDVALIDITY change starts a new generation;
old generations are kept as read-only history.
"""
from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_SURFACE = ("LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "NOOP", "LOGOUT")
FETCH_BATCH = 50  # UIDs per UID FETCH command; small-data collector


class ProtocolError(Exception):
    """The server broke the protocol rules this collector relies on
    (e.g. a FETCH response without an explicit UID, or a literal whose
    byte count does not match its declared size)."""


class UnconfirmedSet(Exception):
    """Part of the re-checked current UID set has no verified original."""

    def __init__(self, uids):
        self.uids = [int(u) for u in uids]
        super().__init__(f"unconfirmed UIDs (no verified original): {self.uids}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------- FETCH parsing

@dataclass
class FetchResult:
    """One message, bound to its explicit UID. `seq` is the transient
    message sequence number of this connection only -- never persisted."""
    uid: int
    seq: int
    flags: tuple
    body: bytes

    @property
    def digest(self) -> str:
        return sha256_hex(self.body)

    @property
    def size(self) -> int:
        return len(self.body)


_RE_UID = re.compile(rb"\bUID (\d+)\b")
_RE_FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
_RE_LITERAL = re.compile(rb"\{(\d+)\}$")
_RE_SEQ = re.compile(rb"^(\d+) ")


def parse_fetch_data(data) -> dict:
    """Parse imaplib's UID FETCH response list into {uid: FetchResult}.

    Every response must carry an explicit UID; the literal must match its
    declared byte length. The protocol trailer (the closing parenthesis
    item imaplib yields after each literal) is validated and discarded --
    it is never stored with the message.
    """
    results = {}
    for item in data or []:
        if item is None:
            continue  # imaplib yields [None] when there were no FETCH responses
        if isinstance(item, tuple):
            header, literal = item
            if not isinstance(header, bytes) or not isinstance(literal, bytes):
                raise ProtocolError(f"malformed FETCH item: {item!r}")
            m = _RE_UID.search(header)
            if not m:
                raise ProtocolError(f"FETCH response without UID: {header!r}")
            uid = int(m.group(1))
            seq_m = _RE_SEQ.match(header)
            seq = int(seq_m.group(1)) if seq_m else 0
            flags_m = _RE_FLAGS.search(header)
            flags = tuple(sorted(
                f.decode("ascii") for f in (flags_m.group(1).split() if flags_m else [])
            ))
            lit_m = _RE_LITERAL.search(header)
            if lit_m and len(literal) != int(lit_m.group(1)):
                raise ProtocolError(
                    f"literal truncated: declared {int(lit_m.group(1))} bytes, "
                    f"got {len(literal)} (UID {uid})"
                )
            if uid in results:
                raise ProtocolError(f"duplicate FETCH response for UID {uid}")
            results[uid] = FetchResult(uid=uid, seq=seq, flags=flags, body=literal)
        elif isinstance(item, bytes):
            if item.strip() != b")":
                raise ProtocolError(f"unexpected FETCH response item: {item!r}")
            # else: protocol trailer of a literal response -- not message data
        else:
            raise ProtocolError(f"unexpected FETCH response item type: {type(item).__name__}")
    return results


# ---------------------------------------------------------------- IMAP session

class ReadOnlySession:
    """IMAP session restricted to LOGIN, EXAMINE, UID SEARCH, UID FETCH,
    NOOP and LOGOUT. Nothing else is ever sent to the server."""

    def __init__(self, host, port, user, password, mailbox, timeout=10.0):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.mailbox = mailbox
        self.timeout = timeout
        self._imap = None
        self.uidvalidity = None
        self.expunged_seq_transient = []  # diagnostic only, never identity

    def __enter__(self):
        imap = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        self._imap = imap
        typ, _ = imap.login(self.user, self.password)
        if typ != "OK":
            raise ProtocolError("LOGIN was not accepted")
        typ, _ = imap.select(self.mailbox, readonly=True)  # EXAMINE
        if typ != "OK":
            raise ProtocolError(f"EXAMINE {self.mailbox} was not accepted")
        uv = imap.untagged_responses.get("UIDVALIDITY")
        if not uv or uv[0] is None:
            raise ProtocolError("EXAMINE did not yield UIDVALIDITY; "
                                "cannot establish the UID domain identity")
        self.uidvalidity = int(uv[0])
        return self

    def uid_search_all(self) -> list:
        typ, data = self._imap.uid("SEARCH", "ALL")
        if typ != "OK":
            raise ProtocolError(f"UID SEARCH ALL failed: {typ}")
        uids = []
        for chunk in data or []:
            if chunk:
                uids.extend(int(tok) for tok in chunk.split())
        return uids

    def uid_fetch_batch(self, uids) -> dict:
        """UID FETCH (UID FLAGS BODY.PEEK[]) for a set of UIDs. Responses
        are bound by their explicit UID, never by sequence position."""
        if not uids:
            return {}
        uid_list = ",".join(str(u) for u in uids)
        typ, data = self._imap.uid("FETCH", uid_list, "(UID FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise ProtocolError(f"UID FETCH {uid_list} failed: {typ}")
        parsed = parse_fetch_data(data)
        extra = sorted(set(parsed) - set(uids))
        if extra:
            raise ProtocolError(f"FETCH returned UIDs outside the requested set: {extra}")
        self._collect_expunged()
        return parsed

    def noop(self):
        typ, _ = self._imap.noop()
        if typ != "OK":
            raise ProtocolError("NOOP failed")

    def _collect_expunged(self):
        exp = self._imap.untagged_responses.pop("EXPUNGE", [])
        self.expunged_seq_transient.extend(int(x) for x in exp if x)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._imap is not None:
                self._imap.logout()
        except Exception:
            pass  # LOGOUT on a broken connection is best-effort
        return False


# ---------------------------------------------------------------- storage

class Store:
    """Layout under the save directory:

        mirror.json                     root identity + generation pointers
        sync-log.jsonl                  one report per sync run
        gen-<uidvalidity>/state.json    confirmed working state of the generation
        gen-<uidvalidity>/current.json  latest published snapshot (readable manifest)
        gen-<uidvalidity>/snapshots/    immutable published snapshots
        gen-<uidvalidity>/originals/    byte-exact archived originals
        gen-<uidvalidity>/staged/       originals fetched by a failed sync, kept
                                        for later verification (never published)
    """

    def __init__(self, root):
        self.root = Path(root)

    def load_root(self):
        return load_json(self.root / "mirror.json")

    def save_root(self, obj):
        write_json_atomic(self.root / "mirror.json", obj)

    def append_sync_log(self, report):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "sync-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")

    def gen_dir(self, gen) -> Path:
        return self.root / f"gen-{gen}"

    def load_state(self, gen):
        return load_json(self.gen_dir(gen) / "state.json")

    def save_state(self, gen, state):
        write_json_atomic(self.gen_dir(gen) / "state.json", state)

    def load_current(self, gen):
        return load_json(self.gen_dir(gen) / "current.json")

    def write_original(self, gen, uid, body):
        """Archive an original byte-exact. An existing identical file is
        only read, never rewritten. A digest conflict is kept alongside
        (never overwritten) and reported as an anomaly."""
        digest = sha256_hex(body)
        path = self.gen_dir(gen) / "originals" / f"{uid}.eml"
        if path.exists():
            if sha256_hex(path.read_bytes()) == digest:
                return f"originals/{uid}.eml", None
            alt = f"{uid}.conflict-{digest.split(':')[1][:12]}.eml"
            write_bytes_atomic(self.gen_dir(gen) / "originals" / alt, body)
            return f"originals/{alt}", {
                "type": "content_changed_within_generation",
                "uid": uid, "new_digest": digest,
            }
        write_bytes_atomic(path, body)
        return f"originals/{uid}.eml", None

    def publish(self, gen, state, rec, search1, search2, now):
        """Write originals, snapshot, current manifest and state. This is
        the only place that advances the published view of a generation."""
        for uid, body in rec["new_originals"].items():
            rel, anomaly = self.write_original(gen, uid, body)
            rec["new_uids"][str(uid)]["original"] = rel
            if anomaly:
                rec["anomalies"].append(anomaly)
        seq = state["snapshot_seq"] + 1
        entries, vanished = {}, {}
        for key, e in rec["new_uids"].items():
            if e["status"] == "alive":
                entries[key] = {"digest": e["digest"], "size": e["size"],
                                "flags": e["flags"], "original": e["original"]}
            else:
                vanished[key] = {"digest": e["digest"], "size": e["size"],
                                 "original": e["original"], "last_flags": e["flags"],
                                 "vanished_at": e.get("vanished_at")}
        snapshot = {
            "kind": "imap-mirror-snapshot",
            "identity": state["identity"],
            "snapshot_id": seq,
            "published_at": now,
            "uid_set": list(search2),
            "search_evidence": {"first": list(search1), "recheck": list(search2)},
            "entries": entries,
            "vanished": vanished,
            "flags_changes": rec["flags_changes"],
            "returned": rec["returned"],
            "anomalies": rec["anomalies"],
        }
        write_json_atomic(self.gen_dir(gen) / "snapshots" / f"snapshot-{seq:06d}.json", snapshot)
        write_json_atomic(self.gen_dir(gen) / "current.json", snapshot)
        state["uids"] = rec["new_uids"]
        state["snapshot_seq"] = seq
        state["updated_at"] = now
        self.save_state(gen, state)
        return snapshot

    def stage_results(self, gen, results, reason):
        """Keep originals fetched by a failed sync for later verification.
        Staged content is never referenced by a published snapshot."""
        staged = []
        d = self.gen_dir(gen) / "staged"
        for uid, r in results.items():
            write_bytes_atomic(d / f"{uid}.eml", r.body)
            write_json_atomic(d / f"{uid}.json", {
                "uid": uid, "digest": r.digest, "size": r.size,
                "flags": list(r.flags), "staged_at": utcnow(), "reason": reason,
            })
            staged.append(uid)
        return staged

    def reconcile_staged(self, gen, results):
        """After a successful sync: staged files whose digest matches the
        freshly fetched original are confirmed and dropped; anything else
        stays staged and is reported."""
        confirmed, remaining = [], []
        d = self.gen_dir(gen) / "staged"
        if not d.exists():
            return confirmed, remaining
        for meta_path in sorted(d.glob("*.json")):
            meta = load_json(meta_path)
            uid = meta["uid"]
            eml = d / f"{uid}.eml"
            ok = (uid in results and results[uid].digest == meta["digest"]
                  and eml.exists() and sha256_hex(eml.read_bytes()) == meta["digest"])
            if ok:
                eml.unlink()
                meta_path.unlink()
                confirmed.append(uid)
            else:
                remaining.append(uid)
        return confirmed, remaining


# ---------------------------------------------------------------- reconcile

def reconcile(state, search1, search2, results, now):
    """Pure reconciliation of a finished network phase.

    Raises UnconfirmedSet if any UID of the re-checked current set lacks
    a verified original -- in that case nothing may be published.
    """
    final = list(search2)
    unconfirmed = [u for u in final if u not in results]
    if unconfirmed:
        raise UnconfirmedSet(unconfirmed)

    old = state["uids"]
    new_uids, new_originals = {}, {}
    flags_changes, vanished, returned, anomalies = [], [], [], []

    for uid in final:
        r = results[uid]
        key = str(uid)
        prev = old.get(key)
        digest = r.digest
        if prev and prev["digest"] == digest:
            original = prev["original"]  # reuse archived original read-only
            first_seen = prev["first_seen"]
        else:
            original = None  # written at publish time
            new_originals[uid] = r.body
            first_seen = prev["first_seen"] if prev else now
            if prev:
                anomalies.append({"type": "content_changed_within_generation",
                                  "uid": uid, "old_digest": prev["digest"],
                                  "new_digest": digest})
        if prev:
            if set(prev["flags"]) != set(r.flags):  # flag order is insignificant
                flags_changes.append({"uid": uid, "old": sorted(prev["flags"]),
                                      "new": sorted(r.flags)})
            if prev["status"] == "vanished":
                returned.append({"uid": uid})
        new_uids[key] = {"status": "alive", "digest": digest, "size": r.size,
                         "flags": sorted(r.flags), "original": original,
                         "first_seen": first_seen, "last_confirmed": now}

    # Fetched but already gone at the recheck: archive as evidence, mark vanished.
    for uid, r in results.items():
        key = str(uid)
        if uid in final or key in new_uids or key in old:
            continue
        new_originals[uid] = r.body
        new_uids[key] = {"status": "vanished", "digest": r.digest, "size": r.size,
                         "flags": sorted(r.flags), "original": None,
                         "first_seen": now, "last_confirmed": now, "vanished_at": now}
        vanished.append({"uid": uid, "archived": True,
                         "note": "vanished between search and recheck"})

    # Retain history; UIDs that disappeared are recorded, originals are kept.
    for key, prev in old.items():
        if key in new_uids:
            continue
        entry = dict(prev)
        if prev["status"] == "alive":
            entry["status"] = "vanished"
            entry["vanished_at"] = now
            vanished.append({"uid": int(key), "archived": True})
        new_uids[key] = entry

    dropped_unarchived = [u for u in search1
                          if u not in final and str(u) not in old and u not in results]
    return {"new_uids": new_uids, "new_originals": new_originals,
            "flags_changes": flags_changes, "vanished": vanished,
            "returned": returned, "anomalies": anomalies,
            "dropped_unarchived": dropped_unarchived}


# ---------------------------------------------------------------- sync

def run_sync(args) -> dict:
    store = Store(args.save_dir)
    now = utcnow()
    base_identity = {"account": args.user, "server": f"{args.host}:{args.port}",
                     "mailbox": args.mailbox}
    root = store.load_root()
    if root is None:
        root = {"kind": "imap-mirror", **base_identity, "created_at": now,
                "generations": {}, "current_generation": None,
                "last_sync_at": None, "last_sync_result": None, "last_error": None}
    elif root["account"] != args.user or root["mailbox"] != args.mailbox:
        raise SystemExit(
            f"save dir {args.save_dir} belongs to {root['account']}/{root['mailbox']}, "
            f"not {args.user}/{args.mailbox}")

    report = {"kind": "sync", "started_at": now, "identity": dict(base_identity),
              "attempts": [], "search_first": None, "search_recheck": None,
              "expunged_seq_transient": [], "fetched": [],
              "flags_changes": [], "vanished": [], "returned": [],
              "dropped_unarchived": [], "unconfirmed": [],
              "staged": [], "staged_confirmed": [], "staged_remaining": [],
              "anomalies": [], "published": None, "error": None}

    uidvalidity, search1, search2 = None, None, None
    results = {}
    net_error = None
    for attempt in range(1, args.retries + 2):
        results = {}
        try:
            with ReadOnlySession(args.host, args.port, args.user, args.password,
                                 args.mailbox, timeout=args.timeout) as s:
                uidvalidity = s.uidvalidity
                search1 = s.uid_search_all()
                for batch in chunks(search1, FETCH_BATCH):
                    results.update(s.uid_fetch_batch(batch))
                s.noop()
                # Re-check the current UID set after collecting; the server
                # may have deleted or delivered messages during the fetch.
                search2 = s.uid_search_all()
                late = [u for u in search2 if u not in results]
                for batch in chunks(late, FETCH_BATCH):
                    results.update(s.uid_fetch_batch(batch))
                s._collect_expunged()
                report["expunged_seq_transient"] = s.expunged_seq_transient
            net_error = None
            break
        except (ProtocolError, imaplib.IMAP4.abort, OSError, EOFError) as e:
            net_error = e
            report["attempts"].append(
                {"attempt": attempt, "error": f"{type(e).__name__}: {e}"})
            if attempt <= args.retries:
                time.sleep(min(0.25 * attempt, 1.0))

    report["identity"]["uidvalidity"] = uidvalidity
    report["search_first"] = search1
    report["search_recheck"] = search2
    report["fetched"] = [
        {"uid": r.uid, "seq_transient": r.seq, "size": r.size,
         "digest": r.digest, "flags": list(r.flags)}
        for r in (results[u] for u in sorted(results))
    ]

    def fail(err_type, message, gen):
        if gen is not None and results:
            report["staged"] = store.stage_results(gen, results, reason=message)
        report["error"] = {"type": err_type, "message": message}
        root["last_sync_at"] = utcnow()
        root["last_sync_result"] = "failed"
        root["last_error"] = {"at": report["finished_at"], "type": err_type,
                              "message": message, "uidvalidity": gen}
        store.save_root(root)
        store.append_sync_log(report)
        return report

    report["finished_at"] = utcnow()
    if net_error is not None:
        return fail(type(net_error).__name__, str(net_error), uidvalidity)

    gen = uidvalidity
    identity = {**base_identity, "uidvalidity": gen}
    state = store.load_state(gen) or {
        "kind": "imap-mirror-generation", "identity": identity,
        "created_at": now, "snapshot_seq": 0, "updated_at": None, "uids": {},
    }
    try:
        rec = reconcile(state, search1, search2, results, now)
    except UnconfirmedSet as e:
        report["unconfirmed"] = e.uids
        return fail("UnconfirmedSet", str(e), gen)

    snapshot = store.publish(gen, state, rec, search1, search2, now)
    confirmed, remaining = store.reconcile_staged(gen, results)
    report.update({
        "flags_changes": rec["flags_changes"],
        "vanished": rec["vanished"],
        "returned": rec["returned"],
        "dropped_unarchived": rec["dropped_unarchived"],
        "anomalies": rec["anomalies"],
        "staged_confirmed": confirmed,
        "staged_remaining": remaining,
        "published": {"generation": gen, "snapshot_id": snapshot["snapshot_id"]},
    })
    gen_info = root["generations"].get(str(gen), {"created_at": now})
    gen_info["snapshots"] = snapshot["snapshot_id"]
    gen_info["updated_at"] = now
    root["generations"][str(gen)] = gen_info
    root["current_generation"] = gen
    root["last_sync_at"] = report["finished_at"]
    root["last_sync_result"] = "published"
    root["last_error"] = None
    store.save_root(root)
    store.append_sync_log(report)
    return report


# ---------------------------------------------------------------- status / export

def build_status(store) -> dict:
    root = store.load_root()
    if root is None:
        raise SystemExit(f"no mirror found at {store.root}")
    generations = {}
    for gen_s in sorted(root["generations"], key=int):
        gen = int(gen_s)
        state = store.load_state(gen) or {}
        current = store.load_current(gen)
        staged_dir = store.gen_dir(gen) / "staged"
        staged = sorted(int(p.stem) for p in staged_dir.glob("*.eml")) if staged_dir.exists() else []
        generations[gen_s] = {
            "identity": (state or {}).get("identity"),
            "snapshot_seq": (state or {}).get("snapshot_seq", 0),
            "published_snapshot": (current or {}).get("snapshot_id"),
            "published_at": (current or {}).get("published_at"),
            "current_uids": (current or {}).get("uid_set"),
            "flags_changes": (current or {}).get("flags_changes", []),
            "vanished": (current or {}).get("vanished", {}),
            "staged_unconfirmed": staged,
        }
    return {"kind": "imap-mirror-status",
            "identity": {"account": root["account"], "server": root["server"],
                         "mailbox": root["mailbox"]},
            "current_generation": root["current_generation"],
            "generations": generations,
            "last_sync_at": root["last_sync_at"],
            "last_sync_result": root["last_sync_result"],
            "last_error": root["last_error"]}


def cmd_status(args) -> int:
    status = build_status(Store(args.save_dir))
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    ident = status["identity"]
    print(f"Domain:     {ident['account']} @ {ident['server']}  mailbox {ident['mailbox']}")
    print(f"Generations: {', '.join(status['generations']) or '(none)'}  "
          f"(current: {status['current_generation']})")
    for gen_s, g in status["generations"].items():
        print(f"\n[gen {gen_s}] published snapshot #{g['published_snapshot']} "
              f"at {g['published_at']}")
        for uid in g["current_uids"] or []:
            cur = Store(args.save_dir).load_current(int(gen_s))
            e = cur["entries"][str(uid)]
            print(f"  alive  UID {uid:<4} flags={e['flags']} size={e['size']} {e['digest']}")
        for uid, v in (g["vanished"] or {}).items():
            print(f"  gone   UID {uid:<4} vanished_at={v['vanished_at']} "
                  f"archived={v['original']} {v['digest']}")
        for fc in g["flags_changes"]:
            print(f"  flags  UID {fc['uid']}: {fc['old']} -> {fc['new']}")
        if g["staged_unconfirmed"]:
            print(f"  staged/unconfirmed UIDs: {g['staged_unconfirmed']}")
    print(f"\nLast sync: {status['last_sync_result']} at {status['last_sync_at']}")
    if status["last_error"]:
        le = status["last_error"]
        print(f"Last error: {le['type']}: {le['message']} (uidvalidity {le['uidvalidity']})")
    return 0


def cmd_export(args) -> int:
    store = Store(args.save_dir)
    root = store.load_root()
    if root is None:
        raise SystemExit(f"no mirror found at {args.save_dir}")
    gen = args.generation if args.generation is not None else root["current_generation"]
    if gen is None:
        raise SystemExit("no generation has been published yet")
    current = store.load_current(gen)
    if current is None:
        raise SystemExit(f"generation {gen} has no published snapshot")
    out = Path(args.out)
    manifest_path = out / "manifest.json"
    if out.exists():
        old = load_json(manifest_path)
        if any(out.iterdir()) and (not old or old.get("kind") != "imap-mirror-export"):
            raise SystemExit(f"export dir {out} exists and was not written by us; refusing")
        for p in sorted((out / "messages").glob("*")) if (out / "messages").exists() else []:
            p.unlink()
    (out / "messages").mkdir(parents=True, exist_ok=True)
    entries = {}
    for uid_s, e in current["entries"].items():
        src = store.gen_dir(gen) / e["original"]
        dst = out / "messages" / f"{uid_s}.eml"
        dst.write_bytes(src.read_bytes())  # byte-exact copy
        entries[uid_s] = {"file": f"messages/{uid_s}.eml", "digest": e["digest"],
                          "size": e["size"], "flags": e["flags"]}
    manifest = {"kind": "imap-mirror-export", "exported_at": utcnow(),
                "identity": current["identity"], "snapshot_id": current["snapshot_id"],
                "published_at": current["published_at"],
                "uid_set": current["uid_set"], "entries": entries,
                "vanished": current["vanished"],
                "flags_changes": current["flags_changes"],
                "anomalies": current["anomalies"]}
    write_json_atomic(manifest_path, manifest)
    print(json.dumps({"exported": str(out), "generation": gen,
                      "snapshot_id": current["snapshot_id"],
                      "messages": sorted(int(u) for u in entries)},
                     ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only IMAP mailbox mirror collector. "
                    "Protocol surface: " + ", ".join(PROTOCOL_SURFACE))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--save-dir", required=True, help="mirror save directory")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=11430)
        p.add_argument("--user", default="demo")
        p.add_argument("--password", default="demo")
        p.add_argument("--mailbox", default="INBOX")

    p_sync = sub.add_parser("sync", help="run one read-only mirror sync")
    common(p_sync)
    p_sync.add_argument("--retries", type=int, default=2,
                        help="extra attempts after the first failure (default 2)")
    p_sync.add_argument("--timeout", type=float, default=10.0)

    p_status = sub.add_parser("status", help="show mirror status")
    common(p_status)
    p_status.add_argument("--json", action="store_true")

    p_export = sub.add_parser("export", help="export a published snapshot")
    common(p_export)
    p_export.add_argument("--out", required=True, help="export directory")
    p_export.add_argument("--generation", type=int, default=None,
                          help="uidvalidity generation (default: current)")

    args = ap.parse_args(argv)
    if args.cmd == "sync":
        report = run_sync(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["published"] else 3
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "export":
        return cmd_export(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
