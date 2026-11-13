-- =============================================================================
-- 001 — allow every ActionStatus in healing_actions.status
--
-- The original CHECK constraint predates four statuses the healing engine records:
--   skipped_max_attempts, skipped_global_limit, skipped_downstream_unhealthy,
--   requires_manual_intervention
-- Without them, any action a safety rail blocks for those reasons fails to insert, so
-- the audit trail silently loses exactly the decisions the rails exist to prove.
--
-- New databases get the fixed constraint from init/02_tables.sql; this file is only for
-- databases created before the fix. Idempotent - safe to run more than once.
--
--   Compose:    docker exec -i aiops-timescaledb psql -U aiops -d aiops < infra/timescaledb/migrations/001_healing_action_statuses.sql
--   Kubernetes: kubectl --context kind-aiops -n aiops exec -i statefulset/timescaledb -- psql -U aiops -d aiops < infra/timescaledb/migrations/001_healing_action_statuses.sql
-- =============================================================================

BEGIN;

ALTER TABLE healing_actions DROP CONSTRAINT IF EXISTS healing_actions_status_check;

ALTER TABLE healing_actions ADD CONSTRAINT healing_actions_status_check
    CHECK (status IN ('proposed', 'skipped_dry_run', 'skipped_cooldown',
                      'skipped_budget', 'skipped_low_confidence',
                      'skipped_insufficient_capacity', 'skipped_min_replicas',
                      'skipped_max_replicas', 'skipped_max_attempts',
                      'skipped_global_limit', 'skipped_downstream_unhealthy',
                      'requires_manual_intervention',
                      'running', 'succeeded', 'failed'));

COMMIT;
