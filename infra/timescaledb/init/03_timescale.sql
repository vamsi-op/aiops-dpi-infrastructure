-- =============================================================================
-- 03_timescale.sql — turn service_metrics into a hypertable, add rollups,
-- compression and retention.
--
-- Why this matters: at a 15s scrape interval across five services we write
-- ~29k rows/day/service. Continuous aggregates let the dashboard and the
-- forecasting model read pre-computed 1-minute and 1-hour buckets instead of
-- scanning raw data, which keeps queries fast as history grows.
-- =============================================================================

-- 1-day chunks: small enough to compress and drop cheaply, large enough that
-- typical dashboard queries touch only one or two chunks.
--
-- `by_range(...)` is the current TimescaleDB 2.13+ API. It is unambiguous, unlike
-- the older positional form which can resolve to the wrong overload.
--
-- No space (hash) dimension is added on purpose: Timescale's own guidance is that
-- space partitioning helps multi-node and multi-disk setups, and only adds planning
-- overhead on a single node. A composite index on (service, timestamp) gives us the
-- per-service query performance we actually need.
SELECT create_hypertable(
    'service_metrics'::regclass,
    by_range('timestamp', INTERVAL '1 day'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS service_metrics_service_time_idx
    ON service_metrics (service, timestamp DESC);

-- -----------------------------------------------------------------------------
-- 1-minute rollup — what the dashboard's live charts read.
--
-- Only parallelisable aggregates are allowed inside a continuous aggregate, so
-- avg/min/max/sum/count only. Ordered-set aggregates such as
-- `percentile_disc(...) WITHIN GROUP (...)` are rejected by TimescaleDB here.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS service_metrics_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', "timestamp") AS bucket,
    service,
    platform,
    avg(tps)                      AS avg_tps,
    max(tps)                      AS max_tps,
    sum(request_count)            AS request_count,
    sum(error_count)              AS error_count,
    avg(error_ratio)              AS avg_error_ratio,
    max(error_ratio)              AS max_error_ratio,
    avg(latency_p50_seconds)      AS avg_latency_p50_seconds,
    avg(latency_p95_seconds)      AS avg_latency_p95_seconds,
    avg(latency_p99_seconds)      AS avg_latency_p99_seconds,
    max(latency_p99_seconds)      AS max_latency_p99_seconds,
    avg(cpu_utilisation_ratio)    AS avg_cpu_utilisation_ratio,
    max(cpu_utilisation_ratio)    AS max_cpu_utilisation_ratio,
    avg(memory_utilisation_ratio) AS avg_memory_utilisation_ratio,
    max(memory_utilisation_ratio) AS max_memory_utilisation_ratio,
    avg(queue_depth)              AS avg_queue_depth,
    max(queue_depth)              AS max_queue_depth,
    avg(active_connections)       AS avg_active_connections,
    avg(settlement_lag_seconds)   AS avg_settlement_lag_seconds,
    max(settlement_lag_seconds)   AS max_settlement_lag_seconds,
    max(replicas)                 AS replicas,
    count(*)                      AS sample_count
FROM service_metrics
GROUP BY bucket, service, platform
WITH NO DATA;

-- Refresh recent history every minute, leaving the last 30s alone so we never
-- aggregate a half-written bucket.
SELECT add_continuous_aggregate_policy(
    'service_metrics_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '30 seconds',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

-- -----------------------------------------------------------------------------
-- 1-hour rollup — the training set for the traffic prediction model.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS service_metrics_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', "timestamp") AS bucket,
    service,
    platform,
    avg(tps)                      AS avg_tps,
    max(tps)                      AS max_tps,
    min(tps)                      AS min_tps,
    sum(request_count)            AS request_count,
    sum(error_count)              AS error_count,
    avg(error_ratio)              AS avg_error_ratio,
    avg(latency_p99_seconds)      AS avg_latency_p99_seconds,
    avg(cpu_utilisation_ratio)    AS avg_cpu_utilisation_ratio,
    avg(memory_utilisation_ratio) AS avg_memory_utilisation_ratio,
    max(replicas)                 AS replicas,
    count(*)                      AS sample_count
FROM service_metrics
GROUP BY bucket, service, platform
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'service_metrics_1h',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '10 minutes',
    if_not_exists     => TRUE
);

-- -----------------------------------------------------------------------------
-- Compression: raw rows older than 2 days compress roughly 10x. Segmenting by
-- service keeps per-service queries efficient on compressed chunks.
-- -----------------------------------------------------------------------------
ALTER TABLE service_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'service, instance',
    timescaledb.compress_orderby   = 'timestamp DESC'
);

SELECT add_compression_policy('service_metrics', INTERVAL '2 days', if_not_exists => TRUE);

-- -----------------------------------------------------------------------------
-- Retention: 30 days of raw samples is plenty — the 1h rollup keeps the long
-- term shape the forecasting model needs. Adjust for production.
-- -----------------------------------------------------------------------------
SELECT add_retention_policy('service_metrics', INTERVAL '30 days', if_not_exists => TRUE);

-- -----------------------------------------------------------------------------
-- Convenience view: the newest sample per service. The dashboard's "right now"
-- panel and the healing engine's pre-flight checks both use this.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW service_metrics_latest AS
SELECT DISTINCT ON (service, instance) *
FROM service_metrics
WHERE "timestamp" > now() - INTERVAL '10 minutes'
ORDER BY service, instance, "timestamp" DESC;

COMMENT ON VIEW service_metrics_latest IS
    'Most recent sample per service/instance within the last 10 minutes.';
