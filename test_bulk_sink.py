#!/usr/bin/env python3
"""Unit tests for bulk_sink using an in-memory scripted fake backend.

run_demo.py covers the full HTTP path against mock_es; these tests exercise
the branches that are hard to inject through the mock (attempt-budget
exhaustion, /_mget outage, position/id misalignment, local rejections) by
replacing the three transport helpers with a deterministic fake.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import bulk_sink


class FakeBackend:
    """Stand-in for the HTTP layer: records requests, returns scripted
    bulk responses, and optionally fails /_mget permanently."""

    def __init__(self, bulk_responses=None, mget_raises=False):
        # bulk_responses: list of either dicts (complete bulk reply) or
        # Exception instances (transport failure). Cycled/padded with the
        # last entry.
        self.bulk_responses = bulk_responses or []
        self.mget_raises = mget_raises
        self.bulk_calls: list[bytes] = []
        self.mget_calls: list[dict] = []
        self.mget_targets: dict[tuple, dict] = {}
        self._bulk_index = 0

    # patched transport helpers ------------------------------------------------
    def post_ndjson(self, base_url, path, raw, timeout):
        assert path == "/_bulk"
        self.bulk_calls.append(raw)
        if not self.bulk_responses:
            raise bulk_sink.TransportError("no scripted bulk response")
        reply = self.bulk_responses[min(self._bulk_index,
                                        len(self.bulk_responses) - 1)]
        self._bulk_index += 1
        if isinstance(reply, Exception):
            raise reply
        return 200, json.dumps(reply).encode()

    def post_json(self, base_url, path, payload, timeout):
        assert path == "/_mget"
        self.mget_calls.append(payload)
        if self.mget_raises:
            raise bulk_sink.TransportError("mget down")
        docs = []
        for item in payload["docs"]:
            key = (item["_index"], item["_id"])
            stored = self.mget_targets.get(key)
            if stored is None:
                docs.append({"_index": key[0], "_id": key[1], "found": False})
            else:
                docs.append({"_index": key[0], "_id": key[1], "found": True,
                             "_version": stored["version"],
                             "_source": {"ingest_fingerprint": stored["fp"]}})
        return 200, json.dumps({"docs": docs}).encode()

    def get(self, base_url, path, timeout):
        raise AssertionError("sink must never call /__inspect")


def _install_fake(sink, fake):
    bulk_sink.http_post_ndjson = fake.post_ndjson
    bulk_sink.http_post_json = fake.post_json
    bulk_sink.http_get = fake.get


def _write_input(records):
    tmp = pathlib.Path(tempfile.mkdtemp())
    path = tmp / "in.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in records), encoding="utf-8")
    return tmp, path


def _make_record(sid, doc_id, version=1, document=None):
    return {
        "source_id": sid, "index": "orders", "id": doc_id,
        "version": version,
        "document": document if document is not None
        else {"sku": sid.upper(), "amount_cents": 100},
    }


class BulkSinkTests(unittest.TestCase):
    def setUp(self):
        self._orig = (bulk_sink.http_post_ndjson, bulk_sink.http_post_json,
                      bulk_sink.http_get)

    def tearDown(self):
        (bulk_sink.http_post_ndjson,
         bulk_sink.http_post_json, bulk_sink.http_get) = self._orig

    def _run(self, records, fake, batch_size=2):
        tmp, input_path = _write_input(records)
        sink = bulk_sink.BulkSink("http://fake", tmp / "work",
                                  batch_size=batch_size, retry_interval=0)
        _install_fake(sink, fake)
        sink.load(input_path)
        return sink, sink.send()

    def test_budget_exhaustion_keeps_unresolved(self):
        # Every bulk reply is a definite-but-retryable 429, so after
        # MAX_WRITE_ATTEMPTS dispatches the record must remain unresolved,
        # never swallowed as success.
        rec = _make_record("u1", "x")
        fake = FakeBackend(bulk_responses=[
            {"errors": True, "items": [
                {"index": {"_index": "orders", "_id": "x", "status": 429,
                           "error": {"type": "rejected execution"}}}]}
        ])
        sink, summary = self._run([rec], fake)
        self.assertEqual(summary["counts"]["unresolved"], 1)
        self.assertEqual(summary["items"][0]["attempts"],
                         bulk_sink.MAX_WRITE_ATTEMPTS)
        self.assertEqual(len(fake.bulk_calls), bulk_sink.MAX_WRITE_ATTEMPTS)
        receipt = json.loads((sink.work_dir / "receipts" / "u1.json").read_text())
        self.assertEqual(receipt["status"], bulk_sink.UNRESOLVED)
        self.assertTrue(receipt["history"][-1]["outcome"] == "retryable")

    def test_mget_outage_preserves_budget_then_unresolved(self):
        # 409 makes the write uncertain, but /_mget is down. Nothing can be
        # proven; the item may keep trying within budget and ends unresolved.
        rec = _make_record("u2", "y")
        fake = FakeBackend(mget_raises=True, bulk_responses=[
            {"errors": True, "items": [
                {"index": {"_index": "orders", "_id": "y", "status": 409,
                           "error": {"type": "version_conflict_engine_exception"}}}]}
        ])
        _, summary = self._run([rec], fake)
        self.assertEqual(summary["counts"]["unresolved"], 1)
        # Each of the three rounds attempted one mget transport triad.
        self.assertGreaterEqual(len(fake.mget_calls),
                                bulk_sink.MAX_WRITE_ATTEMPTS)

    def test_nan_document_locally_rejected_without_dispatch(self):
        rec = _make_record("u3", "z", document={"sku": "Z", "amount_cents": float("nan")})
        fake = FakeBackend()
        _, summary = self._run([rec], fake)
        self.assertEqual(summary["counts"]["rejected"], 1)
        self.assertEqual(fake.bulk_calls, [])  # never sent NaN downstream

    def test_non_positive_version_locally_rejected(self):
        rec = _make_record("u4", "w", version=0)
        fake = FakeBackend()
        _, summary = self._run([rec], fake)
        self.assertEqual(summary["counts"]["rejected"], 1)
        self.assertEqual(fake.bulk_calls, [])

    def test_fingerprint_canonicalisation(self):
        doc = {"amount_cents": 1, "sku": "雪", "nested": {"b": 2, "a": [1, 2]}}
        fp = bulk_sink.fingerprint_of(doc)
        # Deterministic, and equal to mock_es's own canonicalisation rule.
        self.assertEqual(fp, bulk_sink.fingerprint_of(
            {"nested": {"a": [1, 2], "b": 2}, "sku": "雪", "amount_cents": 1}))
        with self.assertRaises(ValueError):
            bulk_sink.canonical_bytes({"x": float("inf")})

    def test_same_key_versions_never_share_a_bulk(self):
        records = [_make_record("v1", "k", version=1),
                   _make_record("v2", "k", version=2),
                   _make_record("v3", "other", version=1)]
        # First bulk: 201 for whatever arrives; later 409 to force ordering.
        def reply_for(raw):
            lines = raw.decode().splitlines()
            items = []
            for i in range(0, len(lines), 2):
                meta = json.loads(lines[i])["index"]
                items.append({"index": {"_index": meta["_index"],
                                        "_id": meta["_id"], "status": 201,
                                        "_version": meta["version"]}})
            return {"errors": False, "items": items}

        class AdaptingFake(FakeBackend):
            def post_ndjson(self, base_url, path, raw, timeout):
                self.bulk_calls.append(raw)
                reply = reply_for(raw)
                return 200, json.dumps(reply).encode()

        fake = AdaptingFake()
        _, summary = self._run(records, fake, batch_size=4)
        self.assertEqual(summary["counts"]["confirmed"], 3)
        # No bulk body may contain two items with the same _id.
        for raw in fake.bulk_calls:
            ids = [json.loads(l)["index"]["_id"]
                   for l in raw.decode().splitlines()[0::2]]
            self.assertEqual(len(ids), len(set(ids)), ids)
        # The two versions of "k" must dispatch in source order: locate the
        # first round carrying each version number.
        def first_round_for_version(version):
            for idx, raw in enumerate(fake.bulk_calls, start=1):
                metas = [json.loads(l)["index"]
                         for l in raw.decode().splitlines()[0::2]]
                if any(m["_id"] == "k" and m["version"] == version
                       for m in metas):
                    return idx
            return None
        r_v1, r_v2 = first_round_for_version(1), first_round_for_version(2)
        self.assertIsNotNone(r_v1)
        self.assertIsNotNone(r_v2)
        self.assertLess(r_v1, r_v2)

    def test_position_id_misalignment_triggers_reconcile(self):
        # The reply swaps the item order, so positional ID cross-check fails
        # and every item is treated as uncertain and sent to /_mget.
        records = [_make_record("p1", "one"), _make_record("p2", "two")]
        fake = FakeBackend(bulk_responses=[
            {"errors": False, "items": [
                {"index": {"_index": "orders", "_id": "two", "status": 201}},
                {"index": {"_index": "orders", "_id": "one", "status": 201}}]}
        ])
        _, summary = self._run(records, fake, batch_size=2)
        # mget reports both targets missing -> resendable -> second dispatch
        # (scripted fake repeats the misaligned reply, then mget again);
        # regardless of final state, a misalignment must have produced mgets
        # rather than trusting HTTP 200.
        self.assertTrue(fake.mget_calls,
                        "misaligned bulk reply must trigger /_mget")
        self.assertEqual(summary["counts"]["confirmed"], 0)

    def test_mget_classification_matrix(self):
        # One record each for confirmed / superseded / conflict / missing.
        own = bulk_sink.fingerprint_of({"sku": "A", "amount_cents": 100})
        records = [
            _make_record("c1", "same", document={"sku": "A", "amount_cents": 100}),
            _make_record("c2", "higher"),
            _make_record("c3", "diff"),
            _make_record("c4", "missing"),
        ]
        fake = FakeBackend(bulk_responses=[{
            "errors": True,
            "items": [{"index": {"_index": "orders", "_id": r["id"],
                                 "status": 409}} for r in records],
        }])
        fake.mget_targets = {
            ("orders", "same"): {"version": 1, "fp": own},
            ("orders", "higher"): {"version": 9, "fp": "other"},
            ("orders", "diff"): {"version": 1, "fp": "different-fingerprint"},
            # "missing" intentionally absent
        }
        _, summary = self._run(records, fake, batch_size=4)
        by_id = {it["source_id"]: it["status"] for it in summary["items"]}
        self.assertEqual(by_id["c1"], bulk_sink.CONFIRMED)
        self.assertEqual(by_id["c2"], bulk_sink.SUPERSEDED)
        self.assertEqual(by_id["c3"], bulk_sink.CONFLICT)
        # Missing target: resendable. The scripted fake keeps answering 409,
        # so it consumes its budget as unresolved rather than being faked to
        # succeed — assert it never got misclassified as confirmed.
        self.assertNotIn(by_id["c4"],
                         (bulk_sink.CONFIRMED, bulk_sink.SUPERSEDED,
                          bulk_sink.CONFLICT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
