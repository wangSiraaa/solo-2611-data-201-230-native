#!/usr/bin/env python3
"""Safety tests for the migration: prechecks and failure recovery.

These do not validate the client contract (see ``verify.py``).  They prove the
operational requirements:

* prechecks reject target-object conflicts and a broken input contract before
  anything is changed;
* a second run after a successful migration is refused without disturbing the
  migrated state;
* if any statement in the migration transaction fails -- including after the
  old table has been dropped and the view created -- the whole transaction
  rolls back and the original wide table is still present, complete and
  writable.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

import psycopg

import migration_lib

WIDE_DDL = """
CREATE SCHEMA split_fixture;
CREATE TABLE split_fixture.records(
 id bigint PRIMARY KEY, tenant text NOT NULL, name text NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 note text, attachment bytea);
INSERT INTO split_fixture.records VALUES
 (1,'east','甲','active',NULL,NULL),
 (2,'east','乙','disabled','',decode('','hex')),
 (3,'west','丙','active','详细备注',decode('0001ff','hex')),
 (4,'west','丁','active',NULL,decode('00','hex'));
"""


class Safety:
    def __init__(self, dsn: str) -> None:
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.checks: list[dict[str, Any]] = []

    def close(self) -> None:
        self.conn.close()

    def fresh(self) -> None:
        self.conn.execute("DROP SCHEMA IF EXISTS split_fixture CASCADE")
        self.conn.execute(WIDE_DDL)

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})

    def run_migration(self, sql: str | None = None) -> tuple[bool, str]:
        try:
            self.conn.execute(sql or migration_lib.migration_sql())
            return True, ""
        except psycopg.Error as exc:
            # The script opens its own BEGIN; a failure before COMMIT leaves
            # the session in an aborted transaction, so issue ROLLBACK here
            # (migrate.py instead closes the connection, which does the same).
            self.conn.rollback()
            return False, str(exc).splitlines()[0]

    def relkind(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='split_fixture' AND c.relname=%s", (name,)).fetchone()
        return row[0] if row else None

    def wide_rows(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT id, tenant, name, status, note, attachment "
            "FROM split_fixture.records ORDER BY id").fetchall()

    def assert_wide_intact(self, label: str, expected: list[tuple]) -> None:
        self.record(f"{label}: old name is still a plain table",
                    self.relkind("records") == "r",
                    f"relkind={self.relkind('records')}")
        self.record(f"{label}: wide data unchanged", self.wide_rows() == expected,
                    f"got={self.wide_rows()}")

    def assert_writable(self, label: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name) VALUES (777,'t','probe')")
            self.conn.execute("UPDATE split_fixture.records SET note='x' WHERE id=777")
            self.conn.execute("DELETE FROM split_fixture.records WHERE id=777")
            self.record(f"{label}: old wide table still accepts writes", True)
        except psycopg.Error as exc:
            self.record(f"{label}: old wide table still accepts writes", False, str(exc))

    # -- tests ---------------------------------------------------------------
    def test_target_conflict(self) -> None:
        self.fresh()
        expected = self.wide_rows()
        self.conn.execute("CREATE TABLE split_fixture.records_main(id bigint)")
        ok, msg = self.run_migration()
        self.record("precheck: target conflict is rejected", not ok and "records_main" in msg, msg)
        self.assert_wide_intact("after target-conflict rejection", expected)
        self.assert_writable("after target-conflict rejection")

    def test_contract_mismatch(self) -> None:
        self.fresh()
        # Break the input contract: wrong type on a hot column.
        self.conn.execute("ALTER TABLE split_fixture.records ALTER COLUMN note TYPE integer "
                          "USING NULL::integer")
        expected = self.wide_rows()
        ok, msg = self.run_migration()
        self.record("precheck: contract mismatch is rejected", not ok and "precheck" in msg, msg)
        self.assert_wide_intact("after contract-mismatch rejection", expected)

    def test_wrong_kind(self) -> None:
        self.fresh()
        self.conn.execute("ALTER TABLE split_fixture.records RENAME TO records_wide")
        self.conn.execute("CREATE VIEW split_fixture.records AS "
                          "SELECT * FROM split_fixture.records_wide")
        expected_sql = self.wide_rows()
        ok, msg = self.run_migration()
        self.record("precheck: non-table old name is rejected",
                    not ok and "plain table" in msg, msg)
        # The old name was deliberately left as a view by this test, so the
        # invariant to assert is that its data is untouched and writable
        # through the view is irrelevant here.
        self.record("after kind rejection: old name still resolves to the same data",
                    self.wide_rows() == expected_sql,
                    f"got={self.wide_rows()}")

    def test_second_run(self) -> None:
        self.fresh()
        ok, msg = self.run_migration()
        self.record("second-run setup: first migration succeeds", ok, msg)
        before = self.conn.execute(
            "SELECT * FROM split_fixture.records ORDER BY id").fetchall()
        ok2, msg2 = self.run_migration()
        self.record("second run is rejected by precheck",
                    not ok2 and "already migrated" in msg2, msg2)
        after = self.conn.execute(
            "SELECT * FROM split_fixture.records ORDER BY id").fetchall()
        self.record("second run left migrated data unchanged", before == after)
        self.record("second run left old name as writable view",
                    self.relkind("records") == "v")

    def test_failure_after_swap_restores(self) -> None:
        """Inject a failure after the view exists but before COMMIT.

        The injected statement belongs to the same transaction as the whole
        migration, so this proves the swap is atomic: the DROP TABLE and every
        CREATE are undone and the original wide table returns.
        """
        self.fresh()
        expected = self.wide_rows()

        sql = migration_lib.migration_sql()
        marker = "\nCOMMIT;\n"
        idx = sql.rfind(marker)
        assert idx != -1, "migrate.sql must end with an explicit COMMIT"
        injected = sql[:idx] + "\nDO $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$;\n" + marker
        ok, msg = self.run_migration(injected)
        self.record("injected post-swap failure is surfaced",
                    not ok and "injected failure" in msg, msg)
        self.assert_wide_intact("after post-swap failure", expected)
        self.assert_writable("after post-swap failure")
        # No half-built split objects may remain.
        for obj in ("records_main", "records_detail", "migration_report"):
            self.record(f"after post-swap failure: {obj} was rolled back",
                        self.relkind(obj) is None)
        # And a real run afterwards still works.
        ok2, msg2 = self.run_migration()
        self.record("migration succeeds on retry after rollback", ok2, msg2)

    def test_multirow_atomicity(self) -> None:
        """A multi-row statement must not leave a partial cold-field change."""
        self.fresh()
        ok, msg = self.run_migration()
        self.record("multirow setup: migration succeeds", ok, msg)

        before = self._split_snapshot()
        # id=1 is first (would get a new detail), id=2 second (bad status).
        try:
            self.conn.execute(
                "UPDATE split_fixture.records "
                "SET note='should not persist', "
                "    status=CASE WHEN id=2 THEN 'broken' ELSE status END "
                "WHERE id IN (1,2)")
            failed = False
        except psycopg.Error:
            self.conn.rollback()
            failed = True
        after = self._split_snapshot()
        self.record("multirow UPDATE: statement rejected", failed)
        self.record("multirow UPDATE: neither main nor detail changed", before == after,
                    f"before={before} after={after}")

        # Multi-row INSERT: first row is legal and would create a detail row,
        # second row duplicates an existing id.  Nothing may persist.
        before = self._split_snapshot()
        try:
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name,note) VALUES "
                "(700,'t','new','would create detail'), (2,'t','dup',NULL)")
            failed = False
        except psycopg.Error:
            self.conn.rollback()
            failed = True
        after = self._split_snapshot()
        self.record("multirow INSERT: statement rejected", failed)
        self.record("multirow INSERT: no partial main row and no partial detail",
                    before == after, f"before={before} after={after}")

        # Multi-row INSERT where a later row breaks the status CHECK, while an
        # earlier row would have created a detail row.
        before = self._split_snapshot()
        try:
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name,status,note) VALUES "
                "(701,'t','ok','active','detail'), (702,'t','bad','broken',NULL)")
            failed = False
        except psycopg.Error:
            self.conn.rollback()
            failed = True
        after = self._split_snapshot()
        self.record("multirow INSERT (CHECK): statement rejected", failed)
        self.record("multirow INSERT (CHECK): nothing persisted", before == after,
                    f"before={before} after={after}")

    def _split_snapshot(self) -> dict[str, list]:
        main = self.conn.execute(
            "SELECT id,tenant,name,status FROM split_fixture.records_main ORDER BY id").fetchall()
        detail = self.conn.execute(
            "SELECT id,note,attachment FROM split_fixture.records_detail ORDER BY id").fetchall()
        return {"main": main, "detail": detail}

    def run(self) -> dict[str, Any]:
        for test in (
            self.test_target_conflict,
            self.test_contract_mismatch,
            self.test_wrong_kind,
            self.test_second_run,
            self.test_failure_after_swap_restores,
            self.test_multirow_atomicity,
        ):
            test()
        return {
            "checks": self.checks,
            "passed": sum(1 for c in self.checks if c["ok"]),
            "failed": sum(1 for c in self.checks if not c["ok"]),
        }


def main() -> int:
    p = argparse.ArgumentParser(description="Migration safety tests.")
    p.add_argument("--dsn", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    args = p.parse_args()

    t = Safety(args.dsn)
    try:
        result = t.run()
    finally:
        t.close()

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for c in result["checks"]:
            print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']}")
            if not c["ok"] and c["detail"]:
                print(f"        {c['detail']}")
        print(f"\n{result['passed']} passed, {result['failed']} failed")
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
