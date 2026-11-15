-- ============================================================================
-- Post-migration inspection / audit.
--
-- Run after sql/migrate.sql on the same database:
--     psql -v ON_ERROR_STOP=1 -d <db> -f sql/inspect.sql
--
-- Read-only.  Confirms:
--   * the old name is a writable view and the split tables exist;
--   * no all-NULL detail row exists;
--   * empty text / empty bytea are stored as real values, not NULL;
--   * the retained report's counts and digests.
-- ============================================================================

\echo '== objects at the old name =='
SELECT c.relname, c.relkind,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'r' THEN 'table' END AS kind
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'split_fixture'
   AND c.relname IN ('records', 'records_main', 'records_detail', 'migration_report')
 ORDER BY c.relname;

\echo '== compatibility view columns (must match the old wide table) =='
SELECT ordinal_position, column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = 'split_fixture' AND table_name = 'records'
 ORDER BY ordinal_position;

\echo '== all-NULL detail rows (must be 0) =='
SELECT count(*) AS all_null_detail_rows
  FROM split_fixture.records_detail
 WHERE note IS NULL AND attachment IS NULL;

\echo '== detail rows whose cold values are empty but present =='
SELECT id,
       note IS NOT NULL       AS note_present,
       octet_length(note)     AS note_bytes,
       attachment IS NOT NULL AS attachment_present,
       octet_length(attachment) AS attachment_bytes,
       encode(attachment, 'hex') AS attachment_hex
  FROM split_fixture.records_detail
 WHERE note = '' OR attachment = ''::bytea
 ORDER BY id;

\echo '== every main row joined back through the old name (sanity) =='
SELECT count(*) AS view_rows FROM split_fixture.records;

\echo '== retained migration report =='
SELECT report_id, migrated_at, source_row_count, main_row_count,
       detail_row_count, detail_all_null_rows,
       source_digest, split_digest,
       source_digest = split_digest AS digest_match
  FROM split_fixture.migration_report
 ORDER BY report_id DESC;
