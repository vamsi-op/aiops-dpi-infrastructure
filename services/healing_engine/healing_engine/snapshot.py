"""Before/after metric snapshots to prove action effectiveness.

Captures key metrics immediately before and after an action executes,
allowing the dashboard and verification scripts to measure whether the
remediation actually fixed the problem.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.db import Database
from aiops_common.log import get_logger

log = get_logger(__name__)


class MetricSnapshot:
    """Captures service metrics before and after healing actions."""

    def __init__(self, db: Database):
        self.db = db

    async def capture(
        self,
        service: str,
        *,
        window_minutes: int = 5,
    ) -> dict[str, float]:
        """Capture recent metric values for a service.
        
        Queries the last N minutes of metrics and computes averages for key features.
        
        Args:
            service: Service name to capture metrics for
            window_minutes: Time window to average over (default: 5 minutes)
            
        Returns:
            Dictionary of feature -> average value
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        
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
                AVG(settlement_lag_seconds) as avg_settlement_lag
            FROM service_metrics
            WHERE service = $1
            AND timestamp >= $2
        """
        
        row = await self.db.fetchrow(query, service, cutoff)
        
        if not row:
            log.warning(
                "snapshot.no_metrics",
                service=service,
                window_minutes=window_minutes,
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
        
        log.info(
            "snapshot.captured",
            service=service,
            window_minutes=window_minutes,
            features=len(snapshot),
        )
        
        return snapshot

    async def compare(
        self,
        before: dict[str, float],
        after: dict[str, float],
    ) -> dict[str, dict[str, float]]:
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
