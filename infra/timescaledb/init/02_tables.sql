-- =============================================================================
-- 02_tables.sql — core schema.
--
-- Conventions:
--   * every timestamp is `timestamptz`, always stored in UTC
--   * enum-like columns are TEXT + CHECK, so adding a value is a one-line migration
--   * JSONB is used where the shape is genuinely open (labels, parameters, snapshots)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- service_metrics — the raw telemetry time series. Becomes a hypertable in 03.
-- One row per service, per instance, per scrape window.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS service_metrics (
    timestamp                   timestamptz      NOT NULL,
    service                     text             NOT NULL,
    instance                    text             NOT NULL DEFAULT 'unknown',
    platform                    text             NOT NULL DEFAULT 'mojaloop',

    -- traffic
    tps                         double precision NOT NULL DEFAULT 0,
    request_count               bigint           NOT NULL DEFAULT 0,
    error_count                 bigint           NOT NULL DEFAULT 0,
    error_ratio                 double precision NOT NULL DEFAULT 0,

    -- latency, seconds
    latency_p50_seconds         double precision NOT NULL DEFAULT 0,
    latency_p95_seconds         double precision NOT NULL DEFAULT 0,
    latency_p99_seconds         double precision NOT NULL DEFAULT 0,

    -- saturation
    cpu_utilisation_ratio       double precision NOT NULL DEFAULT 0,
    memory_utilisation_ratio    double precision NOT NULL DEFAULT 0,
    queue_depth                 double precision NOT NULL DEFAULT 0,
    active_connections          double precision NOT NULL DEFAULT 0,

    -- DPI-specific business signals
    settlement_lag_seconds      double precision NOT NULL DEFAULT 0,
    quote_latency_seconds       double precision NOT NULL DEFAULT 0,
    verification_latency_seconds double precision NOT NULL DEFAULT 0,

    replicas                    integer          NOT NULL DEFAULT 1,
    labels                      jsonb            NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT service_metrics_pkey PRIMARY KEY (timestamp, service, instance)
);

COMMENT ON TABLE service_metrics IS
    'Normalised per-service telemetry. Feature store for anomaly detection and forecasting.';

-- -----------------------------------------------------------------------------
-- anomalies — output of the anomaly detector.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id            text             PRIMARY KEY,
    detected_at           timestamptz      NOT NULL DEFAULT now(),
    window_start          timestamptz,

    service               text             NOT NULL,
    platform              text             NOT NULL DEFAULT 'mojaloop',
    kind                  text             NOT NULL DEFAULT 'multivariate',
    severity              text             NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('info', 'warning', 'critical')),

    score                 double precision NOT NULL CHECK (score >= 0 AND score <= 1),
    raw_score             double precision,
    threshold             double precision NOT NULL DEFAULT 0.65,

    contributing_features text[]           NOT NULL DEFAULT '{}',
    feature_snapshot      jsonb            NOT NULL DEFAULT '{}'::jsonb,
    feature_deviations    jsonb            NOT NULL DEFAULT '{}'::jsonb,

    model_name            text             NOT NULL DEFAULT 'isolation-forest',
    model_version         text             NOT NULL DEFAULT '0',
    description           text             NOT NULL DEFAULT '',

    incident_id           text
);

CREATE INDEX IF NOT EXISTS anomalies_detected_at_idx  ON anomalies (detected_at DESC);
CREATE INDEX IF NOT EXISTS anomalies_service_time_idx ON anomalies (service, detected_at DESC);
CREATE INDEX IF NOT EXISTS anomalies_incident_idx     ON anomalies (incident_id)
    WHERE incident_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- traffic_predictions — forecasts + the scaling recommendation derived from them.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS traffic_predictions (
    prediction_id        text             PRIMARY KEY,
    created_at           timestamptz      NOT NULL DEFAULT now(),

    service              text             NOT NULL,
    platform             text             NOT NULL DEFAULT 'mojaloop',
    horizon_hours        integer          NOT NULL CHECK (horizon_hours > 0),

    current_tps          double precision NOT NULL DEFAULT 0,
    peak_predicted_tps   double precision NOT NULL DEFAULT 0,
    peak_at              timestamptz,
    points               jsonb            NOT NULL DEFAULT '[]'::jsonb,

    current_replicas     integer          NOT NULL DEFAULT 1,
    recommended_replicas integer          NOT NULL DEFAULT 1,
    tps_per_replica      double precision NOT NULL DEFAULT 50,
    scaling_action       text             NOT NULL DEFAULT 'hold'
        CHECK (scaling_action IN ('scale_out', 'scale_in', 'hold')),

    model_name           text             NOT NULL DEFAULT 'seasonal-naive',
    model_version        text             NOT NULL DEFAULT '0',
    backtest_mape        double precision
);

CREATE INDEX IF NOT EXISTS predictions_service_time_idx
    ON traffic_predictions (service, horizon_hours, created_at DESC);

