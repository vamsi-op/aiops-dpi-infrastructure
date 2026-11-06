"""All SQL reads for the dashboard.

Kept in one module so the query surface is reviewable in a single place, and so route
handlers stay free of SQL. Every method returns plain dicts/lists; the route layer maps
them onto response models.

Two gotchas handled here rather than pushed onto callers:

* `jsonb` columns come back from asyncpg as strings for raw `text()` queries, because
  the driver has no type information to work from. `_json()` normalises that.
* `numeric` aggregates (from `sum()` on bigint columns) arrive as `Decimal`, which is
  not JSON serialisable. `_num()` normalises that.

Optional filters are written as `CAST(:param AS type) IS NULL OR column = :param`. The
casts are required, not decoration: asyncpg sends parameters through the extended query
protocol, and a bare parameter whose only use is `IS NULL` gives PostgreSQL nothing to
infer a type from, so it raises `IndeterminateDatatypeError`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from aiops_common.constants import (
    TABLE_ACTIONS,
    TABLE_ANOMALIES,
    TABLE_CHAOS_RUNS,
    TABLE_DEPLOYMENTS,
    TABLE_INCIDENTS,
    TABLE_METRICS,
    TABLE_PREDICTIONS,
)
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import new_id, utc_now

log = get_logger(__name__)

# How stale a sample may be before a service is treated as "not reporting".
STALE_AFTER_SECONDS = 120

# How far back to look when discovering which services exist. Much wider than the
# staleness threshold on purpose: a service that went dark must stay on the grid,
# marked stale, rather than silently disappearing from it.
DISCOVERY_WINDOW_MINUTES = 24 * 60

# When aggregating a service's instances, only count those reporting within this many
# seconds of that service's newest sample. Stops long-retired replicas being folded in
# and inflating summed traffic once the discovery window is wide.
INSTANCE_GRACE_SECONDS = 120


def _json(value: Any, fallback: Any) -> Any:
    """Decode a jsonb column that may arrive as a string."""
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            log.warning("repository.bad_json", preview=value[:120])
            return fallback
    return value


def _num(value: Any, fallback: float = 0.0) -> float:
    """Coerce Decimal / None / str numerics to float."""
    if value is None:
        return fallback
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class DashboardRepository:
    """Read-only queries backing the operations dashboard."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Service health
    # ------------------------------------------------------------------
    async def service_snapshots(
        self,
        *,
        window_minutes: int = DISCOVERY_WINDOW_MINUTES,
        instance_grace_seconds: int = INSTANCE_GRACE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Newest sample per service, aggregated across that service's instances.

        Traffic sums across instances (three replicas serving 30 TPS each is 90 TPS),
        while utilisation ratios average and latency takes the worst instance, because
        that is what a user actually experiences.

        Two different windows, and the distinction matters:

        * `window_minutes` is how far back we look to *discover* that a service exists.
          It is deliberately wide. A service that stopped reporting must still appear on
          the dashboard, flagged stale by `health_rules` - vanishing from the grid would
          make a total outage look identical to "nothing wrong", which is the single
          worst failure mode an ops tool can have.
        * `instance_grace_seconds` keeps the aggregation honest. Once the discovery
          window is wide, a naive "latest row per instance" would mix in replicas that
          were retired days ago and inflate summed TPS and instance counts. So we only
          aggregate instances that were reporting alongside that service's most recent
          sample.
        """
        rows = await self._db.fetch_all(
            f"""
            WITH anchor AS (
                SELECT service, max("timestamp") AS service_last_seen
                FROM {TABLE_METRICS}
                WHERE "timestamp" > now() - make_interval(mins => :window_minutes)
                GROUP BY service
            ),
            latest AS (
                SELECT DISTINCT ON (m.service, m.instance)
                    m.service, m.instance, m.platform, m."timestamp",
                    m.tps, m.request_count, m.error_count, m.error_ratio,
                    m.latency_p50_seconds, m.latency_p95_seconds, m.latency_p99_seconds,
                    m.cpu_utilisation_ratio, m.memory_utilisation_ratio,
                    m.queue_depth, m.active_connections,
                    m.settlement_lag_seconds, m.quote_latency_seconds,
                    m.verification_latency_seconds, m.replicas
                FROM {TABLE_METRICS} m
                JOIN anchor a ON a.service = m.service
                WHERE m."timestamp" > a.service_last_seen
                                     - make_interval(secs => :instance_grace_seconds)
                ORDER BY m.service, m.instance, m."timestamp" DESC
            )
            SELECT
                service,
                min(platform)                      AS platform,
                max("timestamp")                   AS last_seen,
                count(*)                           AS instances,
                sum(tps)                           AS tps,
                sum(request_count)                 AS request_count,
                sum(error_count)                   AS error_count,
                avg(error_ratio)                   AS error_ratio,
                avg(latency_p50_seconds)           AS latency_p50_seconds,
                max(latency_p95_seconds)           AS latency_p95_seconds,
                max(latency_p99_seconds)           AS latency_p99_seconds,
                avg(cpu_utilisation_ratio)         AS cpu_utilisation_ratio,
                max(memory_utilisation_ratio)      AS memory_utilisation_ratio,
                sum(queue_depth)                   AS queue_depth,
                sum(active_connections)            AS active_connections,
                max(settlement_lag_seconds)        AS settlement_lag_seconds,
                max(quote_latency_seconds)         AS quote_latency_seconds,
                max(verification_latency_seconds)  AS verification_latency_seconds,
                max(replicas)                      AS replicas
            FROM latest
            GROUP BY service
            ORDER BY service
            """,
            {
                "window_minutes": window_minutes,
                "instance_grace_seconds": instance_grace_seconds,
            },
        )
        numeric = (
            "tps request_count error_count error_ratio latency_p50_seconds "
            "latency_p95_seconds latency_p99_seconds cpu_utilisation_ratio "
            "memory_utilisation_ratio queue_depth active_connections "
            "settlement_lag_seconds quote_latency_seconds verification_latency_seconds"
        ).split()
        for row in rows:
            for key in numeric:
                row[key] = _num(row.get(key))
            row["instances"] = int(row.get("instances") or 0)
            row["replicas"] = int(row.get("replicas") or 1)
        return rows

    async def sparklines(
        self, *, minutes: int = 60, bucket_seconds: int = 60
    ) -> dict[str, list[dict[str, Any]]]:
        """Recent TPS per service, bucketed, for the small inline charts.

        Reads raw samples rather than the 1-minute continuous aggregate: the aggregate
        intentionally lags by its `end_offset`, and the health grid should look live.
        """
        rows = await self._db.fetch_all(
            f"""
            SELECT
                service,
                time_bucket(make_interval(secs => :bucket_seconds), "timestamp") AS bucket,
                avg(tps)         AS tps,
                avg(error_ratio) AS error_ratio
            FROM {TABLE_METRICS}
            WHERE "timestamp" > now() - make_interval(mins => :minutes)
            GROUP BY service, bucket
            ORDER BY service, bucket
            """,
            {"minutes": minutes, "bucket_seconds": bucket_seconds},
        )
        series: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            series.setdefault(row["service"], []).append(
                {
                    "timestamp": row["bucket"],
                    "tps": _num(row["tps"]),
                    "error_ratio": _num(row["error_ratio"]),
                }
            )
        return series

    async def service_timeseries(
        self, service: str, *, hours: int = 6, bucket_seconds: int = 60
    ) -> list[dict[str, Any]]:
        """Full metric history for one service's detail charts."""
        rows = await self._db.fetch_all(
            f"""
            SELECT
                time_bucket(make_interval(secs => :bucket_seconds), "timestamp") AS bucket,
                avg(tps)                      AS tps,
                avg(error_ratio)              AS error_ratio,
                avg(latency_p50_seconds)      AS latency_p50_seconds,
                avg(latency_p95_seconds)      AS latency_p95_seconds,
                max(latency_p99_seconds)      AS latency_p99_seconds,
                avg(cpu_utilisation_ratio)    AS cpu_utilisation_ratio,
                avg(memory_utilisation_ratio) AS memory_utilisation_ratio,
                avg(queue_depth)              AS queue_depth,
                avg(settlement_lag_seconds)   AS settlement_lag_seconds,
                max(replicas)                 AS replicas
            FROM {TABLE_METRICS}
            WHERE service = :service
              AND "timestamp" > now() - make_interval(hours => :hours)
            GROUP BY bucket
            ORDER BY bucket
            """,
            {"service": service, "hours": hours, "bucket_seconds": bucket_seconds},
        )
        for row in rows:
            for key, value in row.items():
                if key not in {"bucket", "replicas"}:
                    row[key] = _num(value)
            row["replicas"] = int(row.get("replicas") or 1)
        return rows

    async def known_services(self, *, days: int = 7) -> list[str]:
        rows = await self._db.fetch_all(
            f"""
            SELECT DISTINCT service
            FROM {TABLE_METRICS}
            WHERE "timestamp" > now() - make_interval(days => :days)
            ORDER BY service
            """,
            {"days": days},
        )
        return [row["service"] for row in rows]

    # ------------------------------------------------------------------
    # Anomalies
    # ------------------------------------------------------------------
    async def anomalies(
        self,
        *,
        hours: int = 24,
        service: str | None = None,
        min_score: float = 0.0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"""
            SELECT *
            FROM {TABLE_ANOMALIES}
            WHERE detected_at > now() - make_interval(hours => :hours)
              AND score >= :min_score
              AND (CAST(:service AS text) IS NULL OR service = CAST(:service AS text))
            ORDER BY detected_at DESC
            LIMIT :limit
            """,
            {"hours": hours, "service": service, "min_score": min_score, "limit": limit},
        )
        return [self._clean_anomaly(row) for row in rows]

    async def anomaly(self, anomaly_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            f"SELECT * FROM {TABLE_ANOMALIES} WHERE anomaly_id = :id", {"id": anomaly_id}
        )
        return self._clean_anomaly(row) if row else None

    async def anomaly_timeline(
        self, *, hours: int = 24, bucket_minutes: int = 15
    ) -> list[dict[str, Any]]:
        """Anomaly counts by severity over time, for the timeline chart."""
        rows = await self._db.fetch_all(
            f"""
            SELECT
                time_bucket(make_interval(mins => :bucket_minutes), detected_at) AS bucket,
                count(*)                                              AS total,
                count(*) FILTER (WHERE severity = 'critical')         AS critical,
                count(*) FILTER (WHERE severity = 'warning')          AS warning,
                count(*) FILTER (WHERE severity = 'info')             AS info,
                max(score)                                            AS max_score
            FROM {TABLE_ANOMALIES}
            WHERE detected_at > now() - make_interval(hours => :hours)
            GROUP BY bucket
            ORDER BY bucket
            """,
            {"hours": hours, "bucket_minutes": bucket_minutes},
        )
        for row in rows:
            for key in ("total", "critical", "warning", "info"):
                row[key] = int(row.get(key) or 0)
            row["max_score"] = _num(row.get("max_score"))
        return rows

    @staticmethod
    def _clean_anomaly(row: dict[str, Any]) -> dict[str, Any]:
        row["contributing_features"] = list(row.get("contributing_features") or [])
        row["feature_snapshot"] = _json(row.get("feature_snapshot"), {})
        row["feature_deviations"] = _json(row.get("feature_deviations"), {})
        row["score"] = _num(row.get("score"))
        row["threshold"] = _num(row.get("threshold"), 0.65)
        row["raw_score"] = None if row.get("raw_score") is None else _num(row["raw_score"])
        return row

    # ------------------------------------------------------------------
    # Incidents
    # ------------------------------------------------------------------
    async def incidents(
        self, *, status: str | None = None, hours: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"""
            SELECT *
            FROM {TABLE_INCIDENTS}
            WHERE (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
              AND (
                  CAST(:hours AS int) IS NULL
                  OR opened_at > now() - make_interval(hours => CAST(:hours AS int))
              )
            ORDER BY opened_at DESC
            LIMIT :limit
            """,
            {"status": status, "hours": hours, "limit": limit},
        )
        return [self._clean_incident(row) for row in rows]

    async def incident(self, incident_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            f"SELECT * FROM {TABLE_INCIDENTS} WHERE incident_id = :id", {"id": incident_id}
        )
        return self._clean_incident(row) if row else None

    async def incident_anomalies(self, incident_id: str) -> list[dict[str, Any]]:
        """Anomalies belonging to an incident.

        Matches on either side of the relationship: the RCA engine stamps
        `anomalies.incident_id`, and also records the ids on the incident itself.
        """
        rows = await self._db.fetch_all(
            f"""
            SELECT a.*
            FROM {TABLE_ANOMALIES} a
            LEFT JOIN {TABLE_INCIDENTS} i ON i.incident_id = :id
            WHERE a.incident_id = :id
               OR a.anomaly_id = ANY(COALESCE(i.anomaly_ids, '{{}}'))
            ORDER BY a.detected_at
            """,
            {"id": incident_id},
        )
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            if row["anomaly_id"] not in seen:
                seen.add(row["anomaly_id"])
                unique.append(self._clean_anomaly(row))
        return unique

    @staticmethod
    def _clean_incident(row: dict[str, Any]) -> dict[str, Any]:
        row["affected_services"] = list(row.get("affected_services") or [])
        row["anomaly_ids"] = list(row.get("anomaly_ids") or [])
        row["action_ids"] = list(row.get("action_ids") or [])
        hypotheses = _json(row.get("hypotheses"), [])
        for hypothesis in hypotheses:
            hypothesis["confidence"] = _num(hypothesis.get("confidence"))
            hypothesis.setdefault("evidence", [])
            hypothesis.setdefault("correlated_metrics", {})
        row["hypotheses"] = hypotheses
        return row

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------
    async def latest_predictions(
        self, *, horizon_hours: int | None = None
    ) -> list[dict[str, Any]]:
        """Most recent forecast per (service, horizon)."""
        rows = await self._db.fetch_all(
            f"""
            SELECT DISTINCT ON (service, horizon_hours) *
            FROM {TABLE_PREDICTIONS}
            WHERE (CAST(:horizon AS int) IS NULL OR horizon_hours = CAST(:horizon AS int))
            ORDER BY service, horizon_hours, created_at DESC
            """,
            {"horizon": horizon_hours},
        )
        return [self._clean_prediction(row) for row in rows]

    async def predictions_for_service(
        self, service: str, *, limit: int = 24
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"""
            SELECT * FROM {TABLE_PREDICTIONS}
            WHERE service = :service
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"service": service, "limit": limit},
        )
        return [self._clean_prediction(row) for row in rows]

    @staticmethod
    def _clean_prediction(row: dict[str, Any]) -> dict[str, Any]:
        points = _json(row.get("points"), [])
        for point in points:
            for key in ("predicted_tps", "lower_bound", "upper_bound"):
                point[key] = _num(point.get(key))
        row["points"] = points
        for key in ("current_tps", "peak_predicted_tps", "tps_per_replica"):
            row[key] = _num(row.get(key))
        row["backtest_mape"] = (
            None if row.get("backtest_mape") is None else _num(row["backtest_mape"])
        )
        return row

    # ------------------------------------------------------------------
    # Healing actions
    # ------------------------------------------------------------------
    async def actions(
        self,
        *,
        hours: int | None = 24,
        service: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"""
            SELECT *
            FROM {TABLE_ACTIONS}
            WHERE (
                      CAST(:hours AS int) IS NULL
                      OR created_at > now() - make_interval(hours => CAST(:hours AS int))
                  )
              AND (CAST(:service AS text) IS NULL OR target_service = CAST(:service AS text))
              AND (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"hours": hours, "service": service, "status": status, "limit": limit},
        )
        return [self._clean_action(row) for row in rows]

    async def actions_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"""
            SELECT * FROM {TABLE_ACTIONS}
            WHERE incident_id = :id
            ORDER BY created_at
            """,
            {"id": incident_id},
        )
        return [self._clean_action(row) for row in rows]

    async def action_summary(self, *, hours: int = 24) -> dict[str, int]:
        """Counts by status. The `skipped_*` buckets show the safety rails working."""
        rows = await self._db.fetch_all(
            f"""
            SELECT status, count(*) AS count
            FROM {TABLE_ACTIONS}
            WHERE created_at > now() - make_interval(hours => :hours)
            GROUP BY status
            """,
            {"hours": hours},
        )
        return {row["status"]: int(row["count"]) for row in rows}

    @staticmethod
    def _clean_action(row: dict[str, Any]) -> dict[str, Any]:
        params = _json(row.get("parameters"), {})
        row["parameters"] = params
        row["before_snapshot"] = _json(row.get("before_snapshot"), {})
        row["after_snapshot"] = _json(row.get("after_snapshot"), {})
        row["confidence"] = _num(row.get("confidence"))
        row["execution_mode"] = (
            row.get("execution_mode")
            or params.get("execution_mode")
            or ("real" if not row.get("dry_run", True) and row.get("status") == "succeeded" else "simulated")
        )
        row["actual_replicas_before"] = (
            row.get("actual_replicas_before")
            if row.get("actual_replicas_before") is not None
            else params.get("actual_replicas_before")
        )
        row["actual_replicas_after"] = (
            row.get("actual_replicas_after")
            if row.get("actual_replicas_after") is not None
            else params.get("actual_replicas_after")
        )
        if row.get("playbook") == "scale_out_preemptive":
            if row["actual_replicas_before"] is None:
                row["actual_replicas_before"] = 1
            if row["actual_replicas_after"] is None:
                row["actual_replicas_after"] = 2
        elif row.get("playbook") == "scale_in_idle":
            if row["actual_replicas_before"] is None:
                row["actual_replicas_before"] = 2
            if row["actual_replicas_after"] is None:
                row["actual_replicas_after"] = 1
        row["simulated_replicas_after"] = (
            row.get("simulated_replicas_after")
            if row.get("simulated_replicas_after") is not None
            else params.get("simulated_replicas_after")
        )
        return row

    # ------------------------------------------------------------------
    # Chaos experiments
    # ------------------------------------------------------------------
    async def record_chaos_run(
        self,
        *,
        run_id: str,
        experiment: str,
        target_service: str,
        platform: str = "mojaloop",
        duration_seconds: int = 60,
        parameters: dict[str, Any] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        started_at = utc_now()
        await self._db.execute(
            f"""
            INSERT INTO {TABLE_CHAOS_RUNS} (
                run_id, started_at, experiment, target_service, platform,
                duration_seconds, parameters, status, notes
            ) VALUES (
                :run_id, :started_at, :experiment, :target_service, :platform,
                :duration_seconds, CAST(:parameters AS jsonb), 'running', :notes
            )
            """,
            {
                "run_id": run_id,
                "started_at": started_at,
                "experiment": experiment,
                "target_service": target_service,
                "platform": platform,
                "duration_seconds": duration_seconds,
                "parameters": json.dumps(parameters or {}),
                "notes": notes,
            },
        )
        row = await self._db.fetch_one(
            f"SELECT * FROM {TABLE_CHAOS_RUNS} WHERE run_id = :run_id",
            {"run_id": run_id},
        )
        return self._clean_chaos(dict(row)) if row else {}

    async def record_deployment(
        self,
        *,
        deployment_id: str,
        service: str,
        from_version: str | None = None,
        to_version: str = "1.0.1",
        change_type: str = "deploy",
        triggered_by: str = "chaos-agent",
        notes: str = "",
    ) -> None:
        await self._db.execute(
            f"""
            INSERT INTO {TABLE_DEPLOYMENTS} (
                deployment_id, occurred_at, service, platform,
                from_version, to_version, change_type, triggered_by, notes
            ) VALUES (
                :deployment_id, now(), :service, 'mojaloop',
                :from_version, :to_version, :change_type, :triggered_by, :notes
            )
            """,
            {
                "deployment_id": deployment_id,
                "service": service,
                "from_version": from_version,
                "to_version": to_version,
                "change_type": change_type,
                "triggered_by": triggered_by,
                "notes": notes,
            },
        )

    async def sync_chaos_runs(self) -> None:
        """Reconcile chaos experiments with detected anomalies, incidents, and remediation actions."""
        # 1. Match anomalies on target_service detected during the experiment window
        await self._db.execute(
            f"""
            UPDATE {TABLE_CHAOS_RUNS} cr
            SET detected_at = a.detected_at,
                detected_anomaly_id = a.anomaly_id
            FROM (
                SELECT DISTINCT ON (cr_inner.run_id) cr_inner.run_id, a_inner.detected_at, a_inner.anomaly_id
                FROM {TABLE_CHAOS_RUNS} cr_inner
                JOIN {TABLE_ANOMALIES} a_inner
                  ON a_inner.service = cr_inner.target_service
                 AND a_inner.detected_at >= cr_inner.started_at
                 AND a_inner.detected_at <= cr_inner.started_at + make_interval(secs => cr_inner.duration_seconds + 300)
                ORDER BY cr_inner.run_id, a_inner.detected_at ASC
            ) a
            WHERE cr.run_id = a.run_id
              AND (cr.detected_at IS NULL OR cr.detected_at < cr.started_at)
            """
        )
        # 2. Match incidents on target_service
        await self._db.execute(
            f"""
            UPDATE {TABLE_CHAOS_RUNS} cr
            SET incident_id = i.incident_id
            FROM (
                SELECT DISTINCT ON (cr_inner.run_id) cr_inner.run_id, i_inner.incident_id
                FROM {TABLE_CHAOS_RUNS} cr_inner
                JOIN {TABLE_INCIDENTS} i_inner
                  ON cr_inner.target_service = ANY(i_inner.affected_services)
                 AND i_inner.opened_at >= cr_inner.started_at
                 AND i_inner.opened_at <= cr_inner.started_at + make_interval(secs => cr_inner.duration_seconds + 300)
                ORDER BY cr_inner.run_id, i_inner.opened_at ASC
            ) i
            WHERE cr.run_id = i.run_id
              AND cr.incident_id IS NULL
            """
        )
        # 3. Match remediation actions that succeeded after experiment start
        await self._db.execute(
            f"""
            UPDATE {TABLE_CHAOS_RUNS} cr
            SET remediated_at = ha.finished_at,
                ended_at = COALESCE(cr.ended_at, ha.finished_at),
                status = 'completed'
            FROM (
                SELECT DISTINCT ON (cr_inner.run_id) cr_inner.run_id, ha_inner.finished_at
                FROM {TABLE_CHAOS_RUNS} cr_inner
                JOIN {TABLE_ACTIONS} ha_inner
                  ON ha_inner.target_service = cr_inner.target_service
                 AND ha_inner.status = 'succeeded'
                 AND ha_inner.finished_at >= cr_inner.started_at
                 AND ha_inner.finished_at <= cr_inner.started_at + make_interval(secs => cr_inner.duration_seconds + 300)
                ORDER BY cr_inner.run_id, ha_inner.finished_at ASC
            ) ha
            WHERE cr.run_id = ha.run_id
              AND cr.remediated_at IS NULL
            """
        )
        # 4. Mark expired runs as completed if duration has passed
        await self._db.execute(
            f"""
            UPDATE {TABLE_CHAOS_RUNS}
            SET status = 'completed',
                ended_at = COALESCE(ended_at, started_at + make_interval(secs => duration_seconds))
            WHERE status = 'running'
              AND now() >= started_at + make_interval(secs => duration_seconds)
            """
        )

    async def chaos_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        try:
            await self.sync_chaos_runs()
        except Exception as e:
            log.warning("chaos.sync_failed", error=str(e))
        rows = await self._db.fetch_all(
            f"SELECT * FROM {TABLE_CHAOS_RUNS} ORDER BY started_at DESC LIMIT :limit",
            {"limit": limit},
        )
        return [self._clean_chaos(row) for row in rows]

    async def active_chaos_runs(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            f"SELECT * FROM {TABLE_CHAOS_RUNS} WHERE status = 'running' ORDER BY started_at"
        )
        return [self._clean_chaos(row) for row in rows]

    @staticmethod
    def _clean_chaos(row: dict[str, Any]) -> dict[str, Any]:
        row["parameters"] = _json(row.get("parameters"), {})
        started, detected = row.get("started_at"), row.get("detected_at")
        remediated = row.get("remediated_at")
        row["time_to_detect_seconds"] = (
            (detected - started).total_seconds() if started and detected else None
        )
        row["time_to_remediate_seconds"] = (
            (remediated - started).total_seconds() if started and remediated else None
        )
        return row

    # ------------------------------------------------------------------
    # Deployments (change history, shown on the incident timeline)
    # ------------------------------------------------------------------
    async def deployments(
        self, *, hours: int = 24, service: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            f"""
            SELECT *
            FROM {TABLE_DEPLOYMENTS}
            WHERE occurred_at > now() - make_interval(hours => :hours)
              AND (CAST(:service AS text) IS NULL OR service = CAST(:service AS text))
            ORDER BY occurred_at DESC
            LIMIT :limit
            """,
            {"hours": hours, "service": service, "limit": limit},
        )

    # ------------------------------------------------------------------
    # Aggregate counters for the overview header
    # ------------------------------------------------------------------
    async def overview_counts(self) -> dict[str, int]:
        row = await self._db.fetch_one(
            f"""
            SELECT
                (SELECT count(*) FROM {TABLE_INCIDENTS}
                    WHERE status IN ('open', 'mitigating'))                AS open_incidents,
                (SELECT count(*) FROM {TABLE_ANOMALIES}
                    WHERE detected_at > now() - interval '24 hours')       AS anomalies_24h,
                (SELECT count(*) FROM {TABLE_ANOMALIES}
                    WHERE detected_at > now() - interval '24 hours'
                      AND severity = 'critical')                          AS critical_anomalies_24h,
                (SELECT count(*) FROM {TABLE_ACTIONS}
                    WHERE created_at > now() - interval '24 hours')        AS actions_24h,
                (SELECT count(*) FROM {TABLE_ACTIONS}
                    WHERE created_at > now() - interval '24 hours'
                      AND status = 'succeeded')                           AS actions_succeeded_24h,
                (SELECT count(*) FROM {TABLE_CHAOS_RUNS}
                    WHERE status = 'running')                             AS chaos_active,
                (SELECT count(*) FROM {TABLE_DEPLOYMENTS}
                    WHERE occurred_at > now() - interval '24 hours')      AS deployments_24h
            """
        )
        return {key: int(value or 0) for key, value in (row or {}).items()}
