#!/usr/bin/env python3
"""Migration command for the split_fixture.records vertical split.

Usage:
    python migrate.py --dsn "dbname=split_fixture_db" [--json]

The connection must already contain the wide table produced by
``fixtures/schema.sql``.  The whole migration runs in one transaction; on
failure no objects are left behind and the old wide table stays usable.

Connection settings unspecified by ``--dsn`` fall back to the standard
PostgreSQL environment variables (PGHOST, PGPORT, PGUSER, PGPASSWORD).
"""

from __future__ import annotations

import argparse
import sys

import psycopg

import migration_lib


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn",
        default="",
        help="libpq connection string for the target database "
        "(default: standard PG* environment variables)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the post-migration report as JSON instead of a summary",
    )
    args = parser.parse_args(argv)

    conn = psycopg.connect(args.dsn, autocommit=True)
    try:
        report = migration_lib.apply_migration(conn)
    except psycopg.Error as exc:
        conn.close()
        print(f"migration FAILED and was rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        if not conn.closed:
            conn.close()

    if args.json:
        print(migration_lib.dumps_report(report))
    else:
        print("migration committed")
        print(f"  source rows : {report['source_row_count']}")
        print(f"  main rows   : {report['main_row_count']}")
        print(f"  detail rows : {report['detail_row_count']}")
        print(f"  all-null detail rows : {report['detail_all_null_rows']}")
        print(f"  source digest : {report['source_digest']}")
        print(f"  split  digest : {report['split_digest']}")
        print("  report table  : split_fixture.migration_report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
