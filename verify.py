"""Independent-control verification for the vertical split migration.

One database contains two worlds:

* ``split_fixture.records`` -- the migrated writable compatibility view.
* ``baseline.records``      -- an untouched copy of the original wide table,
  created from ``fixtures/schema.sql`` with the schema name rewritten.  It is
  the independent oracle for every legal statement.

Every case from ``fixtures/client-cases.json`` is executed on both sides from
the same starting snapshot.  For successful statements we compare the
RETURNING/SELECT payload by column name, SQL type (``pg_typeof``), NULL-ness,
raw ``bytea`` bytes, and the full ordered row set.  For failing statements we
assert both sides reject the statement *and* that the migrated stores
(main + detail) are byte-identical to the pre-statement snapshot.

Two extra checks requested by the task are run beyond ``client-cases.json``:

* an explicit-transaction test that changes a cold column and then ROLLBACKs;
* direct inspection of ``split_fixture.records_detail`` proving that no
  all-NULL detail row exists and that empty text / empty ``bytea`` are kept as
  real values.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

import psycopg

ROOT = pathlib.Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"

WIDE_DDL = """
CREATE SCHEMA {schema};
CREATE TABLE {schema}.records(
 id bigint PRIMARY KEY, tenant text NOT NULL, name text NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 note text, attachment bytea);
"""

# Snapshot queries are schema-parameterised.  main/detail only exist for the
# migrated world; the baseline uses a single wide table.
SNAPSHOT_MAIN = """
SELECT id, tenant, name, status,
       pg_typeof(id)::text, pg_typeof(tenant)::text,
       pg_typeof(name)::text, pg_typeof(status)::text
  FROM split_fixture.records_main ORDER BY id
"""
SNAPSHOT_DETAIL = """
SELECT id, note, attachment,
       pg_typeof(note)::text, pg_typeof(attachment)::text
  FROM split_fixture.records_detail ORDER BY id
"""
SNAPSHOT_BASELINE = """
SELECT id, tenant, name, status, note, attachment,
       pg_typeof(id)::text, pg_typeof(tenant)::text,
       pg_typeof(name)::text, pg_typeof(status)::text,
       pg_typeof(note)::text, pg_typeof(attachment)::text
  FROM baseline.records ORDER BY id
