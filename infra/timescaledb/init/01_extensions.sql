-- =============================================================================
-- 01_extensions.sql — runs once, on first container start, as the superuser.
-- The official timescaledb image executes every *.sql in this directory in
-- alphabetical order against the POSTGRES_DB database.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- pg_stat_statements gives us query-level visibility; the AIOps platform monitors
-- its own storage layer too.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Used for the `gen_random_uuid()` fallback in a few default values.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    RAISE NOTICE 'TimescaleDB extension version: %',
        (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb');
END
$$;
