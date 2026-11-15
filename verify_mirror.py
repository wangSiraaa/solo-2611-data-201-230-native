#!/usr/bin/env python3
"""
verify_mirror.py -- independent verification of a mirror run.

Everything is checked against two independent sources of truth:
  * fixtures/messages/*.eml  -- the raw message bytes, and
  * the mock server's own send logs (*.jsonl) -- what it actually put
    on the wire (sent_uid / expunged_uid / truncated_uid events).
The collector's own sync reports are not trusted here.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / "messages" / name).read_bytes()


def stages() -> dict:
    return json.loads((FIXTURES / "stages.json").read_text(encoding="utf-8"))


def stage_messages(stage: str) -> dict:
    """uid -> message spec as the stage defines it (before any expunge)."""
    return {m["uid"]: m for m in stages()[stage]["messages"]}


def stage_final_messages(stage: str) -> dict:
    """uid -> message spec after applying the stage's expunge."""
    msgs = stage_messages(stage)
    exp = stages()[stage].get("expunge_on_first_fetch")
    if exp is not None:
        msgs = {u: m for u, m in msgs.items() if u != exp}
    return msgs


def log_events(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_json(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class Checker:
    def __init__(self):
        self.failures = []
        self.passed = 0

    def check(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  PASS {label}")
        else:
            self.failures.append(f"{label}: {detail}")
            print(f"  FAIL {label}  {detail}")


# Which snapshot each stage must have published: stage -> (generation, snapshot_id)
STAGE_SNAPSHOT = {"initial": (900, 1), "shift": (900, 2), "reset": (901, 1)}


def verify_stage_snapshot(save_dir, logs_dir, stage, c: Checker):
    gen, snap_id = STAGE_SNAPSHOT[stage]
    gdir = Path(save_dir) / f"gen-{gen}"
    # Each stage is checked against the immutable snapshot it published,
    # not against current.json (which later stages of the same generation
    # legitimately advance).
    current = load_json(gdir / "snapshots" / f"snapshot-{snap_id:06d}.json")
    c.check(f"[{stage}] gen-{gen} snapshot #{snap_id} exists", current is not None)
    if current is None:
        return
    c.check(f"[{stage}] snapshot id is {snap_id}",
            current["snapshot_id"] == snap_id, f"got {current['snapshot_id']}")

    expected = stage_final_messages(stage)
    c.check(f"[{stage}] uid_set == {sorted(expected)}",
            sorted(current["uid_set"]) == sorted(expected),
            f"got {sorted(current['uid_set'])}")

    for uid, spec in expected.items():
        raw = fixture_bytes(spec["file"])
        entry = current["entries"].get(str(uid))
        c.check(f"[{stage}] uid {uid} has an entry", entry is not None)
        if not entry:
            continue
        c.check(f"[{stage}] uid {uid} digest == sha256({spec['file']})",
                entry["digest"] == sha256_hex(raw),
                f"{entry['digest']} != {sha256_hex(raw)}")
        c.check(f"[{stage}] uid {uid} size == {len(raw)} (literal byte count)",
                entry["size"] == len(raw), f"got {entry['size']}")
        c.check(f"[{stage}] uid {uid} flags == {sorted(spec['flags'])} (order-insensitive)",
                sorted(entry["flags"]) == sorted(spec["flags"]),
                f"got {entry['flags']}")
        archived = gdir / entry["original"]
        ok = archived.exists() and archived.read_bytes() == raw
        c.check(f"[{stage}] uid {uid} archived original is byte-exact "
                f"(no protocol ')' trailer, CRLF/non-ASCII preserved)", ok,
                f"{archived} differs from fixtures/messages/{spec['file']}")

    # The server log is the independent record of what went on the wire.
    events = log_events(Path(logs_dir) / f"{stage}.jsonl")
    sent = {e["sent_uid"]: e for e in events if "sent_uid" in e}
    c.check(f"[{stage}] server log sent UIDs == {sorted(expected)}",
            sorted(sent) == sorted(expected), f"got {sorted(sent)}")
    for uid, ev in sent.items():
        entry = current["entries"].get(str(uid), {})
        spec = expected[uid]
        c.check(f"[{stage}] log: uid {uid} sent {ev['bytes']} bytes == fixture+entry size",
                ev["bytes"] == len(fixture_bytes(spec["file"])) == entry.get("size"),
                f"log={ev['bytes']} entry={entry.get('size')}")


def verify_current_is_latest_snapshot(save_dir, c: Checker):
    """current.json of every generation must equal its highest snapshot."""
    for gdir in sorted(Path(save_dir).glob("gen-*")):
        snaps = sorted((gdir / "snapshots").glob("snapshot-*.json"))
        current = load_json(gdir / "current.json")
        latest = load_json(snaps[-1]) if snaps else None
        c.check(f"[global] {gdir.name} current.json == latest published snapshot",
                current is not None and current == latest)


def verify_shift_specifics(save_dir, logs_dir, c: Checker):
    gdir = Path(save_dir) / "gen-900"
    current = load_json(gdir / "current.json")
    # UID 11 was expunged after our SEARCH; it must be recorded vanished,
    # and its previously archived original must be retained.
    vanished = current["vanished"]
    c.check("[shift] UID 11 recorded vanished", "11" in vanished,
            f"vanished={sorted(vanished)}")
    if "11" in vanished:
        raw = fixture_bytes("11.eml")
        c.check("[shift] vanished UID 11 keeps digest of 11.eml",
                vanished["11"]["digest"] == sha256_hex(raw))
        archived = gdir / vanished["11"]["original"]
        c.check("[shift] vanished UID 11 original retained byte-exact",
                archived.exists() and archived.read_bytes() == raw)
    c.check("[shift] UID 11 not in current uid_set", 11 not in current["uid_set"])
    fc = {f["uid"]: f for f in current["flags_changes"]}
    c.check("[shift] flags change of UID 12 recorded (\\Seen -> \\Flagged \\Seen)",
            12 in fc and fc[12]["old"] == ["\\Seen"]
            and sorted(fc[12]["new"]) == ["\\Flagged", "\\Seen"],
            f"flags_changes={current['flags_changes']}")
    events = log_events(Path(logs_dir) / "shift.jsonl")
    exp = [e for e in events if "expunged_uid" in e]
    c.check("[shift] server log shows expunge of UID 11",
            len(exp) == 1 and exp[0]["expunged_uid"] == 11, f"events={exp}")


def verify_reset_specifics(save_dir, c: Checker):
    # UIDVALIDITY 900 -> 901: UID 11 in the new generation is a different
    # message (11-new.eml); the old generation must be untouched history.
    new = load_json(Path(save_dir) / "gen-901" / "current.json")
    c.check("[reset] gen-901 published with UID 11", new is not None
            and sorted(new["uid_set"]) == [11])
    if new:
        raw = fixture_bytes("11-new.eml")
        entry = new["entries"].get("11", {})
        c.check("[reset] gen-901 UID 11 is the new message (11-new.eml)",
                entry.get("digest") == sha256_hex(raw)
                and (Path(save_dir) / "gen-901" / entry["original"]).read_bytes() == raw)
    old = load_json(Path(save_dir) / "gen-900" / "current.json")
    c.check("[reset] gen-900 history untouched (still snapshot #2, UIDs 12/13 alive)",
            old is not None and old["snapshot_id"] == 2
            and sorted(old["uid_set"]) == [12, 13] and "11" in old["vanished"])
    old11 = Path(save_dir) / "gen-900" / "originals" / "11.eml"
    c.check("[reset] gen-900 UID 11 original still the old message (11.eml)",
            old11.exists() and old11.read_bytes() == fixture_bytes("11.eml"))


def verify_truncated(save_dir, logs_dir, c: Checker):
    # The truncated stage must not have damaged the previously published
    # snapshot; the failure must be recorded; nothing may be half-published.
    gdir = Path(save_dir) / "gen-900"
    current = load_json(gdir / "current.json")
    c.check("[truncated] gen-900 current.json still snapshot #2",
            current is not None and current["snapshot_id"] == 2,
            f"got {current and current['snapshot_id']}")
    snaps = sorted((gdir / "snapshots").glob("snapshot-*.json"))
    c.check("[truncated] no half-published snapshot appeared", len(snaps) == 2,
            f"found {[p.name for p in snaps]}")
    state = load_json(gdir / "state.json")
    c.check("[truncated] gen-900 state not advanced", state["snapshot_seq"] == 2)
    staged = list((gdir / "staged").glob("*.eml")) if (gdir / "staged").exists() else []
    c.check("[truncated] nothing staged from the cut-off literal "
            "(fetch died before any UID completed)", staged == [],
            f"staged={[p.name for p in staged]}")
    for uid, name in ((11, "11.eml"), (12, "12.eml"), (13, "13.eml")):
        p = gdir / "originals" / f"{uid}.eml"
        c.check(f"[truncated] archived uid {uid} original still byte-exact",
                p.exists() and p.read_bytes() == fixture_bytes(name))
    events = log_events(Path(logs_dir) / "truncated.jsonl")
    c.check("[truncated] server log shows truncation, no complete send",
            any("truncated_uid" in e for e in events)
            and not any("sent_uid" in e for e in events))
    sync_log = [json.loads(l) for l in
                Path(save_dir, "sync-log.jsonl").read_text().splitlines()]
    failed = [r for r in sync_log if r["error"]]
    c.check("[truncated] failed sync recorded with error", len(failed) >= 1)
    if failed:
        last = failed[-1]
        c.check("[truncated] failure was a cut connection (abort/EOF)",
                "abort" in last["error"]["type"] or "EOF" in last["error"]["message"],
                f"error={last['error']}")
        c.check("[truncated] limited retries visible (3 attempts, then gave up)",
                len(last["attempts"]) == 3, f"attempts={last['attempts']}")
        c.check("[truncated] failed sync published nothing",
                last["published"] is None)
    root = load_json(Path(save_dir) / "mirror.json")
    c.check("[truncated] root still points at last good generation 901",
            root["current_generation"] == 901)
    c.check("[truncated] root records the last error", root["last_error"] is not None)


def verify_export(save_dir, export_root, c: Checker):
    for gen, expect in ((901, {11: "11-new.eml"}), (900, {12: "12.eml", 13: "13.eml"})):
        out = Path(export_root) / f"gen-{gen}"
        manifest = load_json(out / "manifest.json")
        c.check(f"[export gen-{gen}] manifest exists", manifest is not None)
        if not manifest:
            continue
        c.check(f"[export gen-{gen}] manifest identity uidvalidity == {gen}",
                manifest["identity"]["uidvalidity"] == gen)
        c.check(f"[export gen-{gen}] exported uid set == {sorted(expect)}",
                sorted(int(u) for u in manifest["entries"]) == sorted(expect))
        for uid, name in expect.items():
            raw = fixture_bytes(name)
            exported = out / "messages" / f"{uid}.eml"
            c.check(f"[export gen-{gen}] messages/{uid}.eml byte-exact",
                    exported.exists() and exported.read_bytes() == raw)
            c.check(f"[export gen-{gen}] manifest digest matches exported bytes",
                    manifest["entries"][str(uid)]["digest"] == sha256_hex(raw))
    m900 = load_json(Path(export_root) / "gen-900" / "manifest.json")
    if m900:
        c.check("[export gen-900] vanished UID 11 recorded but not exported as alive",
                "11" in m900["vanished"]
                and not (Path(export_root) / "gen-900" / "messages" / "11.eml").exists())


def verify_no_fabrication(save_dir, c: Checker):
    """Every archived original anywhere in the mirror must be byte-identical
    to one of the fixture messages -- nothing may be invented or emptied."""
    known = {sha256_hex(fixture_bytes(p.name))
             for p in (FIXTURES / "messages").glob("*.eml")}
    originals = sorted(Path(save_dir).glob("gen-*/originals/*.eml"))
    c.check("[global] originals exist", len(originals) > 0)
    for p in originals:
        c.check(f"[global] {p.relative_to(save_dir)} matches a real fixture message",
                sha256_hex(p.read_bytes()) in known,
                f"digest {sha256_hex(p.read_bytes())} unknown")
    staged = sorted(Path(save_dir).glob("gen-*/staged/*.eml"))
    for p in staged:
        c.check(f"[global] staged {p.relative_to(save_dir)} matches a real fixture message",
                sha256_hex(p.read_bytes()) in known)


def verify_all(save_dir, logs_dir, export_root=None) -> list:
    c = Checker()
    print("== initial stage ==")
    verify_stage_snapshot(save_dir, logs_dir, "initial", c)
    print("== shift stage (expunge + sequence shift) ==")
    verify_stage_snapshot(save_dir, logs_dir, "shift", c)
    verify_shift_specifics(save_dir, logs_dir, c)
    print("== reset stage (UIDVALIDITY change) ==")
    verify_stage_snapshot(save_dir, logs_dir, "reset", c)
    verify_reset_specifics(save_dir, c)
    print("== truncated stage (cut literal) ==")
    verify_truncated(save_dir, logs_dir, c)
    print("== no fabricated content ==")
    verify_no_fabrication(save_dir, c)
    verify_current_is_latest_snapshot(save_dir, c)
    if export_root is not None:
        print("== exports ==")
        verify_export(save_dir, export_root, c)
    print(f"\n{ c.passed } checks passed, {len(c.failures)} failed")
    return c.failures


if __name__ == "__main__":
    save_dir = sys.argv[1] if len(sys.argv) > 1 else "run/mirror"
    logs_dir = sys.argv[2] if len(sys.argv) > 2 else "run/logs"
    export_root = sys.argv[3] if len(sys.argv) > 3 else "run/export"
    failures = verify_all(save_dir, logs_dir, export_root)
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
    sys.exit(1 if failures else 0)
