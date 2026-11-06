"""Anomaly correlation engine.

Groups related anomalies across multiple dimensions:
- Temporal: events within a time window
- Topological: following the dependency graph upstream
- Change: correlating with recent deployments
- Event: external causes like traffic surges or chaos experiments
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiops_common.config import get_settings
from aiops_common.constants import MOJALOOP_DEPENDENCIES, RootCauseCategory
from aiops_common.log import get_logger
from aiops_common.schemas import (
    AnomalyEvent,
    DeploymentEvent,
    RootCauseHypothesis,
)

if TYPE_CHECKING:
    from aiops_common.db import Database

log = get_logger(__name__)

# Correlation windows
TEMPORAL_WINDOW_SECONDS = 300  # 5 minutes
CHANGE_WINDOW_SECONDS = 600  # 10 minutes before anomaly
EVENT_WINDOW_SECONDS = 180  # 3 minutes

# Confidence thresholds
MIN_HYPOTHESIS_CONFIDENCE = 0.3


class Correlator:
    """Correlates anomalies to find root causes across 4 dimensions."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = get_settings()

    async def correlate_temporal(
        self,
        anomalies: list[AnomalyEvent],
        window_seconds: int = TEMPORAL_WINDOW_SECONDS,
    ) -> list[list[AnomalyEvent]]:
        """Group anomalies that occurred within a time window.

        Returns clusters of temporally related anomalies. Each cluster is a candidate
        for becoming one incident.
        """
        if not anomalies:
            return []

        # Sort by detection time
        sorted_anomalies = sorted(anomalies, key=lambda a: a.detected_at)
        clusters: list[list[AnomalyEvent]] = []
        current_cluster: list[AnomalyEvent] = [sorted_anomalies[0]]

        for anomaly in sorted_anomalies[1:]:
            # Check if within window of cluster start
            time_diff = (anomaly.detected_at - current_cluster[0].detected_at).total_seconds()

            if time_diff <= window_seconds:
                current_cluster.append(anomaly)
            else:
                # Start new cluster
                if current_cluster:
                    clusters.append(current_cluster)
                current_cluster = [anomaly]

        # Add final cluster
        if current_cluster:
            clusters.append(current_cluster)

        log.info(
            "temporal_correlation_complete",
            anomaly_count=len(anomalies),
            cluster_count=len(clusters),
            window_seconds=window_seconds,
        )

        return clusters

    def correlate_topological(
        self,
        anomalies: list[AnomalyEvent],
    ) -> RootCauseHypothesis | None:
        """Walk dependency graph upstream to find root service.

        If multiple services are affected, the one with no downstream dependencies
        (or the most upstream) is likely the root cause.
        """
        if not anomalies:
            return None

        affected_services = {a.service for a in anomalies}

        if len(affected_services) == 1:
            # Single service - it's the root by definition
            service = list(affected_services)[0]
            return RootCauseHypothesis(
                category=RootCauseCategory.SERVICE,
                target=service,
                confidence=0.9,  # High confidence when only one service affected
                summary=f"Service {service} is experiencing issues",
                evidence=[
                    f"Only {service} showing anomalies",
                    f"No cascade detected to downstream services",
                ],
            )

        # Find root service(s) - those not downstream of any other affected service
        root_services = set(affected_services)

        for service in affected_services:
            downstream = MOJALOOP_DEPENDENCIES.get(service, ())
            # Remove services that are downstream of this one
            root_services -= set(d for d in downstream if d in affected_services)

        if not root_services:
            # Cycle or no clear root - fall back to service with most severe anomaly
            most_severe = max(anomalies, key=lambda a: a.score)
            root_services = {most_severe.service}

        # Count affected downstream services per root candidate
        root_service = list(root_services)[0]  # Take first if multiple
        downstream_affected = [
            svc
            for svc in MOJALOOP_DEPENDENCIES.get(root_service, ())
            if svc in affected_services
        ]

        evidence = [f"Service {root_service} is upstream of affected services"]
        if downstream_affected:
            evidence.append(f"Cascade detected to: {', '.join(downstream_affected)}")
        evidence.append(f"Total affected services: {len(affected_services)}")

        # Higher confidence with more downstream impact
        confidence = min(0.7 + (len(downstream_affected) * 0.1), 0.95)

        return RootCauseHypothesis(
            category=RootCauseCategory.SERVICE,
            target=root_service,
            confidence=confidence,
            summary=f"Service {root_service} failure cascading to {len(downstream_affected)} downstream service(s)",
            evidence=evidence,
        )

    async def correlate_change(
        self,
        anomalies: list[AnomalyEvent],
        window_seconds: int = CHANGE_WINDOW_SECONDS,
    ) -> list[RootCauseHypothesis]:
        """Find deployments that preceded anomalies.

        A deployment shortly before an error burst is a strong signal.
        """
        if not anomalies:
            return []

        hypotheses: list[RootCauseHypothesis] = []
        earliest_anomaly = min(a.detected_at for a in anomalies)
        affected_services = {a.service for a in anomalies}

        # Query recent deployments
        query = """
            SELECT deployment_id, occurred_at, service, to_version, change_type
            FROM deployments
            WHERE occurred_at >= :start AND occurred_at <= :end
            AND platform = :platform
            ORDER BY occurred_at DESC
        """

        lookback = earliest_anomaly - timedelta(seconds=window_seconds)
        rows = await self.db.fetch_all(
            query,
            {
                "start": lookback,
                "end": earliest_anomaly,
                "platform": self.settings.dpi_platform,
            },
        )

        for row in rows:
            deployment_service = row.get("service")
            deployment_time = row.get("occurred_at")
            if not deployment_service or not deployment_time:
                continue

            # Check if deployed service is affected
            if deployment_service not in affected_services:
                continue

            # Time between deployment and first anomaly
            time_to_anomaly = (earliest_anomaly - deployment_time).total_seconds()

            # Closer in time = higher confidence
            confidence = max(0.5, min(0.95, 1.0 - (time_to_anomaly / window_seconds)))

            evidence = [
                f"Deployment {row['deployment_id']} to {deployment_service}",
                f"Version: {row['to_version']}",
                f"Change type: {row['change_type']}",
                f"Deployed {int(time_to_anomaly)}s before anomaly",
            ]

            hypotheses.append(
                RootCauseHypothesis(
                    category=RootCauseCategory.CHANGE,
                    target=row["deployment_id"],
                    confidence=confidence,
                    summary=f"Recent {row['change_type']} to {deployment_service} may have introduced issues",
                    evidence=evidence,
                )
            )

        log.info(
            "change_correlation_complete",
            anomaly_count=len(anomalies),
            hypothesis_count=len(hypotheses),
        )

        return hypotheses

    async def correlate_event(
        self,
        anomalies: list[AnomalyEvent],
        window_seconds: int = EVENT_WINDOW_SECONDS,
    ) -> list[RootCauseHypothesis]:
        """Detect external events: traffic surges, chaos experiments.

        If traffic increased dramatically without a service fault, it's an event cause.
        """
        if not anomalies:
            return []

        hypotheses: list[RootCauseHypothesis] = []
        earliest_anomaly = min(a.detected_at for a in anomalies)

        # Check for traffic surge anomalies
        traffic_anomalies = [
            a for a in anomalies if "traffic_surge" in a.kind or "traffic_drop" in a.kind
        ]

        if traffic_anomalies:
            # Check if there's a service fault vs just traffic change
            service_faults = [
                a
                for a in anomalies
                if a.kind
                in {"error_burst", "latency_spike", "memory_pressure", "cpu_saturation"}
            ]

            if not service_faults:
                # Pure traffic event with no underlying fault
                hypotheses.append(
                    RootCauseHypothesis(
                        category=RootCauseCategory.EVENT,
                        target="traffic_anomaly",
                        confidence=0.8,
                        summary="Traffic pattern changed without service degradation",
                        evidence=[
                            f"Detected {len(traffic_anomalies)} traffic anomaly(ies)",
                            "No error or latency spikes observed",
                            "Likely external load change",
                        ],
                    )
                )

        # Check for active chaos experiments
        query = """
            SELECT run_id, experiment, target_service, started_at
            FROM chaos_runs
            WHERE started_at <= :start
            AND (ended_at IS NULL OR ended_at >= :window_start)
            AND platform = :platform
            AND status IN ('running', 'completed')
        """

        window_start = earliest_anomaly - timedelta(seconds=window_seconds)
        rows = await self.db.fetch_all(
            query,
            {
                "start": earliest_anomaly,
                "window_start": window_start,
                "platform": self.settings.dpi_platform,
            },
        )

        for row in rows:
            chaos_service = row.get("target_service")
            if not chaos_service:
                continue
            affected_services = {a.service for a in anomalies}

            if chaos_service in affected_services:
                hypotheses.append(
                    RootCauseHypothesis(
                        category=RootCauseCategory.EVENT,
                        target=row["run_id"],
                        confidence=0.99,  # Very high - we caused it!
                        summary=f"Active chaos experiment: {row['experiment']} on {chaos_service}",
                        evidence=[
                            f"Chaos run {row['run_id']} is active",
                            f"Target: {chaos_service}",
                            f"Experiment: {row['experiment']}",
                            "This is an expected, controlled failure",
                        ],
                    )
                )

        log.info(
            "event_correlation_complete",
            anomaly_count=len(anomalies),
            hypothesis_count=len(hypotheses),
        )

        return hypotheses

    async def correlate_all(
        self,
        anomalies: list[AnomalyEvent],
    ) -> list[RootCauseHypothesis]:
        """Run all correlation methods and merge results."""
        if not anomalies:
            return []

        hypotheses: list[RootCauseHypothesis] = []

        # Topological correlation (single service vs cascade)
        topo_hypothesis = self.correlate_topological(anomalies)
        if topo_hypothesis:
            hypotheses.append(topo_hypothesis)

        # Change correlation (deployments)
        change_hypotheses = await self.correlate_change(anomalies)
        hypotheses.extend(change_hypotheses)

        # Event correlation (traffic, chaos)
        event_hypotheses = await self.correlate_event(anomalies)
        hypotheses.extend(event_hypotheses)

        # Sort by confidence, highest first
        hypotheses.sort(key=lambda h: h.confidence, reverse=True)

        # Filter low-confidence hypotheses
        hypotheses = [h for h in hypotheses if h.confidence >= MIN_HYPOTHESIS_CONFIDENCE]

        log.info(
            "correlation_complete",
            anomaly_count=len(anomalies),
            total_hypotheses=len(hypotheses),
            top_confidence=hypotheses[0].confidence if hypotheses else 0.0,
        )

        return hypotheses

    correlate = correlate_all


RCACorrelator = Correlator
