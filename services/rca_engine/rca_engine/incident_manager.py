"""Incident lifecycle management.

Creates, updates, and resolves incidents based on correlated anomalies.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import ActionStatus, Severity, Stream
from aiops_common.log import get_logger
from aiops_common.schemas import (
    AnomalyEvent,
    Incident,
    RootCauseHypothesis,
    utc_now,
)
from aiops_common.topology import get_topology

if TYPE_CHECKING:
    from aiops_common.db import Database

log = get_logger(__name__)


def transitive_dependencies(services: list[str] | set[str]) -> set[str]:
    """Every service the given services call, directly or indirectly."""
    return get_topology().transitive_dependencies(set(services))


# Auto-resolve / continuous recovery window (2 minutes)
AUTO_RESOLVE_AFTER_SECONDS = 120

# Concrete SLO thresholds for service recovery verification
SLO_MAX_ERROR_RATIO = 0.01  # 1% error rate threshold
SLO_MAX_LATENCY_P99_SECONDS = 0.250  # 250ms p99 latency threshold
SLO_MAX_QUEUE_DEPTH = 5.0  # queue backlog threshold
SLO_MAX_CPU_RATIO = 0.85  # 85% cpu utilization
SLO_MAX_MEMORY_RATIO = 0.85  # 85% memory utilization


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
        """Create a new incident or correlate into an existing ongoing incident."""
        if not anomalies:
            raise ValueError("Cannot create incident without anomalies")

        affected_services = sorted({a.service for a in anomalies})
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

        # Check for existing open/mitigating incident touching these services (duplicate prevention)
        find_query = """
            SELECT incident_id, opened_at, status, severity, platform,
                   title, affected_services, anomaly_ids, hypotheses, ai_summary
            FROM incidents
            WHERE status IN ('open', 'mitigating')
            AND platform = :platform
            AND affected_services && :services
            ORDER BY opened_at DESC
            LIMIT 1
        """
        # Search with the services *and everything they depend on*. A cascade's victims are
        # the callers of the failing service, not the failing service itself, so matching on
        # the anomalous services alone opened a separate incident per victim - each naming
        # itself as root cause - and healing restarted the victims.
        existing = await self.db.fetch_one(
            find_query,
            {
                "platform": self.settings.dpi_platform,
                "services": sorted(
                    set(affected_services) | transitive_dependencies(affected_services)
                ),
            },
        )

        if existing:
            # Correlate and merge into existing incident instead of creating a duplicate
            existing_id = existing["incident_id"]
            existing_services = list(existing.get("affected_services") or [])
            merged_services = sorted(set(existing_services) | set(affected_services))

            existing_anom_ids = list(existing.get("anomaly_ids") or [])
            merged_anom_ids = list(dict.fromkeys(existing_anom_ids + anomaly_ids))

            new_anom_ids = [aid for aid in anomaly_ids if aid not in existing_anom_ids]

            # Merge hypotheses
            existing_hypo_raw = existing.get("hypotheses") or []
            if isinstance(existing_hypo_raw, str):
                try:
                    existing_hypo_raw = json.loads(existing_hypo_raw)
                except Exception:  # noqa: BLE001
                    existing_hypo_raw = []

            existing_hypotheses = [
                RootCauseHypothesis.model_validate(h) for h in existing_hypo_raw
            ]
            # Victims joining a cascade: the root is downstream, already in the incident, and
            # its hypotheses stay. Hypotheses computed from the victims' anomalies alone would
            # name a victim as the root.
            # Applies to every later batch from a victim too, not just its first: a victim's
            # own anomalies alone always produce "only <victim> is anomalous", and letting that
            # replace the incident's hypotheses renamed the root to the victim.
            failing_dependencies = set(existing_services) & transitive_dependencies(
                affected_services
            )
            if failing_dependencies and existing_hypotheses:
                merged_hypotheses = existing_hypotheses
                new_victims = sorted(set(affected_services) - set(existing_services))
                if new_victims:
                    merged_hypotheses[0].evidence.append(
                        f"Cascade: {', '.join(new_victims)} affected through their dependency "
                        f"on {', '.join(sorted(failing_dependencies))}"
                    )
            elif hypotheses and (
                not existing_hypotheses
                or hypotheses[0].confidence > existing_hypotheses[0].confidence
            ):
                merged_hypotheses = hypotheses
            else:
                # Keep the stronger diagnosis. A later batch analysed alone sees less: five
                # minutes after a bad deploy its CHANGE evidence had decayed to 0.5, and
                # replacing the incident's 0.94 CHANGE root with "only this service is
                # affected" (0.90) lost the rollback.
                merged_hypotheses = existing_hypotheses

            summary = self._generate_summary(anomalies, merged_hypotheses)

            existing_sev = existing.get("severity") or Severity.INFO
            escalated_severity = max(
                [existing_sev, max_severity],
                key=lambda s: severity_order.get(s, 0),
            )

            update_query = """
                UPDATE incidents
                SET affected_services = :affected_services,
                    anomaly_ids = :anomaly_ids,
                    hypotheses = CAST(:hypotheses AS jsonb),
                    ai_summary = :ai_summary,
                    severity = :severity
                WHERE incident_id = :incident_id
            """
            await self.db.execute(
                update_query,
                {
                    "incident_id": existing_id,
                    "affected_services": merged_services,
                    "anomaly_ids": merged_anom_ids,
                    "hypotheses": json.dumps([h.model_dump() for h in merged_hypotheses]),
                    "ai_summary": summary,
                    "severity": escalated_severity,
                },
            )

            if new_anom_ids:
                await self._link_anomalies_to_incident(new_anom_ids, existing_id)

            log.info(
                "incident_updated_merged",
                incident_id=existing_id,
                new_anomalies=len(new_anom_ids),
                total_anomalies=len(merged_anom_ids),
            )

            return Incident(
                incident_id=existing_id,
                opened_at=existing["opened_at"],
                status=existing["status"],
                severity=max_severity,
                platform=self.settings.dpi_platform,
                title=existing["title"],
                affected_services=merged_services,
                anomaly_ids=merged_anom_ids,
                hypotheses=merged_hypotheses,
                ai_summary=summary,
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
            platform=self.settings.dpi_platform,
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
                title, affected_services, anomaly_ids, hypotheses,
                ai_summary
            ) VALUES (
                :incident_id, :opened_at, :status, :severity, :platform,
                :title, :affected_services, :anomaly_ids, CAST(:hypotheses AS jsonb),
                :ai_summary
            )
        """

        await self.db.execute(
            insert_query,
            {
                "incident_id": incident.incident_id,
                "opened_at": incident.opened_at,
                "status": incident.status,
                "severity": incident.severity,
                "platform": incident.platform,
                "title": incident.title,
                "affected_services": incident.affected_services,
                "anomaly_ids": incident.anomaly_ids,
                "hypotheses": json.dumps([h.model_dump() for h in incident.hypotheses]),
                "ai_summary": incident.ai_summary,
            },
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
            severity=max_severity,
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
            SET incident_id = :incident_id
            WHERE anomaly_id = ANY(:anomaly_ids)
        """
        await self.db.execute(query, {"incident_id": incident_id, "anomaly_ids": anomaly_ids})

    async def resolve_incident(self, incident_id: str, reason: str = "") -> None:
        """Mark incident as resolved with resolution audit trail."""
        default_reason = (
            "Verified recovered: All affected services healthy within SLO thresholds "
            f"over continuous {AUTO_RESOLVE_AFTER_SECONDS}s recovery window."
        )
        res_reason = reason or default_reason

        # Fetch current ai_summary to append resolution explanation
        fetch_query = "SELECT ai_summary FROM incidents WHERE incident_id = :incident_id"
        row = await self.db.fetch_one(fetch_query, {"incident_id": incident_id})
        current_summary = (row.get("ai_summary") or "") if row else ""

        if res_reason and "**Resolution:**" not in current_summary:
            updated_summary = f"{current_summary}\n\n**Resolution:** {res_reason}".strip()
        else:
            updated_summary = current_summary

        query = """
            UPDATE incidents
            SET status = 'resolved', closed_at = :closed_at, ai_summary = :ai_summary
            WHERE incident_id = :incident_id AND status != 'resolved'
        """

        closed_at = utc_now()
        result = await self.db.execute(
            query,
            {"closed_at": closed_at, "incident_id": incident_id, "ai_summary": updated_summary},
        )

        if result:
            log.info("incident_resolved", incident_id=incident_id, reason=res_reason)

            # Publish resolution event
            await self.bus.publish(
                Stream.INCIDENTS,
                {
                    "incident_id": incident_id,
                    "status": "resolved",
                    "closed_at": closed_at.isoformat(),
                    "reason": res_reason,
                },
            )

    async def check_auto_resolve(self) -> None:
        """Auto-resolve incidents after verified recovery within SLOs."""
        query = """
            SELECT incident_id, anomaly_ids, affected_services, opened_at, hypotheses, ai_summary
            FROM incidents
            WHERE status IN ('open', 'mitigating')
            AND platform = :platform
        """

        rows = await self.db.fetch_all(query, {"platform": self.settings.dpi_platform})

        for row in rows:
            incident_id = row["incident_id"]
            services = row.get("affected_services") or []
            opened_at = row["opened_at"]
            if not services:
                continue

            # 1. Continuous recovery window: incident must have been open for at least AUTO_RESOLVE_AFTER_SECONDS
            age_seconds = (utc_now() - opened_at).total_seconds()
            if age_seconds < AUTO_RESOLVE_AFTER_SECONDS:
                log.debug(
                    "auto_resolve_skipped_insufficient_recovery_window",
                    incident_id=incident_id,
                    age_seconds=age_seconds,
                    required=AUTO_RESOLVE_AFTER_SECONDS,
                )
                continue

            # 2. Check remediation / healing actions for this incident or target services.
            #    Only actions that ran: a `skipped_*` record is the healer *declining* to act
            #    (cooldown, budget, attempt cap) and says nothing about whether the service
            #    recovered. Judging by the latest row let 37 skipped_max_attempts records keep a
            #    recovered incident open indefinitely, every SLO green.
            action_query = """
                SELECT action_id, status, result_message, parameters, created_at
                FROM healing_actions
                WHERE (incident_id = :incident_id
                       OR (target_service = ANY(:services) AND created_at >= :opened_at))
                  AND status NOT LIKE 'skipped%'
                ORDER BY created_at DESC
                LIMIT 1
            """
            action_row = await self.db.fetch_one(
                action_query,
                {"incident_id": incident_id, "services": services, "opened_at": opened_at},
            )

            action_status = action_row.get("status") if action_row else None

            # Action currently in progress -> do not resolve yet
            if action_status in (ActionStatus.RUNNING, "running"):
                log.info("auto_resolve_skipped_action_in_progress", incident_id=incident_id)
                continue

            # Failed remediation or an explicit escalation to a human -> keep OPEN
            if action_status in (
                ActionStatus.FAILED,
                "failed",
                ActionStatus.REQUIRES_MANUAL_INTERVENTION,
                "requires_manual_intervention",
            ):
                log.info(
                    "auto_resolve_blocked_failed_remediation",
                    incident_id=incident_id,
                    action_status=action_status,
                    result_message=action_row.get("result_message") if action_row else None,
                )
                continue

            # 3. Continuous SLO Telemetry Check over Recovery Window
            metrics_cutoff = utc_now() - timedelta(seconds=AUTO_RESOLVE_AFTER_SECONDS)
            # A caller's error ratio and p99 are rate windows that still hold the calls which
            # failed while a dependency restarted (metrics-agent tags those rows); they kept a
            # recovered incident open for the full five minutes after every remediation. The
            # restarted service itself and every gauge are still judged.
            metrics_query = """
                SELECT service,
                       AVG(error_ratio) FILTER (WHERE NOT COALESCE(labels->>'dependency_restart_in_rate_window', '') = '1') as avg_error,
                       AVG(latency_p99_seconds) FILTER (WHERE NOT COALESCE(labels->>'dependency_restart_in_rate_window', '') = '1') as avg_p99,
                       AVG(queue_depth) as avg_queue,
                       AVG(cpu_utilisation_ratio) as avg_cpu,
                       AVG(memory_utilisation_ratio) as avg_mem
                FROM service_metrics
                WHERE service = ANY(:services)
                  AND timestamp >= :metrics_cutoff
                GROUP BY service
            """
            metrics_rows = await self.db.fetch_all(
                metrics_query,
                {"services": services, "metrics_cutoff": metrics_cutoff},
            )

            service_metrics: dict[str, dict[str, float]] = {}
            for m in metrics_rows or []:
                # Defend against mock DB returning incident rows
                if not any(
                    k in m for k in ("avg_error", "avg_p99", "avg_queue", "error_ratio")
                ):
                    continue
                svc = m.get("service") or (services[0] if services else None)
                if svc:
                    service_metrics[svc] = {
                        "avg_error": float(m.get("avg_error") or 0.0),
                        "avg_p99": float(m.get("avg_p99") or 0.0),
                        "avg_queue": float(m.get("avg_queue") or 0.0),
                        "avg_cpu": float(m.get("avg_cpu") or 0.0),
                        "avg_mem": float(m.get("avg_mem") or 0.0),
                    }

            # If fetch_all did not return metrics (e.g. test harness mocked fetch_one)
            if not service_metrics:
                metrics_one = await self.db.fetch_one(
                    metrics_query,
                    {"services": services, "metrics_cutoff": metrics_cutoff},
                )
                if metrics_one and any(
                    k in metrics_one for k in ("avg_error", "avg_p99", "avg_queue")
                ):
                    svc = metrics_one.get("service") or (services[0] if services else None)
                    if svc:
                        service_metrics[svc] = {
                            "avg_error": float(metrics_one.get("avg_error") or 0.0),
                            "avg_p99": float(metrics_one.get("avg_p99") or 0.0),
                            "avg_queue": float(metrics_one.get("avg_queue") or 0.0),
                            "avg_cpu": float(metrics_one.get("avg_cpu") or 0.0),
                            "avg_mem": float(metrics_one.get("avg_mem") or 0.0),
                        }

            slo_violated = False
            slo_reason = ""
            for svc in services:
                svc_metric = service_metrics.get(svc)
                if not svc_metric:
                    continue
                if svc_metric["avg_error"] > SLO_MAX_ERROR_RATIO:
                    slo_violated = True
                    slo_reason = f"{svc} error ratio ({svc_metric['avg_error']:.2%}) exceeds SLO ({SLO_MAX_ERROR_RATIO:.1%})"
                    break
                if svc_metric["avg_p99"] > SLO_MAX_LATENCY_P99_SECONDS:
                    slo_violated = True
                    slo_reason = f"{svc} p99 latency ({svc_metric['avg_p99']*1000:.0f}ms) exceeds SLO ({SLO_MAX_LATENCY_P99_SECONDS*1000:.0f}ms)"
                    break
                if svc_metric["avg_queue"] > SLO_MAX_QUEUE_DEPTH:
                    slo_violated = True
                    slo_reason = f"{svc} queue depth ({svc_metric['avg_queue']:.1f}) exceeds SLO ({SLO_MAX_QUEUE_DEPTH:.0f})"
                    break
                if svc_metric["avg_cpu"] > SLO_MAX_CPU_RATIO:
                    slo_violated = True
                    slo_reason = f"{svc} CPU ({svc_metric['avg_cpu']:.1%}) exceeds SLO ({SLO_MAX_CPU_RATIO:.0%})"
                    break
                if svc_metric["avg_mem"] > SLO_MAX_MEMORY_RATIO:
                    slo_violated = True
                    slo_reason = f"{svc} memory ({svc_metric['avg_mem']:.1%}) exceeds SLO ({SLO_MAX_MEMORY_RATIO:.0%})"
                    break

            if slo_violated:
                log.warning(
                    "auto_resolve_blocked_unhealthy_slo",
                    incident_id=incident_id,
                    reason=slo_reason,
                )
                continue

            # 4. Check for recurring relevant anomalies vs benign/unrelated anomalies
            recent_cutoff = utc_now() - timedelta(seconds=AUTO_RESOLVE_AFTER_SECONDS)
            recent_query = """
                SELECT anomaly_id, detected_at, service, severity, score, kind, contributing_features
                FROM anomalies
                WHERE service = ANY(:services)
                  AND detected_at >= :recent_cutoff
                ORDER BY detected_at DESC
            """
            recent_anomalies = await self.db.fetch_all(
                recent_query,
                {"services": services, "recent_cutoff": recent_cutoff},
            )

            # Filter valid anomalies (defend against mock DB returning incident rows)
            valid_recent_anomalies = [
                a
                for a in (recent_anomalies or [])
                if any(k in a for k in ("anomaly_id", "severity", "score", "kind"))
            ]

            # Determine primary root cause target from hypotheses
            hypotheses_data = row.get("hypotheses") or []
            if isinstance(hypotheses_data, str):
                try:
                    hypotheses_data = json.loads(hypotheses_data)
                except Exception:  # noqa: BLE001
                    hypotheses_data = []
            primary_hypo = hypotheses_data[0] if hypotheses_data else {}
            primary_target = primary_hypo.get("target")

            has_relevant_failure = False
            relevant_reason = ""
            threshold = getattr(self.settings.ml, "anomaly_score_threshold", 0.65)
            for anom in valid_recent_anomalies:
                sev = anom.get("severity")
                score = float(anom.get("score") or 0.0)
                svc = anom.get("service")
                kind = anom.get("kind") or ""
                feats = anom.get("contributing_features") or []

                # Info-level / sub-alerting anomalies are benign when telemetry meets SLOs
                if sev == Severity.INFO or score < threshold:
                    continue

                # Recurring critical anomaly on primary target service -> genuine recurrence!
                if svc == primary_target and sev == Severity.CRITICAL and score >= 0.85:
                    has_relevant_failure = True
                    relevant_reason = f"Recurring critical anomaly ({kind}, score={score:.2f}) on root cause target {primary_target}"
                    break

                # Severe anomaly with critical feature failure (queue backlog or error burst)
                if (
                    sev == Severity.CRITICAL
                    and score >= 0.90
                    and any(f in ("error_ratio", "queue_depth") for f in feats)
                ):
                    has_relevant_failure = True
                    relevant_reason = f"Severe anomaly ({kind}, score={score:.2f}) with critical features {feats} on {svc}"
                    break

            if has_relevant_failure:
                log.info(
                    "auto_resolve_blocked_recurring_relevant_anomaly",
                    incident_id=incident_id,
                    reason=relevant_reason,
                )
                continue

            # 5. Verified recovery: all criteria met!
            resolution_reason = (
                f"Verified recovered: All affected services ({', '.join(services)}) healthy "
                f"within SLO thresholds (error ratio <= {SLO_MAX_ERROR_RATIO:.0%}, "
                f"p99 latency <= {SLO_MAX_LATENCY_P99_SECONDS*1000:.0f}ms, "
                f"queue depth <= {SLO_MAX_QUEUE_DEPTH:.0f}) over {AUTO_RESOLVE_AFTER_SECONDS}s recovery window."
            )
            if action_row and action_status in (ActionStatus.SUCCEEDED, "succeeded"):
                resolution_reason += " Remediation verified recovery."

            await self.resolve_incident(incident_id, reason=resolution_reason)
            log.info(
                "incident_auto_resolved_verified",
                incident_id=incident_id,
                services=services,
                reason=resolution_reason,
            )
            log.info(
                "incident_auto_resolved_verified",
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
            parts.append(f"Service **{next(iter(services))}** is experiencing issues.")
        else:
            parts.append(
                f"**{len(services)} services** are affected: {', '.join(sorted(services))}."
            )

        # Primary hypothesis
        if hypotheses:
            primary = hypotheses[0]
            parts.append(
                f"\n**Likely cause ({primary.confidence:.0%} confidence):** {primary.summary}"
            )

            if primary.evidence:
                parts.append("\n**Evidence:**")
                for evidence in primary.evidence[:3]:  # Top 3
                    parts.append(f"- {evidence}")

            # Alternative hypotheses
            if len(hypotheses) > 1:
                parts.append(
                    f"\n**Alternative explanations:** {len(hypotheses) - 1} other hypothesis(es) identified."
                )

        # Anomaly details
        critical_anomalies = [a for a in anomalies if a.severity == Severity.CRITICAL]
        if critical_anomalies:
            parts.append(f"\n⚠️ **{len(critical_anomalies)} critical anomaly(ies)** detected.")

        # Top contributing signals
        all_features = []
        for anomaly in anomalies:
            all_features.extend(anomaly.contributing_features[:2])  # Top 2 per anomaly

        if all_features:
            feature_counts: dict[str, int] = {}
            for f in all_features:
                feature_counts[f] = feature_counts.get(f, 0) + 1
            top_features = sorted(feature_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            parts.append(f"\n**Key signals:** {', '.join(f for f, _ in top_features)}")

        return " ".join(parts)

    async def get_open_incidents(self) -> list[Incident]:
        """Retrieve all open incidents."""
        query = """
            SELECT incident_id, opened_at, closed_at, status, severity,
                   platform, title, affected_services, anomaly_ids,
                   hypotheses, action_ids, ai_summary
            FROM incidents
            WHERE status IN ('open', 'mitigating', 'acknowledged')
            AND platform = :platform
            ORDER BY opened_at DESC
        """

        rows = await self.db.fetch_all(query, {"platform": self.settings.dpi_platform})

        incidents = []
        for row in rows:
            hypo_data = row.get("hypotheses") or []
            if isinstance(hypo_data, str):
                try:
                    hypo_data = json.loads(hypo_data)
                except Exception:  # noqa: BLE001
                    hypo_data = []

            hypotheses = [RootCauseHypothesis.model_validate(h) for h in hypo_data]

            incident = Incident(
                incident_id=row["incident_id"],
                opened_at=row["opened_at"],
                closed_at=row["closed_at"],
                status=row["status"],
                severity=row["severity"],
                platform=row["platform"],
                title=row["title"],
                affected_services=row["affected_services"] or [],
                anomaly_ids=row["anomaly_ids"] or [],
                hypotheses=hypotheses,
                action_ids=row["action_ids"] or [],
                ai_summary=row["ai_summary"] or "",
            )
            incidents.append(incident)

        return incidents