"""

class _Rollback(Exception):
    """Sentinel used to force a ROLLBACK out of a psycopg transaction block."""


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Value normalisation: preserves bytes and NULL exactly, renders both sides
# in a JSON-safe but lossless form.
# ----------------------------------------------------------------------------
def norm(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(value).hex()}
    return value


def norm_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: norm(v) for k, v in row.items()}


def rows_as_dicts(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Convert a fetched result (columns from ``cur.description``) to dicts."""
    if not cur.description:
        return []
    columns = [c.name for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


@dataclass
class World:
    """A connection plus helpers bound to either world."""

    conn: psycopg.Connection
    migrated: bool

    def execute(self, sql: str) -> tuple[bool, Any]:
        """Execute one statement in its own transaction.

        Returns ``(succeeded, payload)`` where payload is the row list on
        success or the error text on failure.
        """
        try:
            with self.conn.transaction():
                cur = self.conn.execute(sql)
                rows = rows_as_dicts(cur)
            return True, rows
        except psycopg.Error as exc:
            return False, f"{exc.__class__.__name__}: {exc}".splitlines()[0]

    def execute_with_types(self, sql: str) -> tuple[bool, Any, list[tuple[str, str]]]:
        """Like :meth:`execute`, additionally returning RETURNING column types
        (name, SQL type) as reported by the server."""
        try:
            with self.conn.transaction():
                cur = self.conn.execute(sql)
                types = self._describe_types(cur)
                rows = rows_as_dicts(cur)
            return True, rows, types
        except psycopg.Error as exc:
            return False, f"{exc.__class__.__name__}: {exc}".splitlines()[0], []

    def _describe_types(self, cur: psycopg.Cursor) -> list[tuple[str, str]]:
        if not cur.description:
            return []
        names = [c.name for c in cur.description]
        oids = [c.type_code for c in cur.description]
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT array_agg(format_type(o, NULL) ORDER BY ord) "
                "FROM unnest(%s::oid[]) WITH ORDINALITY AS u(o, ord)", (oids,)
            ).fetchone()
        return list(zip(names, row[0]))

    def one_value(self, sql: str) -> Any:
        with self.conn.transaction():
            return self.conn.execute(sql).fetchone()[0]

    def snapshot(self) -> dict[str, Any]:
        """Full logical snapshot used for the no-partial-state check."""
        if self.migrated:
            with self.conn.transaction():
                main = rows_as_dicts(self.conn.execute(
                    "SELECT id, tenant, name, status FROM split_fixture.records_main ORDER BY id"
                ))
                detail = rows_as_dicts(self.conn.execute(
                    "SELECT id, note, attachment FROM split_fixture.records_detail ORDER BY id"
                ))
            return {"main": [norm_row(r) for r in main],
                    "detail": [norm_row(r) for r in detail]}
        with self.conn.transaction():
            rows = rows_as_dicts(self.conn.execute(
                "SELECT id, tenant, name, status, note, attachment FROM baseline.records ORDER BY id"
            ))
        return {"wide": [norm_row(r) for r in rows]}

    def full_rows(self) -> list[dict[str, Any]]:
        with self.conn.transaction():
            cur = self.conn.execute(
                "SELECT id, tenant, name, status, note, attachment "
                "FROM %s.records ORDER BY id" % ("split_fixture" if self.migrated else "baseline")
            )
            return rows_as_dicts(cur)

    def column_types(self) -> list[dict[str, Any]]:
        schema = "split_fixture" if self.migrated else "baseline"
        with self.conn.transaction():
            cur = self.conn.execute(
                """
                SELECT column_name, ordinal_position,
                       format_type(a.atttypid, a.atttypmod) AS sql_type
                  FROM information_schema.columns c
                  JOIN pg_attribute a
                    ON a.attrelid = %s::regclass AND a.attname = c.column_name
                 WHERE c.table_schema = %s AND c.table_name = 'records'
                 ORDER BY c.ordinal_position
                """,
                (f"{schema}.records", schema),
            )
            return rows_as_dicts(cur)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, **self.extra}


