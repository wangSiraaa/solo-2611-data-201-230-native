"""End-to-end and invariant tests for the partition assembly pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from partition_etl.engine import Pipeline, required_partition
from partition_etl.events import previous_day, read_jsonl

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_csv(path: Path, text: str) -> str:
    path.write_bytes(text.encode("utf-8"))
    return sha(text.encode("utf-8"))


class Workspace:
    """Isolated input tree + output dir for one scenario."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.input_root = root / "input"
        self.out = root / "out"
        self.input_root.mkdir(parents=True)
        self.config = {
            "target_dates": ["2026-01-02", "2026-01-03", "2026-01-04"],
            "input_root": str(self.input_root),
        }
        self.events: list[dict] = []

    def add_file(self, name: str, text: str) -> tuple[str, str]:
        path = self.input_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return name, write_csv(path, text)

    def event(self, event_id: str, source: str, partition: str, version: int, name: str) -> dict:
        path = self.input_root / name
        event = {
            "event_id": event_id,
            "source": source,
            "partition": partition,
            "version": version,
            "path": name,
            "sha256": sha(path.read_bytes()),
        }
        self.events.append(event)
        return event

    def run(self, events: list[dict] | None = None) -> dict:
        chosen = self.events if events is None else events
        text = "\n".join(json.dumps(event) for event in chosen) + "\n"
        pipeline = Pipeline(self.config, self.out)
        return pipeline.ingest(read_jsonl(text))

    def status(self) -> dict:
        return Pipeline(self.config, self.out).status()

    def read_output(self, target: str) -> bytes:
        return (self.out / "assemblies" / target / "current.csv").read_bytes()

    def binding(self, target: str) -> dict:
        return json.loads((self.out / "assemblies" / target / "current.json").read_text())


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def target(self, status: dict, date: str) -> dict:
        return next(item for item in status["targets"] if item["date"] == date)

    def fixture_events(self) -> list[dict]:
        return [
            json.loads(line)
            for line in (FIXTURES / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]

    def copy_fixtures(self) -> None:
        destination = self.ws.input_root / "fixtures"
        destination.mkdir(parents=True, exist_ok=True)
        for path in FIXTURES.glob("*.csv"):
            shutil.copy(path, destination / path.name)


class FixtureScenarioTests(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.copy_fixtures()
        self.ws.config["input_root"] = str(self.ws.input_root)
        self.result = self.ws.run(self.fixture_events())

    def test_outcomes_match_storyline(self) -> None:
        codes = [(item["event_id"], item["outcome"], item["code"]) for item in self.result["results"]]
        self.assertEqual(
            codes,
            [
                ("e1", "accepted", "accepted_effective"),
                ("e2", "accepted", "accepted_effective"),
                ("e3", "accepted", "accepted_effective"),
                ("e4", "accepted", "accepted_effective"),
                ("e3", "duplicate", "duplicate_partition_version"),
                ("e5", "duplicate", "duplicate_partition_version"),
                ("e6", "accepted", "accepted_effective"),
                ("e7", "accepted", "accepted_effective"),
                ("e8", "rejected", "version_content_conflict"),
                ("e9", "accepted", "accepted_effective"),
            ],
        )

    def test_sales_first_then_fx_revision_produces_two_generations(self) -> None:
        # Sales arrived before fx and did not fake an output.
        status = self.ws.status()
        target = self.target(status, "2026-01-02")
        self.assertTrue(target["latest_inputs_processed"])
        self.assertEqual(target["recent_success"]["generation"], 2)
        self.assertEqual(
            target["recent_success_vector"]["fx"]["version"], 2
        )
        history = json.loads(
            (self.ws.out / "state.json").read_text()
        )["outputs"]["2026-01-02"]["history"]
        self.assertEqual([item["generation"] for item in history], [1, 2])
        self.assertEqual(history[0]["input_vector"]["fx"]["version"], 1)

    def test_cny_detail_and_rates(self) -> None:
        text = self.ws.read_output("2026-01-02").decode()
        self.assertEqual(
            text,
            "order_id,currency,amount,rate,cny_amount\n"
            "o1,USD,10.00,7.10,71.00\n"
            "o2,CNY,3.50,1,3.50\n",
        )
        self.assertEqual(
            self.ws.read_output("2026-01-03").decode(),
            "order_id,currency,amount,rate,cny_amount\n"
            "o3,EUR,2.00,7.50,15.00\n",
        )

    def test_fx_revision_affects_next_day_only(self) -> None:
        # fx 01-02 v2 adds EUR and fixes 2026-01-03, not 2026-01-02.
        binding_02 = self.ws.binding("2026-01-02")
        self.assertEqual(binding_02["input_vector"]["fx"]["partition"], "2026-01-01")
        binding_03 = self.ws.binding("2026-01-03")
        self.assertEqual(binding_03["input_vector"]["fx"]["partition"], "2026-01-02")
        self.assertEqual(binding_03["input_vector"]["fx"]["version"], 2)

    def test_failed_etl_attempt_is_logged(self) -> None:
        # fx 01-02 v1 lacked EUR: the 01-03 ETL attempt failed, then v2 fixed it.
        etl = self.ws.status()["runs"][0]["etl"]
        failed = [item for item in etl if item["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["target"], "2026-01-03")
        self.assertEqual(failed[0]["event_id"], "e6")
        self.assertEqual(failed[0]["code"], "missing_rate")

    def test_one_bad_partition_does_not_block_another_day(self) -> None:
        status = self.ws.status()
        self.assertTrue(self.target(status, "2026-01-02")["latest_inputs_processed"])
        self.assertTrue(self.target(status, "2026-01-03")["latest_inputs_processed"])
        target = self.target(status, "2026-01-04")
        self.assertFalse(target["latest_inputs_processed"])
        self.assertEqual(
            target["failure_reason"]["missing"], [{"source": "fx", "partition": "2026-01-03"}]
        )

    def test_late_old_version_does_not_regress(self) -> None:
        # Re-sending fx 01-01 v1 after v2 must not change the effective vector.
        before = (self.ws.out / "assemblies" / "2026-01-02" / "current.csv").read_bytes()
        event = next(e for e in self.fixture_events() if e["event_id"] == "e5")
        # e5's event_id is already known; use a fresh event id for the late v1.
        event = dict(event, event_id="late-v1")
        result = self.ws.run([event])
        self.assertEqual(result["results"][0]["outcome"], "duplicate")
        after = (self.ws.out / "assemblies" / "2026-01-02" / "current.csv").read_bytes()
        self.assertEqual(before, after)
        binding = self.ws.binding("2026-01-02")
        self.assertEqual(binding["input_vector"]["fx"]["version"], 2)
        self.assertEqual(binding["generation"], 2)

    def test_duplicate_notifications_do_not_recompute(self) -> None:
        before = {
            day: (self.ws.out / "assemblies" / day / "current.csv").read_bytes()
            for day in ("2026-01-02", "2026-01-03")
        }
        result = self.ws.run(self.fixture_events())
        self.assertEqual(result["status"], "unchanged")
        # Every repeated effective event either duplicates or is a no-op.
        for item in result["results"]:
            self.assertNotEqual(item["code"], "accepted_effective")
        after = {
            day: (self.ws.out / "assemblies" / day / "current.csv").read_bytes()
            for day in ("2026-01-02", "2026-01-03")
        }
        self.assertEqual(before, after)

    def test_run_log_proves_selective_etl(self) -> None:
        runs = self.ws.status()["runs"]
        self.assertEqual(len(runs), 1)
        etl_targets = [(item["target"], item["event_id"]) for item in runs[0]["etl"]]
        self.assertEqual(
            etl_targets,
            [
                ("2026-01-02", "e3"),
                ("2026-01-02", "e4"),
                ("2026-01-03", "e6"),
                ("2026-01-03", "e7"),
            ],
        )


class BehavioralTests(BaseCase):
    def test_no_discovery_without_notice(self) -> None:
        for name, text in {
            "sales_2026-01-02.csv": "order_id,currency,amount\no1,USD,1.00\n",
            "fx_2026-01-01.csv": "currency,rate\nUSD,7.00\n",
        }.items():
            self.ws.add_file(name, text)
        result = self.ws.run([])
        self.assertEqual(result["processed"], 0)
        status = self.ws.status()
        target = self.target(status, "2026-01-02")
        self.assertFalse(target["latest_inputs_processed"])
        self.assertEqual(len(target["missing_dependencies"]), 2)
        self.assertFalse((self.ws.out / "assemblies").exists())

    def test_sales_before_fx(self) -> None:
        _, _ = self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,10.00\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        first = self.ws.run()
        self.assertFalse((self.ws.out / "assemblies" / "2026-01-02").exists())
        target = self.target(self.ws.status(), "2026-01-02")
        self.assertFalse(target["latest_inputs_processed"])
        self.assertEqual(target["failure_reason"]["missing"], [{"source": "fx", "partition": "2026-01-01"}])

        self.ws.add_file("fx.csv", "currency,rate\nUSD,7.00\n")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx.csv")
        second = self.ws.run()
        self.assertEqual(second["created"], {"2026-01-02": 1})
        self.assertEqual(
            self.ws.read_output("2026-01-02").decode(),
            "order_id,currency,amount,rate,cny_amount\no1,USD,10.00,7.00,70.00\n",
        )

    def test_missing_rate_keeps_previous_generation_and_flags_latest(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,EUR,2.00\n")
        self.ws.event("s1", "sales", "2026-01-03", 1, "s.csv")
        self.ws.add_file("fx1.csv", "currency,rate\nEUR,7.50\n")
        self.ws.event("f1", "fx", "2026-01-02", 1, "fx1.csv")
        self.ws.run()
        self.assertEqual(self.target(self.ws.status(), "2026-01-03")["recent_success"]["generation"], 1)
        good = self.ws.read_output("2026-01-03")

        # fx v2 drops EUR -> ETL fails, v1 output must survive untouched.
        self.ws.add_file("fx2.csv", "currency,rate\nUSD,7.20\n")
        self.ws.event("f2", "fx", "2026-01-02", 2, "fx2.csv")
        result = self.ws.run([self.ws.events[-1]])
        self.assertEqual(result["results"][0]["code"], "accepted_effective")
        self.assertEqual(self.ws.read_output("2026-01-03"), good)
        self.assertEqual(self.ws.binding("2026-01-03")["generation"], 1)

        target = self.target(self.ws.status(), "2026-01-03")
        self.assertFalse(target["latest_inputs_processed"])
        self.assertEqual(target["failure_reason"]["code"], "missing_rate")
        self.assertEqual(target["failure_reason"]["unresolved"][0]["order_id"], "o1")
        # latest vector shows v2, recent success still v1
        self.assertEqual(target["latest_available_vector"]["fx"]["version"], 2)
        self.assertEqual(target["recent_success_vector"]["fx"]["version"], 1)

    def test_bad_sales_content_rejected_and_flagged_stale(self) -> None:
        self.ws.add_file("s1.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s1.csv")
        self.ws.add_file("fx1.csv", "currency,rate\nUSD,7.00\n")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx1.csv")
        self.ws.run()
        self.assertTrue(self.target(self.ws.status(), "2026-01-02")["latest_inputs_processed"])

        # Newer sales v2 has a duplicate order id -> rejected, not usable.
        self.ws.add_file("s2.csv", "order_id,currency,amount\no1,USD,1.00\no1,USD,2.00\n")
        self.ws.event("s2", "sales", "2026-01-02", 2, "s2.csv")
        result = self.ws.run([self.ws.events[-1]])
        self.assertEqual(result["results"][0]["code"], "invalid_content")
        target = self.target(self.ws.status(), "2026-01-02")
        self.assertFalse(target["latest_inputs_processed"])
        self.assertEqual(target["failure_reason"]["code"], "latest_completion_rejected")
        self.assertEqual(target["recent_success_vector"]["sales"]["version"], 1)

    def test_sha_mismatch_rejected_path_escape_rejected(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\n")
        bad = {
            "event_id": "s1",
            "source": "sales",
            "partition": "2026-01-02",
            "version": 1,
            "path": "s.csv",
            "sha256": "0" * 64,
        }
        escape = {
            "event_id": "s2",
            "source": "sales",
            "partition": "2026-01-02",
            "version": 1,
            "path": "../s.csv",
            "sha256": sha((self.ws.input_root / "s.csv").read_bytes()),
        }
        result = self.ws.run([bad, escape])
        self.assertEqual(
            [item["code"] for item in result["results"]],
            ["sha256_mismatch", "path_rejected"],
        )
        self.assertFalse((self.ws.out / "assemblies").exists())

    def test_event_id_conflict(self) -> None:
        self.ws.add_file("a.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.add_file("b.csv", "order_id,currency,amount\no2,USD,2.00\n")
        a = self.ws.event("dup", "sales", "2026-01-02", 1, "a.csv")
        b = dict(
            self.ws.event("dup", "sales", "2026-01-02", 1, "b.csv"),
        )
        # Same event id, same version, different content: event-id conflict.
        result = self.ws.run([a, b])
        self.assertEqual(result["results"][1]["code"], "event_id_conflict")

    def test_same_version_different_content_rejected_then_valid_continues(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,10.00\n")
        self.ws.add_file("fx.csv", "currency,rate\nUSD,7.00\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx.csv")
        self.ws.run()
        good = self.ws.read_output("2026-01-02")

        # fx v1 same version, different content, new event id -> rejected.
        self.ws.add_file("fx_bad.csv", "currency,rate\nUSD,9.00\n")
        conflict = dict(self.ws.event("f1b", "fx", "2026-01-01", 1, "fx_bad.csv"))
        # Later legal event for a different day must still be processed.
        self.ws.add_file("s3.csv", "order_id,currency,amount\no2,USD,1.00\n")
        self.ws.add_file("fx3.csv", "currency,rate\nUSD,7.00\n")
        self.ws.event("s3e", "sales", "2026-01-03", 1, "s3.csv")
        self.ws.event("f3e", "fx", "2026-01-02", 1, "fx3.csv")
        result = self.ws.run(self.ws.events[-3:])
        self.assertEqual(result["results"][0]["code"], "version_content_conflict")
        self.assertEqual(result["results"][1]["outcome"], "accepted")
        self.assertEqual(result["results"][2]["outcome"], "accepted")
        self.assertEqual(self.ws.read_output("2026-01-02"), good)
        self.assertEqual(
            self.ws.read_output("2026-01-03").decode(),
            "order_id,currency,amount,rate,cny_amount\no2,USD,1.00,7.00,7.00\n",
        )

    def test_round_half_up_two_places(self) -> None:
        # 1.005 * 1 => 1.01 under HALF_UP; 2.675 * 1 => 2.68.
        self.ws.add_file("s.csv", "order_id,currency,amount\na,USD,1.005\nb,USD,2.675\n")
        self.ws.add_file("fx.csv", "currency,rate\nUSD,1\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx.csv")
        self.ws.run()
        self.assertEqual(
            self.ws.read_output("2026-01-02").decode(),
            "order_id,currency,amount,rate,cny_amount\n"
            "a,USD,1.005,1,1.01\n"
            "b,USD,2.675,1,2.68\n",
        )

    def test_versions_are_independent_across_sources(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.add_file("fx1.csv", "currency,rate\nUSD,7.00\n")
        self.ws.add_file("fx2.csv", "currency,rate\nUSD,7.10\n")
        self.ws.event("s1", "sales", "2026-01-02", 10, "s.csv")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx1.csv")
        self.ws.event("f2", "fx", "2026-01-01", 2, "fx2.csv")
        self.ws.run()
        binding = self.ws.binding("2026-01-02")
        self.assertEqual(binding["input_vector"]["sales"]["version"], 10)
        self.assertEqual(binding["input_vector"]["fx"]["version"], 2)
        self.assertEqual(required_partition("fx", "2026-01-02"), previous_day("2026-01-02"))

    def test_unchanged_output_bytes_and_generation(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\no2,USD,2.00\n")
        self.ws.add_file("fx1.csv", "currency,rate\nUSD,7.00\n")
        self.ws.add_file("fx2.csv", "currency,rate\nUSD,7.20\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        self.ws.event("s2", "sales", "2026-01-03", 1, "s.csv")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx1.csv")
        self.ws.event("f0", "fx", "2026-01-02", 1, "fx1.csv")
        self.ws.run()
        day2_before = self.ws.read_output("2026-01-03")
        binding2_before = self.ws.binding("2026-01-03")
        self.ws.event("f2", "fx", "2026-01-01", 2, "fx2.csv")
        self.ws.run([self.ws.events[-1]])
        # 01-02 uses fx 01-01 -> changes; 01-03 uses fx 01-02 -> untouched.
        self.assertEqual(self.ws.read_output("2026-01-03"), day2_before)
        self.assertEqual(self.ws.binding("2026-01-03"), binding2_before)
        self.assertEqual(self.ws.binding("2026-01-02")["input_vector"]["fx"]["version"], 2)

    def test_status_reports_exact_missing_dependencies(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.event("s1", "sales", "2026-01-04", 1, "s.csv")
        self.ws.run()
        target = self.target(self.ws.status(), "2026-01-04")
        self.assertEqual(
            target["missing_dependencies"], [{"source": "fx", "partition": "2026-01-03"}]
        )
        self.assertEqual(
            target["required"], {"sales": "2026-01-04", "fx": "2026-01-03"}
        )

    def test_late_older_version_after_effective_is_ignored(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.add_file("fx1.csv", "currency,rate\nUSD,7.00\n")
        self.ws.add_file("fx2.csv", "currency,rate\nUSD,7.20\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        self.ws.event("f2", "fx", "2026-01-01", 2, "fx2.csv")
        self.ws.run()
        binding = self.ws.binding("2026-01-02")
        # v1 arrives late; accepted as content but effective stays v2.
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx1.csv")
        result = self.ws.run([self.ws.events[-1]])
        self.assertEqual(result["results"][0]["code"], "accepted_not_effective")
        self.assertEqual(self.ws.binding("2026-01-02"), binding)

    def test_export_matches_current_binding(self) -> None:
        self.ws.add_file("s.csv", "order_id,currency,amount\no1,USD,1.00\n")
        self.ws.add_file("fx.csv", "currency,rate\nUSD,7.00\n")
        self.ws.event("s1", "sales", "2026-01-02", 1, "s.csv")
        self.ws.event("f1", "fx", "2026-01-01", 1, "fx.csv")
        self.ws.run()
        from partition_etl.cli import main

        destination = self.ws.root / "export.csv"
        code = main(
            [
                "export",
                "--config",
                str(self._write_config()),
                "--out",
                str(self.ws.out),
                "--date",
                "2026-01-02",
                "--to",
                str(destination),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(destination.read_bytes(), self.ws.read_output("2026-01-02"))

    def _write_config(self) -> Path:
        path = self.ws.root / "config.json"
        path.write_text(json.dumps(self.ws.config))
        return path


class FixtureIntegrityTests(BaseCase):
    def test_fixture_hashes_match_notices(self) -> None:
        events = self.fixture_events()
        for event in events:
            path = FIXTURES.parent / event["path"]
            self.assertEqual(sha(path.read_bytes()), event["sha256"])

    def test_decimal_reference_math(self) -> None:
        self.assertEqual(
            (Decimal("10.00") * Decimal("7.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            Decimal("71.00"),
        )


if __name__ == "__main__":
    unittest.main()
