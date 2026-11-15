"""SQLite durable state machine for the QoS2 receiver.

Durability design (one row per (generation, packet identifier), no permanent
uniqueness on the packet identifier and no payload-hash de-duplication):

  deliveries.state transitions
    received   -- PUBLISH persisted row, PUBREC may now be sent (or retried)
    committed  -- PUBREL processed inside a transaction that also inserts the
                  business record; PUBCOMP must be delivered/redelivered
    completed  -- PUBCOMP has actually been written to the wire; the packet
                  identifier is free to carry a brand new delivery, which gets
                  a brand new row even when the payload bytes are identical.
    abandoned  -- the broker reopened the session without Session Present; the
                  old generation's unconfirmed handshakes are ended explicitly.
                  Completed business rows of the old generation are never touched.

A *partial* unique index protects only *unfinished* rows, which is exactly the
MQTT rule "one in-flight delivery per packet identifier per session". Completed
rows stay forever and a reused identifier inserts a new row (new delivery_id).

Every acknowledgement (PUBREC/PUBCOMP) is sent only after the corresponding
SQLite transaction has committed, so a crash before the commit simply causes a
protocol redelivery instead of a lost or double business record.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Delivery lifecycle states (see module docstring).
STATE_RECEIVED = "received"
STATE_COMMITTED = "committed"
# 'completing' is the durable "PUBCOMP in flight" marker: it is committed
# BEFORE the PUBCOMP bytes are written, so a crash leaves a row that simply
# retransmits PUBCOMP instead of reopening or duplicating the delivery.
STATE_COMPLETING = "completing"
STATE_COMPLETED = "completed"
STATE_ABANDONED = "abandoned"
OPEN_STATES = (STATE_RECEIVED, STATE_COMMITTED, STATE_COMPLETING)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    generation        INTEGER PRIMARY KEY,
    client_id         TEXT NOT NULL,
    topic             TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    end_reason        TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    generation        INTEGER NOT NULL REFERENCES sessions(generation),
    packet_id         INTEGER NOT NULL CHECK (packet_id BETWEEN 1 AND 65535),
    topic             TEXT NOT NULL,
    payload           BLOB NOT NULL,
    payload_sha256    TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (
                          state IN ('received','committed','completing',
                                    'completed','abandoned')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- Protocol-level uniqueness: only while the handshake is unfinished.
-- No UNIQUE(packet_id) alone, no UNIQUE(payload_sha256): neither can express
-- protocol state, and both would forbid legitimate packet-id reuse.
CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_open
    ON deliveries(generation, packet_id)
    WHERE state IN ('received','committed','completing');

CREATE TABLE IF NOT EXISTS business_records (
    business_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id       INTEGER NOT NULL UNIQUE REFERENCES deliveries(delivery_id),
    generation        INTEGER NOT NULL,
    packet_id         INTEGER NOT NULL,
    payload           BLOB NOT NULL,
    payload_sha256    TEXT NOT NULL,
    committed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protocol_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    generation        INTEGER,
    conn_ordinal      INTEGER,
    packet_id         INTEGER,
    direction         TEXT NOT NULL,
    packet_type       TEXT NOT NULL,
    event             TEXT NOT NULL,
    delivery_id       INTEGER,
    business_seq      INTEGER,
    state_from        TEXT,
    state_to          TEXT,
    detail            TEXT
);
"""