-- -----------------------------------------------------------------------------
-- deployments — the `change` axis of root cause analysis.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id text        PRIMARY KEY,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    service       text        NOT NULL,
    platform      text        NOT NULL DEFAULT 'mojaloop',
    from_version  text,
    to_version    text        NOT NULL,
    change_type   text        NOT NULL DEFAULT 'deploy'
        CHECK (change_type IN ('deploy', 'rollback', 'config', 'scale')),
    triggered_by  text        NOT NULL DEFAULT 'unknown',
    notes         text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS deployments_service_time_idx
    ON deployments (service, occurred_at DESC);

-- -----------------------------------------------------------------------------
-- incidents — correlated anomalies + ranked root cause hypotheses.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    incident_id       text        PRIMARY KEY,
    opened_at         timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    status            text        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'mitigating', 'resolved', 'acknowledged')),
    severity          text        NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('info', 'warning', 'critical')),

    platform          text        NOT NULL DEFAULT 'mojaloop',
    title             text        NOT NULL DEFAULT '',
    affected_services text[]      NOT NULL DEFAULT '{}',
    anomaly_ids       text[]      NOT NULL DEFAULT '{}',

    hypotheses        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    action_ids        text[]      NOT NULL DEFAULT '{}',
    ai_summary        text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS incidents_opened_at_idx ON incidents (opened_at DESC);
CREATE INDEX IF NOT EXISTS incidents_open_idx      ON incidents (status)
    WHERE status IN ('open', 'mitigating');

-- -----------------------------------------------------------------------------
-- healing_actions — audit trail of every remediation, executed or not.
-- The `skipped_*` statuses are as important as the successes: they prove the
-- safety rails work.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS healing_actions (
    action_id       text             PRIMARY KEY,
    created_at      timestamptz      NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,

    playbook        text             NOT NULL,
    target_service  text             NOT NULL,
    target_instance text,
    platform        text             NOT NULL DEFAULT 'mojaloop',

    incident_id     text REFERENCES incidents (incident_id) ON DELETE SET NULL,
    anomaly_id      text,
    trigger_reason  text             NOT NULL DEFAULT '',
    confidence      double precision NOT NULL DEFAULT 0,

    -- Must list every aiops_common.constants.ActionStatus value; tests/test_schema_enums.py
    -- fails if the two drift. An existing database needs
    -- infra/timescaledb/migrations/001_healing_action_statuses.sql.
    status          text             NOT NULL DEFAULT 'proposed'
        CONSTRAINT healing_actions_status_check
        CHECK (status IN ('proposed', 'skipped_dry_run', 'skipped_cooldown',
                          'skipped_budget', 'skipped_low_confidence',
                          'skipped_insufficient_capacity', 'skipped_min_replicas',
                          'skipped_max_replicas', 'skipped_max_attempts',
                          'skipped_global_limit', 'skipped_downstream_unhealthy',
                          'requires_manual_intervention',
                          'running', 'succeeded', 'failed')),
    dry_run         boolean          NOT NULL DEFAULT true,
    execution_mode  text             NOT NULL DEFAULT 'simulated',
    actual_replicas_before   integer,
    actual_replicas_after    integer,
    simulated_replicas_after integer,
    parameters      jsonb            NOT NULL DEFAULT '{}'::jsonb,
    result_message  text             NOT NULL DEFAULT '',
    before_snapshot jsonb            NOT NULL DEFAULT '{}'::jsonb,
    after_snapshot  jsonb            NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS actions_created_at_idx ON healing_actions (created_at DESC);
CREATE INDEX IF NOT EXISTS actions_target_idx     ON healing_actions (target_service, created_at DESC);
CREATE INDEX IF NOT EXISTS actions_incident_idx   ON healing_actions (incident_id)
    WHERE incident_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- chaos_runs — injected failures and how fast the platform reacted.
-- This table is the scorecard for the live demo.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chaos_runs (
    run_id              text        PRIMARY KEY,
    started_at          timestamptz NOT NULL DEFAULT now(),
    ended_at            timestamptz,

    experiment          text        NOT NULL,
    target_service      text        NOT NULL,
    platform            text        NOT NULL DEFAULT 'mojaloop',
    duration_seconds    integer     NOT NULL DEFAULT 60,
    parameters          jsonb       NOT NULL DEFAULT '{}'::jsonb,

    status              text        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'aborted', 'failed')),
    detected_at         timestamptz,
    detected_anomaly_id text,
    incident_id         text,
    remediated_at       timestamptz,
    notes               text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS chaos_started_at_idx ON chaos_runs (started_at DESC);

-- -----------------------------------------------------------------------------
-- model_runs — training audit log, so model quality is traceable over time.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_runs (
    run_id           text             PRIMARY KEY,
    created_at       timestamptz      NOT NULL DEFAULT now(),
    model_name       text             NOT NULL,
    model_version    text             NOT NULL,
    service          text,
    training_rows    bigint           NOT NULL DEFAULT 0,
    duration_seconds double precision NOT NULL DEFAULT 0,
    metrics          jsonb            NOT NULL DEFAULT '{}'::jsonb,
    artifact_path    text             NOT NULL DEFAULT '',
    notes            text             NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS model_runs_name_time_idx
    ON model_runs (model_name, created_at DESC);
