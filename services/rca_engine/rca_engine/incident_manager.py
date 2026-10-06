"""Incident lifecycle management.

Creates, updates, and resolves incidents based on correlated anomalies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import Severity, Stream
from aiops_common.log import get_logger
from aiops_common.schemas import (
    AnomalyEvent,
    Incident,
    RootCauseHypothesis,
    utc_now,
)

if TYPE_CHECKING:
    from aiops_common.db import Database

log = get_logger(__name__)

# Auto-resolve incidents after anomalies have been clear for this long
AUTO_RESOLVE_AFTER_SECONDS = 300  # 5 minutes


class IncidentManager:
    """Manages incident lifecycle: create, update, resolve."""

    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self.settings = get_settings()

    async def create_incident(
        self,
        anomalies: list[AnomalyEvent],
        hypotheses: list[RootCauseHypothesis],
    ) -> Incident:
        """Create a new incident from correlated anomalies."""
        if not anomalies:
            raise ValueError("Cannot create incident without anomalies")

        affected_services = list({a.service for a in anomalies})
        anomaly_ids = [a.anomaly_id for a in anomalies]

        # Determine severity: max of contributing anomalies
        severity_order = {
            Severity.INFO: 1,
            Severity.WARNING: 2,
            Severity.CRITICAL: 3,
        }
        max_severity = max(
            (a.severity for a in anomalies),
            key=lambda s: severity_order.get(s, 0),
            default=Severity.WARNING,
        )

        # Generate title from primary hypothesis or affected services
        if hypotheses:
            primary = hypotheses[0]
            if primary.category == "service":
                title = f"Service {primary.target} failure"
                if len(affected_services) > 1:
                    title += f" affecting {len(affected_services)} services"
            elif primary.category == "change":
                title = f"Deploy-related issue in {affected_services[0]}"
            else:  # event
                title = primary.summary
        else:
            if len(affected_services) == 1:
                title = f"Anomaly detected in {affected_services[0]}"
            else:
                title = f"Multiple services affected: {', '.join(affected_services[:3])}"

        # Create incident record
        incident = Incident(
            opened_at=utc_now(),
            status="open",
            severity=max_severity,
            platform=self.settings.dpi.platform,
            title=title,
            affected_services=affected_services,
            anomaly_ids=anomaly_ids,
            hypotheses=hypotheses,
            ai_summary=self._generate_summary(anomalies, hypotheses),
        )

        # Persist to database
        insert_query = """
            INSERT INTO incidents (
                incident_id, opened_at, status, severity, platform,
                title, affected_services, anomaly_ids, hypotheses_json,
                ai_summary
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """

        await self.db.execute(
            insert_query,
            incident.incident_id,
            incident.opened_at,
            incident.status,
            incident.severity,
            incident.platform,
            incident.title,
            incident.affected_services,
            incident.anomaly_ids,
            [h.model_dump() for h in incident.hypotheses],
            incident.ai_summary,
        )

        # Update anomalies with incident_id
        await self._link_anomalies_to_incident(anomaly_ids, incident.incident_id)

        # Publish to event bus
        await self.bus.publish(Stream.INCIDENTS, incident.model_dump())

        log.info(
            "incident_created",
            incident_id=incident.incident_id,
            affected_services=len(affected_services),
            anomaly_count=len(anomaly_ids),
            severity=severity,
            hypothesis_count=len(hypotheses),
        )

        return incident

    async def _link_anomalies_to_incident(
        self,
        anomaly_ids: list[str],
        incident_id: str,
    ) -> None:
        """Update anomaly records to reference their incident."""
        if not anomaly_ids:
            return

        query = """
            UPDATE anomalies
            SET incident_id = $1
            WHERE anomaly_id = ANY($2)
        """
        await self.db.execute(query, incident_id, anomaly_ids)

    async def resolve_incident(self, incident_id: str) -> None:
        """Mark incident as resolved."""
        query = """
            UPDATE incidents
            SET status = 'resolved', closed_at = $1
            WHERE incident_id = $2 AND status != 'resolved'
        """

        closed_at = utc_now()
        result = await self.db.execute(query, closed_at, incident_id)

        if result:
            log.info("incident_resolved", incident_id=incident_id)

            # Publish resolution event
            await self.bus.publish(
                Stream.INCIDENTS,
                {
                    "incident_id": incident_id,
                    "status": "resolved",
                    "closed_at": closed_at.isoformat(),
                },
            )

    async def check_auto_resolve(self) -> None:
        """Auto-resolve incidents where anomalies have been clear for a while."""
        # Find open incidents
        query = """
            SELECT incident_id, anomaly_ids, opened_at
            FROM incidents
            WHERE status IN ('open', 'mitigating')
            AND platform = $1
        """

        rows = await self.db.query(query, self.settings.dpi.platform)

        for row in rows:
            incident_id = row["incident_id"]
            anomaly_ids = row["anomaly_ids"]

            # Check if all anomalies are old enough to consider resolved
            anomaly_query = """
                SELECT MAX(detected_at) as latest_detection
                FROM anomalies
                WHERE anomaly_id = ANY($1)
            """

            anomaly_row = await self.db.query_one(anomaly_query, anomaly_ids)

            if anomaly_row and anomaly_row["latest_detection"]:
                latest = anomaly_row["latest_detection"]
                age_seconds = (utc_now() - latest).total_seconds()

                if age_seconds >= AUTO_RESOLVE_AFTER_SECONDS:
                    await self.resolve_incident(incident_id)
                    log.info(
                        "auto_resolved",
                        incident_id=incident_id,
                        age_seconds=age_seconds,
                    )

    def _generate_summary(
        self,
        anomalies: list[AnomalyEvent],
        hypotheses: list[RootCauseHypothesis],
    ) -> str:
        """Generate plain-English summary for the dashboard."""
        parts = []

        # Affected services
        services = {a.service for a in anomalies}
        if len(services) == 1:
            parts.append(f"Service **{list(services)[0]}** is experiencing issues.")
        else:
            parts.append(
                f"**{len(services)} services** are affected: {', '.join(sorted(services))}."
            )

        # Primary hypothesis
        if hypotheses:
            primary = hypotheses[0]
            parts.append(f"\n**Likely cause ({primary.confidence:.0%} confidence):** {primary.summary}")

            if primary.evidence:
                parts.append("\n**Evidence:**")
                for evidence in primary.evidence[:3]:  # Top 3
                    parts.append(f"- {evidence}")

            # Alternative hypotheses
            if len(hypotheses) > 1:
                parts.append(f"\n**Alternative explanations:** {len(hypotheses) - 1} other hypothesis(es) identified.")

        # Anomaly details
        critical_anomalies = [a for a in anomalies if a.severity == Severity.CRITICAL]
        if critical_anomalies:
            parts.append(f"\n⚠️ **{len(critical_anomalies)} critical anomaly(ies)** detected.")

        # Top contributing signals
        all_features = []
        for anomaly in anomalies:
            all_features.extend(anomaly.contributing_features[:2])  # Top 2 per anomaly

        if all_features:
            feature_counts = {}
            for f in all_features:
                feature_counts[f] = feature_counts.get(f, 0) + 1
            top_features = sorted(feature_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            parts.append(
                f"\n**Key signals:** {', '.join(f for f, _ in top_features)}"
            )

        return " ".join(parts)

    async def get_open_incidents(self) -> list[Incident]:
        """Retrieve all open incidents."""
        query = """
            SELECT incident_id, opened_at, closed_at, status, severity,
                   platform, title, affected_services, anomaly_ids,
                   hypotheses_json, action_ids, ai_summary
            FROM incidents
            WHERE status IN ('open', 'mitigating', 'acknowledged')
            AND platform = $1
            ORDER BY opened_at DESC
        """

        rows = await self.db.query(query, self.settings.dpi.platform)

        incidents = []
        for row in rows:
            hypotheses = [
                RootCauseHypothesis.model_validate(h) for h in row["hypotheses_json"]
            ]

            incident = Incident(
                incident_id=row["incident_id"],
                opened_at=row["opened_at"],
                closed_at=row["closed_at"],
                status=row["status"],
                severity=row["severity"],
                platform=row["platform"],
                title=row["title"],
                affected_services=row["affected_services"],
                anomaly_ids=row["anomaly_ids"],
                hypotheses=hypotheses,
                action_ids=row["action_ids"] or [],
                ai_summary=row["ai_summary"],
            )
            incidents.append(incident)

        return incidents
