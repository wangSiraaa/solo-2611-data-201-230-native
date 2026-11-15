-- ============================================================================
-- split_fixture.records: vertical split migration (stop-the-world window)
--
-- This script is self-contained and performs the whole migration in ONE
-- explicit transaction.  Run it either as
--
--     psql -v ON_ERROR_STOP=1 -d <db> -f sql/migrate.sql
--
-- or through migrate.py (which executes the same script and commits/rolls
-- back as one unit).
--
-- On any error the transaction is rolled back by the caller and the original
-- wide table split_fixture.records remains in place and writable.
--
-- Resulting objects:
--   split_fixture.records_main    hot columns (id, tenant, name, status)
--   split_fixture.records_detail  cold columns (id -> records_main, note,
--                                 attachment), row exists only when note or
--                                 attachment is non-NULL
--   split_fixture.records         writable compatibility view at the old name
--   split_fixture.migration_report auditable structure + counts snapshot
-- ============================================================================

BEGIN;

-- Fail fast instead of queueing behind long readers/writers.
SET LOCAL lock_timeout = '5s';

-- Stop-the-world: block every concurrent writer (and reader) for the rest of
-- the transaction.  The lock is held until COMMIT/ROLLBACK.
LOCK TABLE split_fixture.records IN ACCESS EXCLUSIVE MODE;

DO $migrate$
DECLARE
    -- source relation
    v_oid            oid;
    v_kind           "char";
    v_names          text[];
    v_types          text[];
    v_notnull        boolean[];
    v_defaults       text[];
    v_pk             text[];
    v_status_attnum  smallint;
    v_check_count    integer;
    v_dep_views      integer;
    v_dep_fks        integer;

    -- data movement
    v_src_count      bigint;
    v_main_count     bigint;
    v_detail_count   bigint;
    v_expected_detail bigint;
    v_all_null_detail bigint;
    v_src_digest     text;
    v_split_digest   text;

    -- result
    v_structure      jsonb;
    v_contract       jsonb;
