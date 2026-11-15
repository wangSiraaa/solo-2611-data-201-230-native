"""Unit tests for the durable QoS2 state machine (no broker, no sockets)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from receiver import store as s
from receiver.store import Store

CID, TOPIC = "test-client", "telemetry/line"
P1 = b'{"sensor":"A","value":17}'
P2 = b'{"sensor":"A","value":23}'
P3 = b'{"sensor":"A","value":29}'


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.db = Store(self.db_path, CID, TOPIC)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def complete(self, gen, conn, packet_id, delivery_id):
        """Mirror the receiver's two-phase PUBCOMP handling."""
        self.db.prepare_pubcomp(gen, conn, packet_id, delivery_id,
                                "pubcomp_prepared_first_time")
        self.db.confirm_pubcomp_sent(gen, conn, packet_id, delivery_id)

    # --- scenario 'standard' exercised purely through the state machine ------
    def test_standard_scenario_two_deliveries_one_packet_id(self):
        gen = self.db.begin_generation(conn_ordinal=1)

        # First PUBLISH: new pending row, then PUBREL commits one business row.
        r = self.db.save_publish(gen, 1, 7, P1, dup=False)
        self.assertEqual(r["action"], "new")
        first_delivery = r["delivery_id"]
        self.db.note_pubrec_sent(gen, 1, 7, first_delivery)

        rel = self.db.process_pubrel(gen, 1, 7)
        self.assertEqual(rel["action"], "committed")
        first_business = rel["business_seq"]

        # Simulate the first process dying right after the PUBREL transaction,
        # before PUBCOMP: a second connection resumes the same generation.
        self.db.resume_generation(gen, conn_ordinal=2)
        rel2 = self.db.process_pubrel(gen, 2, 7)
        self.assertEqual(rel2["action"], "duplicate")  # no second business row
        self.complete(gen, 2, 7, first_delivery)

        # Packet id 7 now carries a NEW delivery, with an identical PUBLISH
        # repeated as DUP=1 before the release.
        r = self.db.save_publish(gen, 2, 7, P2, dup=False)
        self.assertEqual(r["action"], "new")
        second_delivery = r["delivery_id"]
        self.assertNotEqual(second_delivery, first_delivery)
        self.db.note_pubrec_sent(gen, 2, 7, second_delivery)

        r = self.db.save_publish(gen, 2, 7, P2, dup=True)
        self.assertEqual(r["action"], "redelivery")
        self.db.note_pubrec_sent(gen, 2, 7, second_delivery)

        rel = self.db.process_pubrel(gen, 2, 7)
        self.assertEqual(rel["action"], "committed")
        second_business = rel["business_seq"]
        self.complete(gen, 2, 7, second_delivery)

        # Required assertions: two deliveries, two business rows, no leftovers.
        self.assertEqual(self.db.counts()["business_records"], 2)
        self.assertEqual(self.db.counts()["deliveries"], 2)
        self.assertEqual(self.db.counts()["open_handshakes"], 0)
        self.assertNotEqual(first_business, second_business)
        payloads = [bytes(r["payload"]) for r in self.db.list_business()]
        self.assertEqual(payloads, [P1, P2])

    def test_duplicate_publish_same_packet_does_not_overwrite_or_add_row(self):
        gen = self.db.begin_generation(1)
        self.db.save_publish(gen, 1, 7, P1, dup=False)
        # Different payload reusing the still-open packet id must be rejected.
        r = self.db.save_publish(gen, 1, 7, P2, dup=True)
        self.assertEqual(r["action"], "conflict")
        rows = [row for row in self.db.list_deliveries()
                if row["state"] in s.OPEN_STATES]
        self.assertEqual(len(rows), 1)
        self.assertEqual(bytes(rows[0]["payload"]), P1)  # original protected
        self.assertEqual(self.db.counts()["business_records"], 0)

    def test_repeated_pubrel_completes_only_original_delivery(self):
        gen = self.db.begin_generation(1)
        r = self.db.save_publish(gen, 1, 7, P1, dup=False)
        self.db.process_pubrel(gen, 1, 7)
        for _ in range(3):
            again = self.db.process_pubrel(gen, 1, 7)
            self.assertEqual(again["action"], "duplicate")
        self.assertEqual(self.db.counts()["business_records"], 1)
        # Even after PUBCOMP, a retransmitted PUBREL is a redelivery of the
        # same handshake: re-answer PUBCOMP, never match it to a later message.
        self.complete(gen, 1, 7, r["delivery_id"])
        late = self.db.process_pubrel(gen, 1, 7)
        self.assertEqual(late["action"], "duplicate")
        self.assertEqual(self.db.counts()["business_records"], 1)

    def test_legacy_pubrel_after_session_reset_is_an_orphan(self):
        gen1 = self.db.begin_generation(1)
        stuck = self.db.save_publish(gen1, 1, 7, P1, dup=False)
        gen2 = self.db.begin_generation(2)  # broker lost the session
        states = {r["delivery_id"]: r["state"]
                  for r in self.db.list_deliveries()}
        self.assertEqual(states[stuck["delivery_id"]], s.STATE_ABANDONED)
        # The old PUBREL arriving on the new session must not create a row nor
        # be matched against a future reuse of packet id 7.
        legacy = self.db.process_pubrel(gen2, 2, 7)
        self.assertEqual(legacy["action"], "orphan")
        self.assertEqual(self.db.counts()["business_records"], 0)

    def test_same_payload_after_completion_is_a_new_delivery(self):
        gen = self.db.begin_generation(1)
        r1 = self.db.save_publish(gen, 1, 7, P1, dup=False)
        self.db.process_pubrel(gen, 1, 7)
        self.complete(gen, 1, 7, r1["delivery_id"])
        r2 = self.db.save_publish(gen, 1, 7, P1, dup=False)  # bytes identical
        self.assertEqual(r2["action"], "new")
        self.assertNotEqual(r1["delivery_id"], r2["delivery_id"])
        rel = self.db.process_pubrel(gen, 1, 7)
        self.assertEqual(rel["action"], "committed")
        self.assertEqual(self.db.counts()["business_records"], 2)

    def test_crash_between_pubcomp_marker_and_socket_write_is_recoverable(self):
        gen = self.db.begin_generation(1)
        r = self.db.save_publish(gen, 1, 7, P1, dup=False)
        self.db.process_pubrel(gen, 1, 7)
        self.assertEqual(self.db.counts()["business_records"], 1)
        # Simulate the hard crash: durable marker committed, process dies here.
        prior = self.db.prepare_pubcomp(gen, 1, 7, r["delivery_id"],
                                        "pubcomp_prepared_first_time")
        self.assertEqual(prior, s.STATE_COMMITTED)
        rows = {x["delivery_id"]: x["state"]
                for x in self.db.list_deliveries()}
        self.assertEqual(rows[r["delivery_id"]], s.STATE_COMPLETING)
        self.assertEqual(self.db.counts()["open_handshakes"], 1)

        # A brand-new Store instance opens the same database file.
        self.db.close()
        restarted = Store(self.db_path, CID, TOPIC)
        restarted.resume_generation(gen, conn_ordinal=2)
        # The broker redelivers PUBREL: no second business row, same delivery.
        again = restarted.process_pubrel(gen, 2, 7)
        self.assertEqual(again["action"], "duplicate")
        self.assertEqual(again["delivery_id"], r["delivery_id"])
        prior2 = restarted.prepare_pubcomp(gen, 2, 7, r["delivery_id"],
                                           "pubcomp_prepared_for_redelivery")
        self.assertEqual(prior2, s.STATE_COMPLETING)
        restarted.confirm_pubcomp_sent(gen, 2, 7, r["delivery_id"])
        self.assertEqual(restarted.counts()["business_records"], 1)
        self.assertEqual(restarted.counts()["open_handshakes"], 0)
        restarted.close()
        self.db = Store(self.db_path, CID, TOPIC)  # reopen for tearDown

    def test_generation_reset_ends_open_state_but_keeps_completed_records(self):
        gen1 = self.db.begin_generation(1)
        stuck = self.db.save_publish(gen1, 1, 7, P1, dup=False)
        self.db.note_pubrec_sent(gen1, 1, 7, stuck["delivery_id"])
        done = self.db.save_publish(gen1, 1, 8, P2, dup=False)
        self.db.process_pubrel(gen1, 1, 8)
        self.complete(gen1, 1, 8, done["delivery_id"])
        completed_before = self.db.counts()["business_records"]
        self.assertEqual(completed_before, 1)

        # Broker lost the session: Session Present=false again -> new generation.
        gen2 = self.db.begin_generation(conn_ordinal=2)
        self.assertEqual(gen2, gen1 + 1)
        self.assertEqual(self.db.counts()["open_handshakes"], 0)
        states = {r["delivery_id"]: r["state"] for r in self.db.list_deliveries()}
        self.assertEqual(states[stuck["delivery_id"]], s.STATE_ABANDONED)
        self.assertEqual(states[done["delivery_id"]], s.STATE_COMPLETED)

        # A legacy PUBREL for the abandoned packet id must not touch the new gen.
        legacy = self.db.process_pubrel(gen2, 2, 7)
        self.assertEqual(legacy["action"], "orphan")
        # The old completed business record survives untouched.
        self.assertEqual(self.db.counts()["business_records"], completed_before)

        # Fresh delivery on the reused id in the new generation is independent.
        fresh = self.db.save_publish(gen2, 2, 7, P3, dup=False)
        self.assertEqual(fresh["action"], "new")
        rel = self.db.process_pubrel(gen2, 2, 7)
        self.assertEqual(rel["action"], "committed")
        self.assertEqual(self.db.counts()["business_records"], 2)

    def test_open_packet_identifier_is_unique_but_completed_rows_are_not(self):
        gen = self.db.begin_generation(1)
        self.db.save_publish(gen, 1, 7, P1, dup=False)
        # The partial unique index itself must refuse a second open row.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO deliveries (generation, packet_id, topic, payload,"
                " payload_sha256, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,'t','t')",
                (gen, 7, TOPIC, P1, s.digest(P1), s.STATE_RECEIVED))
        self.db.conn.rollback()
        # No permanent unique constraint on packet id: completed + new row OK.
        self.db.process_pubrel(gen, 1, 7)
        row = self.db.list_deliveries()[0]
        self.complete(gen, 1, 7, row["delivery_id"])
        again = self.db.save_publish(gen, 1, 7, P2, dup=False)
        self.assertEqual(again["action"], "new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
