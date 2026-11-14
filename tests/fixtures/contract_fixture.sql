-- CI-only fixture for scripts/check_api_contract.py.
--
-- The contract check diffs every field the dashboard's types.ts requires against real
-- gateway responses, so each endpoint needs at least one row to return. This file supplies
-- exactly one representative row per table, loaded into the throwaway CI database only.
--
-- It is NOT demo data and is never loaded into a running platform: every id starts with
-- `contract-fixture-`, every text field says so, and nothing outside .github/workflows/ci.yml
-- references this file. Anomalies, incidents, actions and forecasts on a real deployment
-- come only from the live services (docs/DATA.md).

INSERT INTO service_metrics (
    "timestamp", service, instance, platform, tps, request_count, error_count, error_ratio,
    latency_p50_seconds, latency_p95_seconds, latency_p99_seconds, cpu_utilisation_ratio,
    memory_utilisation_ratio, queue_depth, active_connections, settlement_lag_seconds,
    quote_latency_seconds, verification_latency_seconds, replicas, labels
)
SELECT now() - make_interval(secs => step * 60), service, service || ':contract-fixture',
       'mojaloop', 20, 1200, 2, 0.0017, 0.04, 0.09, 0.12, 0.5, 0.45, 0, 50, 0, 0, 0, 1,
       '{"source": "contract-fixture"}'::jsonb
FROM unnest(ARRAY['ml-api-adapter', 'central-ledger', 'quoting-service',
                  'account-lookup-service', 'transaction-request-service']) AS service,
     generate_series(0, 4) AS step;

INSERT INTO incidents (
    incident_id, opened_at, status, severity, platform, title, affected_services,
    anomaly_ids, hypotheses, action_ids, ai_summary
) VALUES (
    'contract-fixture-incident', now() - interval '10 minutes', 'open', 'warning', 'mojaloop',
    'contract fixture', ARRAY['central-ledger'], ARRAY['contract-fixture-anomaly'],
    '[{"category": "service", "target": "central-ledger", "confidence": 0.8,
       "summary": "contract fixture", "evidence": ["contract fixture"],
       "correlated_metrics": {"error_ratio": 0.1}}]'::jsonb,
    ARRAY['contract-fixture-action'], 'contract fixture'
);

INSERT INTO anomalies (
    anomaly_id, detected_at, window_start, service, platform, kind, severity, score,
    raw_score, threshold, contributing_features, feature_snapshot, feature_deviations,
    model_name, model_version, description, incident_id
) VALUES (
    'contract-fixture-anomaly', now() - interval '10 minutes', now() - interval '11 minutes',
    'central-ledger', 'mojaloop', 'error_burst', 'warning', 0.8, -0.2, 0.65,
    ARRAY['error_ratio'], '{"error_ratio": 0.1}'::jsonb, '{"error_ratio": 4.0}'::jsonb,
    'isolation-forest', 'contract-fixture', 'contract fixture', 'contract-fixture-incident'
);

INSERT INTO healing_actions (
    action_id, created_at, started_at, finished_at, playbook, target_service, platform,
    incident_id, anomaly_id, trigger_reason, confidence, status, dry_run, execution_mode,
    parameters, result_message, before_snapshot, after_snapshot
) VALUES (
    'contract-fixture-action', now() - interval '9 minutes', now() - interval '9 minutes',
    now() - interval '9 minutes', 'restart_on_oom', 'central-ledger', 'mojaloop',
    'contract-fixture-incident', 'contract-fixture-anomaly', 'contract fixture', 0.8,
    'skipped_dry_run', true, 'simulated', '{}'::jsonb, 'contract fixture', '{}'::jsonb,
    '{}'::jsonb
);

INSERT INTO traffic_predictions (
    prediction_id, created_at, service, platform, horizon_hours, current_tps,
    peak_predicted_tps, peak_at, points, current_replicas, recommended_replicas,
    tps_per_replica, scaling_action, model_name, model_version, backtest_mape
) VALUES (
    'contract-fixture-prediction', now(), 'central-ledger', 'mojaloop', 1, 20, 22,
    now() + interval '30 minutes',
    jsonb_build_array(jsonb_build_object('timestamp', now() + interval '1 hour',
        'predicted_tps', 22, 'lower_bound', 18, 'upper_bound', 26)),
    1, 1, 50, 'hold', 'seasonal-profile', 'contract-fixture', 0.1
);

INSERT INTO deployments (
    deployment_id, occurred_at, service, platform, from_version, to_version, change_type,
    triggered_by, notes
) VALUES (
    'contract-fixture-deployment', now() - interval '15 minutes', 'central-ledger', 'mojaloop',
    'v1.0.4', 'v1.0.5', 'deploy', 'contract-fixture', 'contract fixture'
);

INSERT INTO chaos_runs (
    run_id, started_at, experiment, target_service, platform, duration_seconds, parameters,
    status, notes
) VALUES (
    'contract-fixture-chaos', now() - interval '12 minutes', 'error_injection',
    'central-ledger', 'mojaloop', 60, '{}'::jsonb, 'completed', 'contract fixture'
);
