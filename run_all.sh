#!/usr/bin/env bash
# ============================================================================
# One-shot reproduction: create throwaway databases, run the migration and
# both verification suites, and print the retained reports.
#
# Prerequisites: PostgreSQL 16 binaries on PATH and a reachable server.
# Override connection settings with the standard PG* environment variables
# (PGHOST, PGPORT, PGUSER, PGPASSWORD) and optionally:
#   KEEP_DB=1     keep the created databases
#   PGDATABASE_*  names below
#
# This script only touches the databases it creates.  It never calls initdb
# or stops a server, so it works with any local PostgreSQL 16 instance.
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

MIGRATE_DB="${MIGRATE_DB:-split_fixture_migrate}"
VERIFY_DB="${VERIFY_DB:-split_fixture_verify}"
SAFETY_DB="${SAFETY_DB:-split_fixture_safety}"

run_psql() { psql -v ON_ERROR_STOP=1 "$@"; }
recreate() {
    local db="$1"
    run_psql -d postgres -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" >/dev/null
    run_psql -d postgres -c "CREATE DATABASE \"$db\"" >/dev/null
}

cleanup() {
    if [ "${KEEP_DB:-0}" != "1" ]; then
        for db in "$MIGRATE_DB" "$VERIFY_DB" "$SAFETY_DB"; do
            run_psql -d postgres -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" >/dev/null 2>&1 || true
        done
    fi
}
trap cleanup EXIT

mkdir -p "$HERE/output"

echo "==> [1/4] applying fixtures/schema.sql to $MIGRATE_DB"
recreate "$MIGRATE_DB"
run_psql -d "$MIGRATE_DB" -f "$HERE/fixtures/schema.sql" >/dev/null

echo "==> [2/4] running the migration (sql/migrate.sql via psql -f)"
run_psql -d "$MIGRATE_DB" -f "$HERE/sql/migrate.sql" \
    | tee "$HERE/output/migrate-psql.log"

echo "==> [2b]  migration command via migrate.py on a fresh database"
recreate "$MIGRATE_DB" >/dev/null
run_psql -d "$MIGRATE_DB" -f "$HERE/fixtures/schema.sql" >/dev/null
"$PY" "$HERE/migrate.py" --dsn "dbname=$MIGRATE_DB" | tee "$HERE/output/migrate-cli.log"

echo "==> [3/4] client-contract + baseline comparison (verify.py)"
recreate "$VERIFY_DB"
"$PY" "$HERE/verify.py" --dsn "dbname=$VERIFY_DB" \
    --out "$HERE/output/verify.json" | tee "$HERE/output/verify.log"

echo "==> [4/4] precheck / failure-recovery safety tests (test_safety.py)"
recreate "$SAFETY_DB"
"$PY" "$HERE/test_safety.py" --dsn "dbname=$SAFETY_DB" \
    --out "$HERE/output/safety.json" | tee "$HERE/output/safety.log"

echo
echo "==> retained migration report ($MIGRATE_DB)"
run_psql -d "$MIGRATE_DB" -c "
SELECT report_id, source_row_count, main_row_count, detail_row_count,
       detail_all_null_rows, source_digest = split_digest AS digest_match
  FROM split_fixture.migration_report"
echo "Reports on disk: $HERE/output/{migrate-psql.log,migrate-cli.log,verify.json,safety.json}"
