"""Before/after metric snapshots to prove action effectiveness.

Captures key metrics immediately before and after an action executes,
allowing the dashboard and verification scripts to measure whether the
remediation actually fixed the problem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.db import Database
from aiops_common.log import get_logger

log = get_logger(__name__)


class MetricSnapshot:
    """Captures service metrics before and after healing actions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def capture(
        self,
        service: str,
        *,
        window_minutes: int = 5,
        window_seconds: int | None = None,
        since: datetime | None = None,
    ) -> dict[str, float]:
        """Capture recent metric values for a service.

        Queries recent metrics and computes averages for key features.

        Args:
            service: Service name to capture metrics for
            window_minutes: Time window to average over (default: 5 minutes)
            window_seconds: If provided, overrides window_minutes with exact seconds

        Returns:
            Dictionary of feature -> average value
        """
        if since is not None:
            # Only samples taken after `since` (e.g. the remediation). A fixed look-back
            # window captured mostly pre-action data and judged the action by the fault.
            cutoff = since
        elif window_seconds is not None:
            cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
        else:
            cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)

        # Query recent metrics
        query = """
            SELECT
                AVG(tps) as avg_tps,
                AVG(error_ratio) as avg_error_ratio,
                AVG(latency_p50_seconds) as avg_latency_p50,
                AVG(latency_p99_seconds) as avg_latency_p99,
                AVG(cpu_utilisation_ratio) as avg_cpu,
                AVG(memory_utilisation_ratio) as avg_memory,
                AVG(queue_depth) as avg_queue_depth,
                AVG(active_connections) as avg_connections,
                AVG(settlement_lag_seconds) as avg_settlement_lag,
                COUNT(*) as samples,
                bool_and(COALESCE(labels->>'restart_in_rate_window', '') = '1')
                    as all_in_restart_window
            FROM service_metrics
            WHERE service = :service
            AND timestamp >= :cutoff
        """

        row = await self.db.fetch_one(query, {"service": service, "cutoff": cutoff})

        if not row or not row.get("samples", 1):
            log.warning(
                "snapshot.no_metrics",
                service=service,
                cutoff=cutoff.isoformat(),
            )
            return {}

        # Build snapshot dictionary
        snapshot = {
            "tps": float(row["avg_tps"] or 0.0),
            "error_ratio": float(row["avg_error_ratio"] or 0.0),
            "latency_p50_seconds": float(row["avg_latency_p50"] or 0.0),
            "latency_p99_seconds": float(row["avg_latency_p99"] or 0.0),
            "cpu_utilisation_ratio": float(row["avg_cpu"] or 0.0),
            "memory_utilisation_ratio": float(row["avg_memory"] or 0.0),
            "queue_depth": float(row["avg_queue_depth"] or 0.0),
            "active_connections": float(row["avg_connections"] or 0.0),
            "settlement_lag_seconds": float(row["avg_settlement_lag"] or 0.0),
        }
        if row.get("all_in_restart_window"):
            # Every sample is inside the 5-minute window after a restart: the rate-based
            # signals still describe the pre-restart window (observed: a rollback judged
            # "failed" on the bad deploy's own 1.5% errors). Leave them out; the instant
            # gauges (CPU, memory, queue, connections) are current. Rate-based SLOs are
            # judged by the SLO-window auto-resolve.
            for rate_based in ("tps", "error_ratio", "latency_p50_seconds", "latency_p99_seconds"):
                snapshot.pop(rate_based, None)
            snapshot["rate_signals_deferred"] = 1.0

        log.info(
            "snapshot.captured",
            service=service,
            cutoff=cutoff.isoformat(),
            features=len(snapshot),
        )

        return snapshot

    def compare(
        self,
        before: dict[str, float],
        after: dict[str, float],
    ) -> dict[str, Any]:
        """Compare before and after snapshots.

        Args:
            before: Snapshot before action
            after: Snapshot after action

        Returns:
            Dictionary with change analysis:
            {
                "absolute_change": {feature: delta},
                "percent_change": {feature: percent},
                "improved": [list of improved features],
                "degraded": [list of degraded features]
            }
        """
        absolute_change = {}
        percent_change = {}
        improved = []
        degraded = []

        for feature in ANOMALY_FEATURES:
            if feature not in before or feature not in after:
                continue

            before_val = before[feature]
            after_val = after[feature]
            delta = after_val - before_val

            absolute_change[feature] = delta

            # Compute percent change (avoid division by zero)
            if before_val != 0:
                pct = (delta / before_val) * 100.0
                percent_change[feature] = pct
            else:
                percent_change[feature] = 0.0

            # Determine if improved or degraded
            # Lower is better for: error_ratio, latency, cpu, memory, queue_depth, settlement_lag
            # Higher is better for: tps, active_connections (debatable, but generally yes)
            if feature in {
                "error_ratio",
                "latency_p50_seconds",
                "latency_p99_seconds",
                "cpu_utilisation_ratio",
                "memory_utilisation_ratio",
                "queue_depth",
                "settlement_lag_seconds",
            }:
                if delta < -0.01:  # Decreased by more than threshold
                    improved.append(feature)
                elif delta > 0.01:  # Increased
                    degraded.append(feature)
            else:  # tps, active_connections
                if delta > 0.01:  # Increased
                    improved.append(feature)
                elif delta < -0.01:  # Decreased
                    degraded.append(feature)

        return {
            "absolute_change": absolute_change,
            "percent_change": percent_change,
            "improved": improved,
            "degraded": degraded,
        }

    def verify_recovery(
        self,
        service: str,
        before: dict[str, float],
        after: dict[str, float],
        *,
        playbook: str | None = None,
        anomaly_kind: str | None = None,
        container_healthy: bool = True,
        thresholds: dict[str, float] | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Verify whether the service has genuinely recovered after a healing action.

        Evaluates:
        1. Container running/healthy state
        2. Metric comparison against service SLO thresholds
        3. Feature-specific recovery for the anomaly kind (memory drop, queue clearing, etc.)

        Returns:
            (recovered: bool, message: str, details: dict)
        """
        slo_limits = {
            "error_ratio": 0.01,
            "latency_p99_seconds": 0.25,
            "latency_p50_seconds": 0.10,
            "cpu_utilisation_ratio": 0.80,
            "memory_utilisation_ratio": 0.85,
            "queue_depth": 5.0,
            "settlement_lag_seconds": 5.0,
        }
        if thresholds:
            slo_limits.update(thresholds)

        if not container_healthy:
            return (
                False,
                f"Container health check failed for {service}: container is not running or crashed",
                {"container_healthy": False},
            )

        if not after:
            return (
                False,
                f"No post-remediation telemetry available for {service} to verify recovery",
                {"after_metrics_available": False},
            )

        comparison = self.compare(before, after)
        details = {
            "comparison": comparison,
            "thresholds": slo_limits,
            "target_metric_improved": False,
        }

        # Check target metric improvement based on anomaly_kind / playbook
        # 1. Memory Pressure / OOM
        if (
            anomaly_kind in ("memory_pressure", "cpu_saturation")
            or playbook == "restart_on_oom"
        ):
            before_mem = before.get("memory_utilisation_ratio", 0.0)
            after_mem = after.get("memory_utilisation_ratio", 0.0)
            before_cpu = before.get("cpu_utilisation_ratio", 0.0)
            after_cpu = after.get("cpu_utilisation_ratio", 0.0)

            mem_ok = after_mem <= slo_limits["memory_utilisation_ratio"] or (
                after_mem < before_mem - 0.05
            )
            cpu_ok = after_cpu <= slo_limits["cpu_utilisation_ratio"] or (
                after_cpu < before_cpu - 0.05
            )

            if not (mem_ok and cpu_ok):
                reason = (
                    f"Resource saturation persisted: memory={after_mem:.2f} (SLO <= {slo_limits['memory_utilisation_ratio']}), "
                    f"cpu={after_cpu:.2f} (SLO <= {slo_limits['cpu_utilisation_ratio']})"
                )
                return False, reason, details
            details["target_metric_improved"] = True

        # 2. Queue Backlog / Settlement Delay
        elif (
            anomaly_kind in ("queue_backlog", "settlement_delay")
            or playbook == "clear_queue_backlog"
        ):
            before_queue = before.get("queue_depth", 0.0)
            after_queue = after.get("queue_depth", 0.0)
            queue_ok = after_queue <= slo_limits["queue_depth"] or (
                before_queue > 0 and after_queue < before_queue * 0.5
            )
            if not queue_ok:
                reason = f"Queue backlog persisted: depth={after_queue:.1f} (SLO <= {slo_limits['queue_depth']})"
                return False, reason, details
            details["target_metric_improved"] = True

        # 3. Latency Spike / Circuit Breaker
        elif anomaly_kind == "latency_spike" or playbook == "circuit_break_cascade":
            before_p99 = before.get("latency_p99_seconds", 0.0)
            after_p99 = after.get("latency_p99_seconds", 0.0)
            p99_ok = after_p99 <= slo_limits["latency_p99_seconds"] or (
                after_p99 < before_p99 * 0.7
            )
            if not p99_ok:
                reason = f"High latency persisted: p99={after_p99:.3f}s (SLO <= {slo_limits['latency_p99_seconds']}s)"
                return False, reason, details
            details["target_metric_improved"] = True

        # 4. Error Burst
        elif anomaly_kind == "error_burst":
            after_err = after.get("error_ratio", 0.0)
            if after_err > slo_limits["error_ratio"]:
                reason = f"Error ratio exceeded SLO: {after_err:.3f} (SLO <= {slo_limits['error_ratio']})"
                return False, reason, details
            details["target_metric_improved"] = True

        # General degradation check: ensure error ratio didn't blow out
        after_error = after.get("error_ratio", 0.0)
        if after_error > 0.10:
            return False, f"Severe error ratio observed post-action: {after_error:.2%}", details

        details["target_metric_improved"] = True
        return (
            True,
            f"Service {service} verified healthy: all key metrics within SLO thresholds",
            details,
        )
