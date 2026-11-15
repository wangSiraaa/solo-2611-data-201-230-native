"""Controlled MQTT 3.1.1 client subset over a plain TCP socket.

Why not paho-mqtt (verified against paho 2.2.0, paho/mqtt/client.py):
  * ``_handle_publish`` calls ``_send_pubrec`` immediately on an inbound QoS2
    PUBLISH (line 4147) and keeps the pending message only in an in-memory dict
    (line 4151). There is no callback between PUBLISH and PUBREC, so a receiver
    cannot durably save the payload *before* acknowledging receipt.
  * ``on_message`` only fires from ``_handle_pubrel`` (line 4195).
  * ``manual_ack=True`` merely delays PUBCOMP (lines 4208-4211); PUBREC is still
    automatic. For an unknown PUBREL the library acknowledges anyway and its own
    comment admits this may lose the message (lines 4202-4207); it has no
    on-disk session state.

This module implements exactly the MQTT 3.1.1 packets this receiver needs and
enforces the ordering required by the task: every PUBREC/PUBCOMP is written to
the socket only after the matching SQLite transaction has committed, and every
state transition is driven by the actual packets exchanged.
"""
from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

from . import store as store_mod

# Fixed-header control packet types.
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = range(1, 8)
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = range(8, 15)

PROTOCOL_NAME = b"MQTT"
PROTOCOL_LEVEL_311 = 4
SUBSCRIBE_PACKET_ID = 1


class ProtocolError(RuntimeError):
    """A received packet violates the MQTT 3.1.1 subset used here."""


class BrokerClosed(ConnectionError):
    """The broker closed the connection (normal for the scripted fixture)."""


# --------------------------------------------------------------- packet encode
def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n:
            digit |= 0x80
        out.append(digit)
        if not n:
            return bytes(out)


def _packet(packet_type: int, flags: int, body: bytes = b"") -> bytes:
    return (bytes([(packet_type << 4) | (flags & 0x0F)])
            + _encode_remaining_length(len(body)) + body)


