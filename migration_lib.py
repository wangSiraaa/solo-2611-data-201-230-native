"""Helpers shared by the migration CLI and the verification harness.

The migration is driven entirely by ``sql/migrate.sql``: it opens its own
``BEGIN``/``COMMIT`` so that prechecks, data copy, the name swap and the report
insert are one atomic unit.  This module simply executes that script and reads
back the persisted :class:`migration_report` row.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import psycopg

ROOT = pathlib.Path(__file__).resolve().parent
MIGRATION_SQL_PATH = ROOT / "sql" / "migrate.sql"

REPORT_QUERY = """
SELECT report_id, migrated_at, source_relation, source_kind,
       source_row_count, main_row_count, detail_row_count,
       detail_all_null_rows, source_digest, split_digest,
       contract, structure
  FROM split_fixture.migration_report
 ORDER BY report_id DESC
 LIMIT 1
"""


def migration_sql() -> str:
    return MIGRATION_SQL_PATH.read_text(encoding="utf-8")


def apply_migration(conn: psycopg.Connection) -> dict[str, Any]:
    """Run the migration script on ``conn`` and return the persisted report.

    ``conn`` must be opened with ``autocommit=True``.  On success the script's
    own ``COMMIT`` has already committed everything; on error the caller sees
    the exception and, by closing the connection, the server rolls back the
    aborted transaction.  No partial state survives either way.
    """
    try:
        conn.execute(migration_sql())
    except Exception:
        # The script runs an explicit BEGIN; if it fails mid-transaction the
        # session is left aborted.  Roll back so the original wide table is
        # immediately usable again on this connection.
        conn.rollback()
        raise

    row = conn.execute(REPORT_QUERY).fetchone()
    if row is None:
        raise RuntimeError("migration_report row was not written")
    return {
        "report_id": row[0],
        "migrated_at": row[1].isoformat(),
        "source_relation": row[2],
        "source_kind": row[3],
        "source_row_count": row[4],
        "main_row_count": row[5],
        "detail_row_count": row[6],
        "detail_all_null_rows": row[7],
        "source_digest": row[8],
        "split_digest": row[9],
        "contract": row[10],
        "structure": row[11],
    }


def dumps_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