# MQTT 3.1.1 control packet type numbers (MQTT-2.2.1).
PACKET_TYPE_NAME = {
    1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 5: "PUBREC",
    6: "PUBREL", 7: "PUBCOMP", 8: "SUBSCRIBE", 9: "SUBACK",
    10: "UNSUBSCRIBE", 11: "UNSUBACK", 12: "PINGREQ", 13: "PINGRESP",
    14: "DISCONNECT",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Store:
    """All SQLite access for the receiver; one short transaction per protocol step."""

    def __init__(self, path: str | Path, client_id: str, topic: str):
        self.path = str(path)
        self.client_id = client_id
        self.topic = topic
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ events
    def event(self, *, direction: str, packet_type: str, event: str,
              generation: int | None = None, conn_ordinal: int | None = None,
              packet_id: int | None = None, delivery_id: int | None = None,
              business_seq: int | None = None, state_from: str | None = None,
              state_to: str | None = None, detail: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO protocol_events
               (ts, generation, conn_ordinal, packet_id, direction, packet_type,
                event, delivery_id, business_seq, state_from, state_to, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (utcnow(), generation, conn_ordinal, packet_id, direction, packet_type,
             event, delivery_id, business_seq, state_from, state_to, detail),
        )
        self.conn.commit()

    # ------------------------------------------------------------- generations
    def latest_generation(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(generation) AS g FROM sessions").fetchone()
        return row["g"]

    def begin_generation(self, conn_ordinal: int) -> int:
        """CONNACK Session Present = false: start a fresh generation.

        Any unconfirmed handshakes of the previous generation are explicitly
        ended as 'abandoned'; completed business records are preserved.
        """
        gen = (self.latest_generation() or 0) + 1
        prev = gen - 1
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO sessions (generation, client_id, topic, started_at) "
            "VALUES (?,?,?,?)",
            (gen, self.client_id, self.topic, utcnow()))
        if prev >= 1:
            cur.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? "
                "WHERE generation=? AND ended_at IS NULL",
                (utcnow(), "broker_session_lost", prev))
            cur.execute(
                "UPDATE deliveries SET state=?, updated_at=? "
                "WHERE generation=? AND state IN (?,?,?)",
                (STATE_ABANDONED, utcnow(), prev,
                 STATE_RECEIVED, STATE_COMMITTED, STATE_COMPLETING))
        cur.execute(
            """INSERT INTO protocol_events
               (ts, generation, conn_ordinal, direction, packet_type, event, detail)
               VALUES (?,?,?,'inbound','CONNACK','generation_started',?)""",
            (utcnow(), gen, conn_ordinal,
             "session_present=false; fresh session generation"))
        self.conn.commit()
        return gen

    def resume_generation(self, generation: int, conn_ordinal: int) -> None:
        """CONNACK Session Present = true: keep the same generation and its open rows."""
        self.event(
            direction="inbound", packet_type="CONNACK",
            event="generation_resumed", generation=generation,
            conn_ordinal=conn_ordinal,
            detail="session_present=true; unfinished handshakes redelivered")

    def end_connection(self, generation: int, conn_ordinal: int, reason: str) -> None:
        self.event(direction="local", packet_type="DISCONNECT",
                   event="connection_closed", generation=generation,
                   conn_ordinal=conn_ordinal, detail=reason)

    # --------------------------------------------------------- inbound PUBLISH
    def save_publish(self, generation: int, conn_ordinal: int, packet_id: int,
                     payload: bytes, dup: bool) -> dict[str, Any]:
        """Persist the pending payload *before* PUBREC is ever sent.

        Returns an action dict:
          new          -> a new 'received' row; caller sends PUBREC
          redelivery   -> same open row, identical payload; caller re-sends PUBREC
          conflict     -> same open packet id, DIFFERENT payload: the original
                          payload is protected and the row is not overwritten;
                          caller still answers PUBREC for the original delivery
          reuse_after_complete -> a completed id is reused; a new row is inserted
        """
        sha = digest(payload)
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE generation=? AND packet_id=? "
            "AND state IN (?,?,?)",
            (generation, packet_id,
             STATE_RECEIVED, STATE_COMMITTED, STATE_COMPLETING)).fetchone()

        if row is None:
            cur = self.conn.execute(
                """INSERT INTO deliveries
                   (generation, packet_id, topic, payload, payload_sha256,
                    state, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (generation, packet_id, self.topic, payload, sha,
                 STATE_RECEIVED, utcnow(), utcnow()))
            delivery_id = cur.lastrowid
            self.event(direction="inbound", packet_type="PUBLISH",
                       event="new_publish_saved", generation=generation,
                       conn_ordinal=conn_ordinal, packet_id=packet_id,
                       delivery_id=delivery_id, state_to=STATE_RECEIVED,
                       detail=f"dup={dup}; {len(payload)} bytes; sha256={sha[:16]}…")
            self.conn.commit()
            return {"action": "new", "delivery_id": delivery_id}

        delivery_id = row["delivery_id"]
        if row["payload"] == payload:
            self.event(direction="inbound", packet_type="PUBLISH",
                       event="duplicate_publish_ignored", generation=generation,
                       conn_ordinal=conn_ordinal, packet_id=packet_id,
                       delivery_id=delivery_id, state_from=row["state"],
                       state_to=row["state"],
                       detail=f"dup={dup}; identical payload; no new row, no overwrite")
            self.conn.commit()
            return {"action": "redelivery", "delivery_id": delivery_id}

        self.event(direction="inbound", packet_type="PUBLISH",
                   event="duplicate_publish_payload_conflict",
                   generation=generation, conn_ordinal=conn_ordinal,
                   packet_id=packet_id, delivery_id=delivery_id,
                   state_from=row["state"], state_to=row["state"],
                   detail=("dup=%s; payload differs from the in-flight delivery; "
                           "original payload retained" % dup))
        self.conn.commit()
        return {"action": "conflict", "delivery_id": delivery_id}

    def note_pubrec_sent(self, generation: int, conn_ordinal: int,
                         packet_id: int, delivery_id: int) -> None:
        self.event(direction="outbound", packet_type="PUBREC",
                   event="pubrec_sent_after_commit", generation=generation,
                   conn_ordinal=conn_ordinal, packet_id=packet_id,
                   delivery_id=delivery_id)
        self.conn.commit()

    # ----------------------------------------------------------- inbound PUBREL
    def process_pubrel(self, generation: int, conn_ordinal: int,
                       packet_id: int) -> dict[str, Any]:
        """Produce the single business record, exactly once per delivery.

        Returns:
          committed -> first PUBREL for a 'received' row; the business record
                       was inserted in the same transaction as the state change;
                       the caller must send PUBCOMP.
          duplicate -> the delivery was already committed (or even completed);
                       no new business row, caller just re-sends PUBCOMP.
          orphan    -> no open delivery owns this packet id in this generation.
                       Nothing is inserted; the legacy PUBREL must not be matched
                       against a later, unrelated message. Caller sends PUBCOMP
                       only to close the broker's side, and logs the anomaly.
        """
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=("
            "  SELECT delivery_id FROM deliveries "
            "  WHERE generation=? AND packet_id=? "
            "  ORDER BY delivery_id DESC LIMIT 1)",
            (generation, packet_id)).fetchone()

        # A PUBREL belongs to a delivery that exists in THIS generation and was
        # not abandoned by a session reset. 'committed' (business row in place),
        # 'completing' (PUBCOMP already in flight) and 'completed' (PUBCOMP sent)
        # must all be answered as a redelivery, never as an orphan: a legacy
        # PUBREL is only an orphan when no live delivery owns the packet id.
        if row is None or row["state"] == STATE_ABANDONED:
            self.event(direction="inbound", packet_type="PUBREL",
                       event="orphan_pubrel_rejected", generation=generation,
                       conn_ordinal=conn_ordinal, packet_id=packet_id,
                       delivery_id=row["delivery_id"] if row else None,
                       state_from=row["state"] if row else None,
                       detail="no live delivery owns this packet id in this generation")
            self.conn.commit()
            return {"action": "orphan"}

        if row["state"] == STATE_RECEIVED:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE deliveries SET state=?, updated_at=? WHERE delivery_id=?",
                (STATE_COMMITTED, utcnow(), row["delivery_id"]))
            cur.execute(
                """INSERT INTO business_records
                   (delivery_id, generation, packet_id, payload,
                    payload_sha256, committed_at)
                   VALUES (?,?,?,?,?,?)""",
                (row["delivery_id"], generation, packet_id, row["payload"],
                 row["payload_sha256"], utcnow()))
            business_seq = cur.lastrowid
            cur.execute(
                """INSERT INTO protocol_events
                   (ts, generation, conn_ordinal, packet_id, direction,
                    packet_type, event, delivery_id, business_seq,
                    state_from, state_to, detail)
                   VALUES (?,?,?,?,'inbound','PUBREL','business_record_committed',
                           ?,?,?,?,?)""",
                (utcnow(), generation, conn_ordinal, packet_id,
                 row["delivery_id"], business_seq, STATE_RECEIVED,
                 STATE_COMMITTED, "exactly one business row inserted"))
            self.conn.commit()
            return {"action": "committed", "delivery_id": row["delivery_id"],
                    "business_seq": business_seq}

        # Redelivered PUBREL while committed / completing / completed: the
        # original delivery is the only one that can finish; no second row.
        self.event(direction="inbound", packet_type="PUBREL",
                   event="duplicate_pubrel_ignored", generation=generation,
                   conn_ordinal=conn_ordinal, packet_id=packet_id,
                   delivery_id=row["delivery_id"], state_from=row["state"],
                   state_to=row["state"],
                   detail="delivery already released; no second business row")
        self.conn.commit()
        return {"action": "duplicate", "delivery_id": row["delivery_id"],
                "state": row["state"]}

    def prepare_pubcomp(self, generation: int, conn_ordinal: int,
                        packet_id: int, delivery_id: int, reason: str) -> str:
        """Durably mark PUBCOMP as in flight BEFORE the bytes are sent.

        committed  -> completing : first PUBCOMP for this delivery.
        completing -> completing : crash recovery / redelivery, PUBCOMP resent.
        completed  -> completing : a PUBCOMP was already confirmed sent; this
                                   can only be another redelivery, re-mark it.
        Returns the prior state so the caller can log the exact transition.
        """
        row = self.conn.execute(
            "SELECT state FROM deliveries WHERE delivery_id=?",
            (delivery_id,)).fetchone()
        prior = row["state"]
        self.conn.execute(
            "UPDATE deliveries SET state=?, updated_at=? WHERE delivery_id=?",
            (STATE_COMPLETING, utcnow(), delivery_id))
        self.event(direction="outbound", packet_type="PUBCOMP",
                   event=reason, generation=generation,
                   conn_ordinal=conn_ordinal, packet_id=packet_id,
                   delivery_id=delivery_id, state_from=prior,
                   state_to=STATE_COMPLETING,
                   detail="durable marker committed before socket write")
        self.conn.commit()
        return prior

    def confirm_pubcomp_sent(self, generation: int, conn_ordinal: int,
                             packet_id: int, delivery_id: int) -> None:
        """PUBCOMP bytes are on the wire: free the packet identifier.

        A crash between prepare_pubcomp and this call leaves the row in
        'completing'; on resume the receiver retransmits PUBCOMP rather than
        reprocessing the delivery or inserting another business record.
        """
        self.conn.execute(
            "UPDATE deliveries SET state=?, updated_at=? WHERE delivery_id=? "
            "AND state=?",
            (STATE_COMPLETED, utcnow(), delivery_id, STATE_COMPLETING))
        self.event(direction="outbound", packet_type="PUBCOMP",
                   event="pubcomp_written_completed", generation=generation,
                   conn_ordinal=conn_ordinal, packet_id=packet_id,
                   delivery_id=delivery_id, state_from=STATE_COMPLETING,
                   state_to=STATE_COMPLETED)
        self.conn.commit()

    def note_orphan_pubcomp_sent(self, generation: int, conn_ordinal: int,
                                 packet_id: int) -> None:
        self.event(direction="outbound", packet_type="PUBCOMP",
                   event="pubcomp_sent_for_orphan_pubrel",
                   generation=generation, conn_ordinal=conn_ordinal,
                   packet_id=packet_id,
                   detail="protocol courtesy only; no business row affected")
        self.conn.commit()

    # ----------------------------------------------------------------- queries
    def open_deliveries(self, generation: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM deliveries WHERE generation=? AND state IN (?,?,?) "
            "ORDER BY delivery_id",
            (generation, STATE_RECEIVED, STATE_COMMITTED,
             STATE_COMPLETING)).fetchall())

    def list_sessions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM sessions ORDER BY generation").fetchall())

    def list_deliveries(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM deliveries ORDER BY delivery_id").fetchall())

    def list_business(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM business_records ORDER BY business_seq").fetchall())

    def list_events(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM protocol_events ORDER BY event_id "
               + ("DESC LIMIT ?" if limit else "ASC"))
        params: Iterable[Any] = (limit,) if limit else ()
        if limit:
            rows = list(self.conn.execute(sql, params).fetchall())
            return list(reversed(rows))
        return list(self.conn.execute(sql).fetchall())

    def counts(self) -> dict[str, int]:
        c = self.conn
        return {
            "sessions": c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"],
            "deliveries": c.execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"],
            "business_records": c.execute(
                "SELECT COUNT(*) n FROM business_records").fetchone()["n"],
            "protocol_events": c.execute(
                "SELECT COUNT(*) n FROM protocol_events").fetchone()["n"],
            "open_handshakes": c.execute(
                "SELECT COUNT(*) n FROM deliveries "
                "WHERE state IN ('received','committed','completing')"
            ).fetchone()["n"],
        }