def _utf8(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def encode_connect(client_id: str, keepalive: int) -> bytes:
    # Connect flags = 0x00: Clean Session = 0 (durable session), no will,
    # no username/password, per the fixed-Client-ID / CleanSession=false rule.
    variable = struct.pack("!H4sBB", len(PROTOCOL_NAME), PROTOCOL_NAME,
                           PROTOCOL_LEVEL_311, 0x00) + struct.pack("!H", keepalive)
    payload = _utf8(client_id)
    return _packet(CONNECT, 0, variable + payload)


def encode_subscribe(packet_id: int, topic: str, qos: int) -> bytes:
    body = struct.pack("!H", packet_id) + _utf8(topic) + bytes([qos])
    return _packet(SUBSCRIBE, 0b0010, body)


def encode_ack(packet_type: int, packet_id: int) -> bytes:
    # PUBREL carries fixed-header flags 0b0010; PUBREC/PUBCOMP flags are zero.
    flags = 0b0010 if packet_type == PUBREL else 0
    return _packet(packet_type, flags, struct.pack("!H", packet_id))


# --------------------------------------------------------------- packet decode
@dataclass
class IncomingPublish:
    packet_id: int
    topic: str
    payload: bytes
    dup: bool
    qos: int
    retain: bool


class FrameReader:
    """Reads whole MQTT control packets from a blocking socket, buffering tails."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = bytearray()

    def _fill(self, need: int) -> None:
        while len(self._buf) < need:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrokerClosed("peer closed the TCP connection")
            self._buf.extend(chunk)

    def read(self) -> tuple[int, bytes]:
        self._fill(1)
        fixed = self._buf[0]
        # Remaining length: 1-4 bytes, each contributing 7 bits.
        multiplier = 1
        remaining = 0
        pos = 1
        for _ in range(4):
            self._fill(pos + 1)
            byte = self._buf[pos]
            pos += 1
            remaining += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        else:
            raise ProtocolError("malformed remaining length")
        self._fill(pos + remaining)
        body = bytes(self._buf[pos:pos + remaining])
        del self._buf[:pos + remaining]
        return fixed, body


def parse_publish(fixed: int, body: bytes) -> IncomingPublish:
    dup = bool(fixed & 0x08)
    qos = (fixed >> 1) & 0x03
    retain = bool(fixed & 0x01)
    (topic_len,) = struct.unpack_from("!H", body, 0)
    topic = body[2:2 + topic_len].decode("utf-8")
    pos = 2 + topic_len
    if qos > 0:
        (packet_id,) = struct.unpack_from("!H", body, pos)
        pos += 2
    else:
        packet_id = 0
    return IncomingPublish(packet_id, topic, body[pos:], dup, qos, retain)


def parse_packet_id(body: bytes) -> int:
    if len(body) < 2:
        raise ProtocolError("packet identifier missing")
    return struct.unpack("!H", body[:2])[0]


# ------------------------------------------------------------------ connection
@dataclass
class ConnectionStats:
    conn_ordinal: int
    generation: int
    session_present: bool
    publishes: int = 0
    pubrecs: int = 0
    pubrels: int = 0
    pubcomps: int = 0


def run_connection(store: store_mod.Store, *, host: str, port: int,
                   client_id: str, topic: str, conn_ordinal: int,
                   keepalive: int = 60,
                   crash_before_pubcomp: bool = False,
                   log: Callable[[str], None] = print) -> ConnectionStats:
    """Run one TCP/ MQTT connection until the broker closes it or an error occurs.

    All durable state changes go through ``store`` first; the acknowledgement
    packet is only sent after the corresponding transaction commits.

    crash_before_pubcomp deterministically hard-exits the process after the
    durable 'completing' marker is committed and before the PUBCOMP bytes are
    written, so a separate process can demonstrate PUBCOMP retransmission.
    """
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(1.0)  # tick used for keepalive; blocking otherwise
    reader = FrameReader(sock)
    stats: ConnectionStats | None = None

    try:
        # ---- CONNECT / CONNACK -------------------------------------------
        sock.sendall(encode_connect(client_id, keepalive))
        fixed, body = reader.read()
        if fixed >> 4 != CONNACK or len(body) != 2:
            raise ProtocolError("expected CONNACK")
        session_present = bool(body[0] & 0x01)
        return_code = body[1]
        if return_code != 0:
            raise ProtocolError(f"CONNACK return code {return_code}")

        if session_present:
            generation = store.latest_generation()
            if generation is None:
                raise ProtocolError(
                    "broker claims Session Present but no local session exists")
            store.resume_generation(generation, conn_ordinal)
        else:
            # New session generation: explicitly end any unconfirmed handshakes
            # of the previous generation; completed business rows stay intact.
            generation = store.begin_generation(conn_ordinal)
        stats = ConnectionStats(conn_ordinal, generation, session_present)
        log(f"[conn {conn_ordinal}] CONNACK session_present={session_present} "
            f"generation={generation}")

        # ---- SUBSCRIBE telemetry/line QoS2 (only needed on a fresh session) -
        if not session_present:
            sock.sendall(encode_subscribe(SUBSCRIBE_PACKET_ID, topic, 2))
            fixed, body = reader.read()
            if fixed >> 4 != SUBACK or parse_packet_id(body) != SUBSCRIBE_PACKET_ID:
                raise ProtocolError("expected SUBACK for subscription")
            if body[-1] != 2:
                raise ProtocolError(f"broker refused QoS2 subscription: {body[-1]}")
            store.event(direction="outbound", packet_type="SUBSCRIBE",
                        event="subscribe_qos2_acknowledged",
                        generation=generation, conn_ordinal=conn_ordinal,
                        detail=f"topic={topic} qos=2")
            log(f"[conn {conn_ordinal}] SUBACK confirms {topic} at QoS2")

        last_activity = time.monotonic()
        pending_ping = False

        # ---- inbound QoS2 state machine ----------------------------------
        while True:
            try:
                fixed, body = reader.read()
            except socket.timeout:
                # Keepalive tick; the mock broker answers PINGREQ itself.
                if not pending_ping and time.monotonic() - last_activity >= keepalive:
                    sock.sendall(_packet(PINGREQ, 0))
                    pending_ping = True
                    store.event(direction="outbound", packet_type="PINGREQ",
                                event="keepalive_sent", generation=generation,
                                conn_ordinal=conn_ordinal)
                continue
            last_activity = time.monotonic()
            ptype = fixed >> 4

            if ptype == PINGRESP:
                pending_ping = False
                continue

            if ptype == PUBLISH:
                msg = parse_publish(fixed, body)
                if msg.qos != 2:
                    raise ProtocolError(f"expected QoS2 PUBLISH, got QoS{msg.qos}")
                if msg.topic != topic:
                    raise ProtocolError(f"unexpected topic {msg.topic!r}")
                stats.publishes += 1

                # 1) Durably save the pending payload (or reconcile a duplicate)
                #    BEFORE any PUBREC leaves the process.
                result = store.save_publish(generation, conn_ordinal,
                                            msg.packet_id, msg.payload, msg.dup)
                action = result["action"]
                log(f"[conn {conn_ordinal}] PUBLISH id={msg.packet_id} "
                    f"dup={int(msg.dup)} bytes={len(msg.payload)} -> {action}")

                # 2) PUBREC after commit. A crash before this point simply makes
                #    the broker redeliver the PUBLISH; no row is lost or doubled.
                sock.sendall(encode_ack(PUBREC, msg.packet_id))
                stats.pubrecs += 1
                store.note_pubrec_sent(generation, conn_ordinal,
                                       msg.packet_id, result["delivery_id"])
                continue

            if ptype == PUBREL:
                if (fixed & 0x0F) != 0b0010:
                    raise ProtocolError("malformed PUBREL fixed-header flags")
                packet_id = parse_packet_id(body)
                stats.pubrels += 1

                # The single business record is created inside this transaction
                # (state received -> committed). Repeated PUBRELs return
                # 'duplicate'/'orphan' and can never insert a second row.
                result = store.process_pubrel(generation, conn_ordinal, packet_id)
                action = result["action"]
                log(f"[conn {conn_ordinal}] PUBREL  id={packet_id} -> {action}")

                if action == "orphan":
                    # No local delivery owns this release: answer only to close
                    # the broker's side, without touching any business row.
                    sock.sendall(encode_ack(PUBCOMP, packet_id))
                    stats.pubcomps += 1
                    store.note_orphan_pubcomp_sent(
                        generation, conn_ordinal, packet_id)
                    continue

                # Two-phase PUBCOMP so a crash can never lose or double-finish a
                # delivery: (1) durably mark 'completing', (2) write the bytes,
                # (3) mark 'completed'. A crash between 1 and 2 leaves an
                # explicit in-flight marker that a restart simply retransmits.
                prior = store.prepare_pubcomp(
                    generation, conn_ordinal, packet_id,
                    result["delivery_id"],
                    "pubcomp_prepared_first_time" if action == "committed"
                    else "pubcomp_prepared_for_redelivery")
                if crash_before_pubcomp:
                    # Deterministic fault: durable marker committed, bytes never
                    # written. os._exit skips buffers/finally, mimicking a hard
                    # crash; the WAL commit above is already on disk.
                    log(f"[conn {conn_ordinal}] FAULT: hard exit before PUBCOMP "
                        f"id={packet_id} (row left in 'completing')")
                    os._exit(77)
                sock.sendall(encode_ack(PUBCOMP, packet_id))
                stats.pubcomps += 1
                store.confirm_pubcomp_sent(
                    generation, conn_ordinal, packet_id,
                    result["delivery_id"])
                if prior != "committed":
                    log(f"[conn {conn_ordinal}]         PUBCOMP retransmitted "
                        f"(prior state={prior})")
                continue

            raise ProtocolError(f"unexpected control packet type {ptype}")

    except BrokerClosed as exc:
        if stats is not None:
            store.end_connection(stats.generation, conn_ordinal,
                                 "broker_closed: " + str(exc))
            log(f"[conn {conn_ordinal}] broker closed the connection")
        raise
    finally:
        sock.close()