BEGIN
    -- ------------------------------------------------------------------
    -- 1. Input contract checks
    -- ------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'split_fixture') THEN
        RAISE EXCEPTION 'precheck: schema split_fixture does not exist';
    END IF;

    SELECT c.oid, c.relkind
      INTO v_oid, v_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'split_fixture'
       AND c.relname = 'records';

    IF v_oid IS NULL THEN
        RAISE EXCEPTION 'precheck: source relation split_fixture.records does not exist';
    END IF;

    -- A rerun on an already-migrated schema: the old name is the
    -- compatibility view and the split tables are present.
    IF v_kind = 'v'
       AND to_regclass('split_fixture.records_main') IS NOT NULL
       AND to_regclass('split_fixture.records_detail') IS NOT NULL THEN
        RAISE EXCEPTION
            'precheck: split_fixture is already migrated (records is a view and split tables exist); refusing to run twice';
    END IF;

    IF v_kind <> 'r' THEN
        RAISE EXCEPTION 'precheck: split_fixture.records must be a plain table (relkind=%), found %',
            'r', v_kind;
    END IF;

    SELECT array_agg(a.attname ORDER BY a.attnum),
           array_agg(format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum),
           array_agg(a.attnotnull ORDER BY a.attnum),
           array_agg(COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '') ORDER BY a.attnum)
      INTO v_names, v_types, v_notnull, v_defaults
      FROM pg_attribute a
      LEFT JOIN pg_attrdef ad
             ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
     WHERE a.attrelid = v_oid
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF v_names IS DISTINCT FROM
       ARRAY['id', 'tenant', 'name', 'status', 'note', 'attachment'] THEN
        RAISE EXCEPTION 'precheck: unexpected column names/order %, expected id,tenant,name,status,note,attachment',
            v_names;
    END IF;

    IF v_types IS DISTINCT FROM
       ARRAY['bigint', 'text', 'text', 'text', 'text', 'bytea'] THEN
        RAISE EXCEPTION 'precheck: unexpected column types %, expected bigint,text,text,text,text,bytea',
            v_types;
    END IF;

    IF v_notnull IS DISTINCT FROM
       ARRAY[true, true, true, true, false, false] THEN
        RAISE EXCEPTION 'precheck: unexpected NOT NULL flags %, expected id/tenant/name/status NOT NULL and note/attachment nullable',
            v_notnull;
    END IF;

    IF v_defaults IS DISTINCT FROM
       ARRAY['', '', '', '''active''::text', '', ''] THEN
        RAISE EXCEPTION 'precheck: unexpected column defaults %, expected only status DEFAULT active',
            v_defaults;
    END IF;

    SELECT array_agg(a.attname ORDER BY k.ord)
      INTO v_pk
      FROM pg_constraint c
      CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a
        ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.conrelid = v_oid
       AND c.contype = 'p';

    IF v_pk IS DISTINCT FROM ARRAY['id'] THEN
        RAISE EXCEPTION 'precheck: expected PRIMARY KEY (id), found %', v_pk;
    END IF;

    SELECT a.attnum
      INTO v_status_attnum
      FROM pg_attribute a
     WHERE a.attrelid = v_oid AND a.attname = 'status' AND a.attnum > 0;

    SELECT count(*)
      INTO v_check_count
      FROM pg_constraint c
     WHERE c.conrelid = v_oid
       AND c.contype = 'c'
       AND v_status_attnum = ANY (c.conkey)
       AND pg_get_constraintdef(c.oid) LIKE '%active%'
       AND pg_get_constraintdef(c.oid) LIKE '%disabled%';

    IF v_check_count < 1 THEN
        RAISE EXCEPTION 'precheck: missing status CHECK constraint allowing active/disabled';
    END IF;

    -- ------------------------------------------------------------------
    -- 2. Target object conflict checks
    -- ------------------------------------------------------------------
    IF to_regclass('split_fixture.records_main') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: target table split_fixture.records_main already exists';
    END IF;
    IF to_regclass('split_fixture.records_detail') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: target table split_fixture.records_detail already exists';
    END IF;
    IF to_regclass('split_fixture.migration_report') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: target table split_fixture.migration_report already exists';
    END IF;
    IF to_regprocedure('split_fixture.records_compat_insert()') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: function split_fixture.records_compat_insert() already exists';
    END IF;
    IF to_regprocedure('split_fixture.records_compat_update()') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: function split_fixture.records_compat_update() already exists';
    END IF;
    IF to_regprocedure('split_fixture.records_compat_delete()') IS NOT NULL THEN
        RAISE EXCEPTION 'precheck: function split_fixture.records_compat_delete() already exists';
    END IF;

    -- The source table is dropped by this migration.  Refuse to run if any
    -- other object (incoming FK, dependent view) would silently be affected
    -- or would make DROP fail halfway.
    SELECT count(*)
      INTO v_dep_fks
      FROM pg_constraint
     WHERE confrelid = v_oid;

    IF v_dep_fks > 0 THEN
        RAISE EXCEPTION 'precheck: % foreign key(s) reference split_fixture.records', v_dep_fks;
    END IF;

    SELECT count(*)
      INTO v_dep_views
      FROM pg_depend d
      JOIN pg_rewrite r ON r.oid = d.objid
     WHERE d.classid = 'pg_rewrite'::regclass
       AND d.refclassid = 'pg_class'::regclass
       AND d.refobjid = v_oid
       AND r.ev_class <> v_oid;

    IF v_dep_views > 0 THEN
        RAISE EXCEPTION 'precheck: % dependent view(s) reference split_fixture.records', v_dep_views;
    END IF;

    -- ------------------------------------------------------------------
    -- 3. Create split tables
    -- ------------------------------------------------------------------
    CREATE TABLE split_fixture.records_main (
        id     bigint PRIMARY KEY,
        tenant text NOT NULL,
        name   text NOT NULL,
        status text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'disabled'))
    );

    CREATE TABLE split_fixture.records_detail (
        id         bigint PRIMARY KEY
                   REFERENCES split_fixture.records_main(id) ON DELETE CASCADE,
        note       text,
        attachment bytea,
        CONSTRAINT records_detail_present
            CHECK (note IS NOT NULL OR attachment IS NOT NULL)
    );

    -- ------------------------------------------------------------------
    -- 4. Copy data byte-for-byte.  A detail row exists iff at least one
    --    cold column is non-NULL (empty text / empty bytea are NOT NULL).
    -- ------------------------------------------------------------------
    INSERT INTO split_fixture.records_main (id, tenant, name, status)
    SELECT id, tenant, name, status
      FROM split_fixture.records;

    INSERT INTO split_fixture.records_detail (id, note, attachment)
    SELECT id, note, attachment
      FROM split_fixture.records
     WHERE note IS NOT NULL OR attachment IS NOT NULL;

    SELECT count(*),
           md5(COALESCE(string_agg(md5(row_to_json(t)::text), '' ORDER BY t.id), ''))
      INTO v_src_count, v_src_digest
      FROM (SELECT id, tenant, name, status, note, attachment
              FROM split_fixture.records) t;

    SELECT count(*) INTO v_main_count FROM split_fixture.records_main;
    SELECT count(*) INTO v_detail_count FROM split_fixture.records_detail;
    SELECT count(*) INTO v_expected_detail
      FROM split_fixture.records
     WHERE note IS NOT NULL OR attachment IS NOT NULL;
    SELECT count(*) INTO v_all_null_detail
      FROM split_fixture.records_detail
     WHERE note IS NULL AND attachment IS NULL;

    IF v_main_count <> v_src_count THEN
        RAISE EXCEPTION 'copy check: main row count % <> source %', v_main_count, v_src_count;
    END IF;
    IF v_detail_count <> v_expected_detail THEN
        RAISE EXCEPTION 'copy check: detail row count % <> expected %',
            v_detail_count, v_expected_detail;
    END IF;
    IF v_all_null_detail <> 0 THEN
        RAISE EXCEPTION 'copy check: % detail row(s) with both cold columns NULL',
            v_all_null_detail;
    END IF;

    -- ------------------------------------------------------------------
    -- 5. Compatibility layer (functions created before the old table is
    --    dropped, trigger names only after the view exists).
    -- ------------------------------------------------------------------
    CREATE FUNCTION split_fixture.records_compat_insert()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
        INSERT INTO split_fixture.records_main (id, tenant, name, status)
        VALUES (NEW.id, NEW.tenant, NEW.name, NEW.status);

        -- Only materialise a detail row when there is something to store.
        IF NEW.note IS NOT NULL OR NEW.attachment IS NOT NULL THEN
            INSERT INTO split_fixture.records_detail (id, note, attachment)
            VALUES (NEW.id, NEW.note, NEW.attachment);
        END IF;

        RETURN NEW;
    END;
    $fn$;

    CREATE FUNCTION split_fixture.records_compat_update()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
        -- Contract rule: id is immutable through the old interface.
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION
                'immutable column "id": cannot change id from % to %',
                OLD.id, NEW.id
                USING ERRCODE = '0A000';
        END IF;

        UPDATE split_fixture.records_main
           SET tenant = NEW.tenant,
               name   = NEW.name,
               status = NEW.status
         WHERE id = OLD.id;

        -- Unchanged cold columns arrive as OLD values, so a hot-only UPDATE
        -- keeps an existing detail row untouched.  If both cold values end
        -- up NULL the optional detail row is removed but the main row stays.
        IF NEW.note IS NOT NULL OR NEW.attachment IS NOT NULL THEN
            INSERT INTO split_fixture.records_detail (id, note, attachment)
            VALUES (NEW.id, NEW.note, NEW.attachment)
            ON CONFLICT (id) DO UPDATE
               SET note       = EXCLUDED.note,
                   attachment = EXCLUDED.attachment;
        ELSE
            DELETE FROM split_fixture.records_detail WHERE id = OLD.id;
        END IF;

        RETURN NEW;
    END;
    $fn$;

    CREATE FUNCTION split_fixture.records_compat_delete()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
        -- ON DELETE CASCADE removes the optional detail row.
        DELETE FROM split_fixture.records_main WHERE id = OLD.id;
        RETURN OLD;
    END;
    $fn$;

    -- ------------------------------------------------------------------
    -- 6. Swap the old name: drop the wide table, expose a writable view.
    --    Both happen in the same transaction, so a failure restores the
    --    original wide table.
    -- ------------------------------------------------------------------
    DROP TABLE split_fixture.records;

    CREATE VIEW split_fixture.records AS
    SELECT m.id,
           m.tenant,
           m.name,
           m.status,
           d.note,
           d.attachment
      FROM split_fixture.records_main AS m
      LEFT JOIN split_fixture.records_detail AS d ON d.id = m.id;

    -- Preserve the old INSERT default when status is omitted.
    ALTER VIEW split_fixture.records ALTER COLUMN status SET DEFAULT 'active';

    CREATE TRIGGER records_compat_insert
        INSTEAD OF INSERT ON split_fixture.records
        FOR EACH ROW EXECUTE FUNCTION split_fixture.records_compat_insert();

    CREATE TRIGGER records_compat_update
        INSTEAD OF UPDATE ON split_fixture.records
        FOR EACH ROW EXECUTE FUNCTION split_fixture.records_compat_update();

    CREATE TRIGGER records_compat_delete
        INSTEAD OF DELETE ON split_fixture.records
        FOR EACH ROW EXECUTE FUNCTION split_fixture.records_compat_delete();

    -- ------------------------------------------------------------------
    -- 7. Post-migration validation: shape, counts, full-content digest.
    -- ------------------------------------------------------------------
    SELECT array_agg(a.attname ORDER BY a.attnum),
           array_agg(format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum)
      INTO v_names, v_types
      FROM pg_attribute a
     WHERE a.attrelid = 'split_fixture.records'::regclass
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF v_names IS DISTINCT FROM
       ARRAY['id', 'tenant', 'name', 'status', 'note', 'attachment'] THEN
        RAISE EXCEPTION 'postcheck: compatibility view columns %, expected id,tenant,name,status,note,attachment',
            v_names;
    END IF;
    IF v_types IS DISTINCT FROM
       ARRAY['bigint', 'text', 'text', 'text', 'text', 'bytea'] THEN
        RAISE EXCEPTION 'postcheck: compatibility view types %', v_types;
    END IF;

    IF to_regclass('split_fixture.records_main') IS NULL
       OR to_regclass('split_fixture.records_detail') IS NULL THEN
        RAISE EXCEPTION 'postcheck: split tables missing';
    END IF;
    IF (SELECT relkind FROM pg_class
         WHERE oid = 'split_fixture.records'::regclass) <> 'v' THEN
        RAISE EXCEPTION 'postcheck: split_fixture.records is not a view';
    END IF;
    IF (SELECT count(*) FROM pg_trigger
         WHERE tgrelid = 'split_fixture.records'::regclass
           AND NOT tgisinternal) <> 3 THEN
        RAISE EXCEPTION 'postcheck: compatibility view does not have 3 INSTEAD OF triggers';
    END IF;

    SELECT count(*),
           md5(COALESCE(string_agg(md5(row_to_json(t)::text), '' ORDER BY t.id), ''))
      INTO v_main_count, v_split_digest
      FROM (SELECT id, tenant, name, status, note, attachment
              FROM split_fixture.records) t;

    IF v_main_count <> v_src_count THEN
        RAISE EXCEPTION 'postcheck: view row count % <> source %', v_main_count, v_src_count;
    END IF;
    IF v_split_digest IS DISTINCT FROM v_src_digest THEN
        RAISE EXCEPTION 'postcheck: full-content digest mismatch (source %, split %)',
            v_src_digest, v_split_digest;
    END IF;

    SELECT count(*) INTO v_all_null_detail
      FROM split_fixture.records_detail
     WHERE note IS NULL AND attachment IS NULL;
    IF v_all_null_detail <> 0 THEN
        RAISE EXCEPTION 'postcheck: % detail row(s) with both cold columns NULL',
            v_all_null_detail;
    END IF;

    -- ------------------------------------------------------------------
    -- 8. Auditable structure + counts report (retained after commit).
    -- ------------------------------------------------------------------
    SELECT jsonb_build_object(
        'schema', 'split_fixture',
        'main_table', jsonb_build_object(
            'name', 'records_main',
            'columns', (
                SELECT jsonb_agg(jsonb_build_object(
                           'position', a.attnum,
                           'name', a.attname,
                           'type', format_type(a.atttypid, a.atttypmod),
                           'not_null', a.attnotnull,
                           'default', pg_get_expr(d.adbin, d.adrelid))
                       ORDER BY a.attnum)
                  FROM pg_attribute a
                  LEFT JOIN pg_attrdef d
                         ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                 WHERE a.attrelid = 'split_fixture.records_main'::regclass
                   AND a.attnum > 0 AND NOT a.attisdropped),
            'constraints', (
                SELECT jsonb_agg(jsonb_build_object(
                           'name', c.conname,
                           'type', c.contype,
                           'definition', pg_get_constraintdef(c.oid))
                       ORDER BY c.conname)
                  FROM pg_constraint c
                 WHERE c.conrelid = 'split_fixture.records_main'::regclass)
        ),
        'detail_table', jsonb_build_object(
            'name', 'records_detail',
            'columns', (
                SELECT jsonb_agg(jsonb_build_object(
                           'position', a.attnum,
                           'name', a.attname,
                           'type', format_type(a.atttypid, a.atttypmod),
                           'not_null', a.attnotnull,
                           'default', pg_get_expr(d.adbin, d.adrelid))
                       ORDER BY a.attnum)
                  FROM pg_attribute a
                  LEFT JOIN pg_attrdef d
                         ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                 WHERE a.attrelid = 'split_fixture.records_detail'::regclass
                   AND a.attnum > 0 AND NOT a.attisdropped),
            'constraints', (
                SELECT jsonb_agg(jsonb_build_object(
                           'name', c.conname,
                           'type', c.contype,
                           'definition', pg_get_constraintdef(c.oid))
                       ORDER BY c.conname)
                  FROM pg_constraint c
                 WHERE c.conrelid = 'split_fixture.records_detail'::regclass)
        ),
        'compat_view', jsonb_build_object(
            'name', 'split_fixture.records',
            'columns', (
                SELECT jsonb_agg(jsonb_build_object(
                           'position', a.attnum,
                           'name', a.attname,
                           'type', format_type(a.atttypid, a.atttypmod),
                           'not_null', a.attnotnull,
                           'default', pg_get_expr(d.adbin, d.adrelid))
                       ORDER BY a.attnum)
                  FROM pg_attribute a
                  LEFT JOIN pg_attrdef d
                         ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                 WHERE a.attrelid = 'split_fixture.records'::regclass
                   AND a.attnum > 0 AND NOT a.attisdropped),
            'definition', pg_get_viewdef('split_fixture.records'::regclass, true),
            'triggers', (
                SELECT jsonb_agg(jsonb_build_object(
                           'name', t.tgname,
                           'function', p.proname,
                           'timing', CASE
                                         WHEN t.tgtype & 64 <> 0 THEN 'INSTEAD'
                                         WHEN t.tgtype & 2  <> 0 THEN 'BEFORE'
                                         ELSE 'AFTER'
                                     END,
                           'row_level', (t.tgtype & 1) <> 0,
                           'events', concat_ws(',',
                                         CASE WHEN t.tgtype & 4  <> 0 THEN 'INSERT' END,
                                         CASE WHEN t.tgtype & 8  <> 0 THEN 'DELETE' END,
                                         CASE WHEN t.tgtype & 16 <> 0 THEN 'UPDATE' END,
                                         CASE WHEN t.tgtype & 32 <> 0 THEN 'TRUNCATE' END))
                       ORDER BY t.tgname)
                  FROM pg_trigger t
                  JOIN pg_proc p ON p.oid = t.tgfoid
                 WHERE t.tgrelid = 'split_fixture.records'::regclass
                   AND NOT t.tgisinternal)
        )
    ) INTO v_structure;

    v_contract := jsonb_build_object(
        'old_name', 'split_fixture.records',
        'hot_columns', jsonb_build_array('id', 'tenant', 'name', 'status'),
        'cold_columns', jsonb_build_array('note', 'attachment'),
        'detail_presence', 'at least one cold value is non-NULL; empty text and empty bytea count as present',
        'immutable_columns', jsonb_build_array('id'),
        'supported_client_sql', jsonb_build_array(
            'SELECT', 'INSERT with explicit id', 'UPDATE without changing id',
            'DELETE', 'RETURNING'),
        'migration_window', 'no concurrent writers');

    CREATE TABLE split_fixture.migration_report (
        report_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        migrated_at          timestamptz NOT NULL DEFAULT now(),
        source_relation      text        NOT NULL,
        source_kind          text        NOT NULL,
        source_row_count     bigint      NOT NULL,
        main_row_count       bigint      NOT NULL,
        detail_row_count     bigint      NOT NULL,
        detail_all_null_rows bigint      NOT NULL,
        source_digest        text        NOT NULL,
        split_digest         text        NOT NULL,
        contract             jsonb       NOT NULL,
        structure            jsonb       NOT NULL
    );

    INSERT INTO split_fixture.migration_report (
        source_relation, source_kind,
        source_row_count, main_row_count, detail_row_count,
        detail_all_null_rows, source_digest, split_digest,
        contract, structure)
    VALUES (
        'split_fixture.records', 'wide table -> hot table + optional detail table + writable view',
        v_src_count, v_main_count, v_detail_count,
        v_all_null_detail, v_src_digest, v_split_digest,
        v_contract, v_structure);

    RAISE NOTICE
        'migration complete: source_rows=%, main_rows=%, detail_rows=%, digest=%',
        v_src_count, v_main_count, v_detail_count, v_split_digest;
END
$migrate$;

COMMIT;