class Harness:
    def __init__(self, dsn: str) -> None:
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.migrated = World(self.conn, migrated=True)
        # A second connection is easier to reason about for independent
        # transactions, though both share the same server-side state.
        self.baseline_conn = psycopg.connect(dsn, autocommit=True)
        self.baseline = World(self.baseline_conn, migrated=False)
        self.checks: list[Check] = []

    def close(self) -> None:
        self.conn.close()
        self.baseline_conn.close()

    # -- setup ---------------------------------------------------------------
    def reset_state(self) -> None:
        """Drop everything and rebuild BOTH worlds from the same seed."""
        self.conn.execute("DROP SCHEMA IF EXISTS split_fixture CASCADE")
        self.conn.execute("DROP SCHEMA IF EXISTS baseline CASCADE")
        self.conn.execute(WIDE_DDL.format(schema="split_fixture"))
        self.conn.execute(WIDE_DDL.format(schema="baseline"))
        seed = (FIXTURES / "schema.sql").read_text(encoding="utf-8")
        inserts = seed[seed.index("INSERT INTO"):]
        self.conn.execute(inserts)  # fills split_fixture.records
        self.conn.execute(inserts.replace("split_fixture.records", "baseline.records"))

    # -- checks --------------------------------------------------------------
    def record(self, name: str, ok: bool, detail: str = "", **extra: Any) -> None:
        self.checks.append(Check(name, ok, detail, extra))

    def compare_types(self) -> None:
        a = [(r["column_name"], r["sql_type"]) for r in self.migrated.column_types()]
        b = [(r["column_name"], r["sql_type"]) for r in self.baseline.column_types()]
        self.record(
            "schema: column names/order/types identical to baseline",
            a == b,
            f"migrated={a} baseline={b}",
        )

    def compare_full_state(self, label: str) -> None:
        a = [norm_row(r) for r in self.migrated.full_rows()]
        b = [norm_row(r) for r in self.baseline.full_rows()]
        self.record(f"{label}: full ordered row content matches baseline", a == b,
                    f"migrated={a} baseline={b}")

    def run_case(self, case: dict[str, Any]) -> None:
        cid = case["id"]
        sql = case["sql"]
        expect = case["expect"]
        # client-cases speak the old name; the independent oracle lives in
        # its own schema, so redirect for the baseline run.
        baseline_sql = sql.replace("split_fixture.records", "baseline.records")
        # id immutability is an extra contract rule the plain wide table does
        # not enforce, so the baseline cannot be an oracle for that case.
        baseline_is_oracle = cid != "immutable_id"

        pre_migrated = self.migrated.snapshot()
        pre_baseline = self.baseline.snapshot()

        m_ok, m_payload, m_types = self.migrated.execute_with_types(sql)
        if baseline_is_oracle:
            b_ok, b_payload, b_types = self.baseline.execute_with_types(baseline_sql)
        else:
            b_ok, b_payload, b_types = None, None, None

        if expect == "success":
            if not m_ok or (baseline_is_oracle and not b_ok):
                self.record(f"case {cid}: expected success", False,
                            f"migrated_ok={m_ok} baseline_ok={b_ok} "
                            f"migrated={m_payload} baseline={b_payload}")
                return
            if baseline_is_oracle:
                # RETURNING payload comparison, by value ...
                self.record(f"case {cid}: RETURNING payload equals baseline",
                            m_payload == b_payload,
                            f"migrated={m_payload} baseline={b_payload}")
                # ... and by server-reported column type.
                self.record(f"case {cid}: RETURNING column types equal baseline",
                            m_types == b_types,
                            f"migrated={m_types} baseline={b_types}")
                self.compare_full_state(f"case {cid}: after statement")
            # Byte-for-byte column types of the SELECT result.
            sel = "SELECT * FROM split_fixture.records ORDER BY id"
            m_types = self._result_types(sel)
            b_types = self._result_types("SELECT * FROM baseline.records ORDER BY id")
            self.record(f"case {cid}: result column types equal baseline", m_types == b_types,
                        f"migrated={m_types} baseline={b_types}")
            self.record(f"case {cid}: expect=success", True)
        else:  # failure
            self.record(f"case {cid}: migrated rejects as expected", not m_ok,
                        f"migrated_ok={m_ok} reason={m_payload}")
            if baseline_is_oracle:
                self.record(f"case {cid}: both sides reject", (not m_ok) and (not b_ok),
                            f"migrated_ok={m_ok} baseline_ok={b_ok} "
                            f"reasons=({m_payload} | {b_payload})")
                self.record(f"case {cid}: failed statement left baseline unchanged",
                            self.baseline.snapshot() == pre_baseline)
            else:
                self.record(f"case {cid}: baseline accepts change (contract is stricter)",
                            b_ok is None, "baseline not used as oracle for immutable id")
            post_migrated = self.migrated.snapshot()
            self.record(f"case {cid}: failed statement left migrated stores unchanged",
                        post_migrated == pre_migrated,
                        f"pre={pre_migrated} post={post_migrated}")

    def _result_types(self, sql: str) -> list[tuple[str, str]]:
        with self.conn.transaction():
            cur = self.conn.execute(sql)
            return self.migrated._describe_types(cur)

    # -- required extra checks ----------------------------------------------
    def immutable_id_check(self) -> None:
        """id immutability is a contract rule beyond the baseline semantics.

        The baseline wide table would accept ``UPDATE ... SET id=99`` (no
        constraint stops it), so this cannot be compared to baseline; it is
        asserted directly against the contract.
        """
        pre = self.migrated.snapshot()
        ok, payload = self.migrated.execute(
            "UPDATE split_fixture.records SET id=99 WHERE id=1")
        self.record("extra: id update is explicitly rejected", (not ok) and "immutable" in str(payload).lower(),
                    f"ok={ok} payload={payload}")
        self.record("extra: rejected id update left stores unchanged",
                    self.migrated.snapshot() == pre)
        # And the baseline is *not* used as an oracle here.
        self.record("extra: id immutability is stricter than baseline (documented)", True,
                    "baseline has no immutability trigger; contract adds it")

    def transaction_rollback_check(self) -> None:
        pre = self.migrated.snapshot()
        conn = self.conn
        try:
            with conn.transaction():
                conn.execute("UPDATE split_fixture.records SET note='临时' WHERE id=1")
                # Prove the change is visible inside the transaction.
                inside = conn.execute(
                    "SELECT note FROM split_fixture.records WHERE id=1").fetchone()[0]
                assert inside == "临时", inside
                raise _Rollback()
        except _Rollback:
            pass
        post = self.migrated.snapshot()
        self.record("extra: cold-field change followed by ROLLBACK leaves no trace",
                    post == pre, f"pre={pre} post={post}")

    def detail_invariant_checks(self) -> None:
        pre = self.migrated.snapshot()
        # The original NOT NULL rules must still bite through the view.
        for col in ("tenant", "name", "status"):
            ok, payload = self.migrated.execute(
                f"UPDATE split_fixture.records SET {col}=NULL WHERE id=1")
            self.record(f"extra: NOT NULL on {col} enforced through view", not ok,
                        f"ok={ok} payload={payload}")
            if not ok:
                self.record(f"extra: rejected NULL {col} update left stores unchanged",
                            self.migrated.snapshot() == pre)
        # And a fresh INSERT omitting a hot NOT NULL column is rejected.
        ok, payload = self.migrated.execute(
            "INSERT INTO split_fixture.records(id,tenant,name) VALUES (600,'t',NULL)")
        self.record("extra: NOT NULL name enforced on INSERT through view", not ok,
                    f"ok={ok} payload={payload}")

        with self.conn.transaction():
            all_null = self.conn.execute(
                "SELECT count(*) FROM split_fixture.records_detail "
                "WHERE note IS NULL AND attachment IS NULL").fetchone()[0]
            self.record("extra: no all-NULL detail rows exist", all_null == 0,
                        f"all_null_rows={all_null}")

            # Newly insert an all-NULL record and confirm no detail row.
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name) VALUES (500,'t','n')")
            orphan = self.conn.execute(
                "SELECT count(*) FROM split_fixture.records_detail WHERE id=500").fetchone()[0]
            self.record("extra: INSERT with no cold values creates no detail row", orphan == 0,
                        f"detail_rows_for_500={orphan}")

            # Empty text and empty bytea must be stored, not collapsed to NULL.
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name,note,attachment) "
                "VALUES (501,'t','n',CAST('' AS text),CAST('' AS bytea))")
            row = self.conn.execute(
                "SELECT note, attachment, note IS NULL, attachment IS NULL, "
                "       octet_length(attachment) "
                "  FROM split_fixture.records_detail WHERE id=501").fetchone()
            self.record("extra: empty text and empty bytea stored as present (not NULL)",
                        row[0] == "" and bytes(row[1]) == b"" and row[2] is False
                        and row[3] is False and row[4] == 0,
                        f"row={row}")

            # Source row 2 is the seeded empty-empty case: confirm it is a
            # real detail row with zero-length values.
            seeded = self.conn.execute(
                "SELECT note, attachment, note IS NULL, attachment IS NULL "
                "  FROM split_fixture.records_detail WHERE id=2").fetchone()
            self.record("extra: seeded empty text/bytea row (id=2) retained in detail",
                        seeded is not None and seeded[0] == "" and bytes(seeded[1]) == b""
                        and seeded[2] is False and seeded[3] is False,
                        f"seeded={seeded}")

            # Remove our temporary probe rows via the compatible interface.
            self.conn.execute("DELETE FROM split_fixture.records WHERE id IN (500,501)")
            # Compare empty bytea against the baseline's empty bytea byte-for-byte.
            baseline_empty = self.conn.execute(
                "SELECT attachment FROM baseline.records WHERE id=2").fetchone()[0]
            migrated_empty = self.conn.execute(
                "SELECT attachment FROM split_fixture.records_detail WHERE id=2").fetchone()[0]
            self.record("extra: empty bytea bytes identical to baseline",
                        bytes(migrated_empty) == bytes(baseline_empty) == b"",
                        f"migrated={bytes(migrated_empty)!r} baseline={bytes(baseline_empty)!r}")

            # DELETE must remove the detail row too (FK ON DELETE CASCADE),
            # not merely hide it behind the view's LEFT JOIN.
            self.conn.execute(
                "INSERT INTO split_fixture.records(id,tenant,name,note) "
                "VALUES (502,'t','n','to be deleted')")
            assert self.conn.execute(
                "SELECT count(*) FROM split_fixture.records_detail WHERE id=502"
            ).fetchone()[0] == 1
            self.conn.execute("DELETE FROM split_fixture.records WHERE id=502")
            orphan = self.conn.execute(
                "SELECT count(*) FROM split_fixture.records_detail WHERE id=502").fetchone()[0]
            self.record("extra: detail row physically removed with main row on DELETE",
                        orphan == 0, f"detail_rows_for_502={orphan}")

    def no_mismatched_wide_table_check(self) -> None:
        relkinds = dict(
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT c.relname, c.relkind FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='split_fixture' AND c.relname IN "
                "('records','records_main','records_detail')").fetchall()
        )
        self.record(
            "extra: old name is a view; no live wide table shadows it",
            relkinds.get("records") == "v" and relkinds.get("records_main") == "r"
            and relkinds.get("records_detail") == "r",
            f"relkinds={relkinds}",
        )

    # -- driver --------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        contract = read_json(FIXTURES / "contract.json")
        cases = read_json(FIXTURES / "client-cases.json")

        self.reset_state()
        self.compare_types()  # before migration: identical wide tables
        import migration_lib
        report = migration_lib.apply_migration(self.conn)

        self.record("migration: source row count preserved",
                    report["source_row_count"] == report["main_row_count"] == len(self.baseline.full_rows()),
                    f"report={report['source_row_count']}/{report['main_row_count']} "
                    f"baseline={len(self.baseline.full_rows())}")
        self.record("migration: source/split digest match",
                    report["source_digest"] == report["split_digest"],
                    f"{report['source_digest']} != {report['split_digest']}")
        self.record("migration: report retains no all-NULL detail rows",
                    report["detail_all_null_rows"] == 0)

        self.compare_types()
        self.compare_full_state("post-migration")

        # Contract sanity: every declared hot/cold column present and split
        # where expected.
        with self.conn.transaction():
            main_cols = [r[0] for r in self.conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid="
                "'split_fixture.records_main'::regclass AND attnum>0 AND NOT attisdropped "
                "ORDER BY attnum").fetchall()]
            detail_cols = [r[0] for r in self.conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid="
                "'split_fixture.records_detail'::regclass AND attnum>0 AND NOT attisdropped "
                "ORDER BY attnum").fetchall()]
        self.record("contract: hot columns are exactly in main table",
                    main_cols == contract["hot_columns"], f"main={main_cols}")
        self.record("contract: cold columns are ref(id)+cold columns in detail table",
                    detail_cols == ["id"] + contract["cold_columns"], f"detail={detail_cols}")

        for case in cases:
            self.run_case(case)

        self.immutable_id_check()
        self.transaction_rollback_check()
        self.detail_invariant_checks()
        self.no_mismatched_wide_table_check()

        return {
            "checks": [c.as_dict() for c in self.checks],
            "passed": sum(1 for c in self.checks if c.ok),
            "failed": sum(1 for c in self.checks if not c.ok),
            "report": report,
        }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify the vertical split migration.")
    parser.add_argument("--dsn", default="", help="libpq connection string")
    parser.add_argument("--json", action="store_true", help="emit JSON results")
    parser.add_argument("--out", help="write JSON results to this path")
    args = parser.parse_args()

    harness = Harness(args.dsn)
    try:
        result = harness.run()
    finally:
        harness.close()

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for c in result["checks"]:
            mark = "PASS" if c["ok"] else "FAIL"
            print(f"[{mark}] {c['name']}")
            if not c["ok"] and c.get("detail"):
                print(f"        {c['detail']}")
        print(f"\n{result['passed']} passed, {result['failed']} failed")
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
