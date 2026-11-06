"""Root Cause Analysis engine - correlates anomalies and creates incidents.

This service:
1. Consumes anomaly events from aiops.anomalies stream
2. Correlates them using 4 methods (temporal, topological, change, event)
3. Generates ranked root cause hypotheses
4. Creates/updates incidents with AI summaries
5. Publishes incidents to aiops.incidents stream

The engine maintains a sliding window of recent anomalies for correlation and
auto-resolves incidents after a configurable idle period.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from aiops_common.bus import EventBus, Stream
from aiops_common.config import get_settings
from aiops_common.constants import AiopsService
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent

from .correlator import Correlator
from .incident_manager import IncidentManager

SERVICE_NAME = str(AiopsService.RCA_ENGINE)

log = get_logger(SERVICE_NAME)
settings = get_settings()


class RCAEngine:
    """Main RCA engine that processes anomaly stream and manages incidents."""

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        correlator: Correlator,
        incident_manager: IncidentManager,
        *,
        window_seconds: int = 300,
        auto_resolve_seconds: int = 300,
    ):
        self.db = db
        self.bus = bus
        self.correlator = correlator
        self.incident_manager = incident_manager
        self.window_seconds = window_seconds
        self.auto_resolve_seconds = auto_resolve_seconds

        # Sliding window of recent anomalies for correlation
        self.recent_anomalies: list[AnomalyEvent] = []
        self._consumer_task: asyncio.Task[None] | None = None
        self._auto_resolve_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start consuming anomaly events and processing them."""
        if self._running:
            log.warning("rca_engine.already_running")
            return

        self._running = True

        # Load recent anomalies from database to bootstrap window
        await self._bootstrap_window()

        # Start consumer task
        self._consumer_task = asyncio.create_task(
            self._consume_anomalies(),
            name="rca_anomaly_consumer",
        )

        # Start auto-resolve task
        self._auto_resolve_task = asyncio.create_task(
            self._auto_resolve_loop(),
            name="rca_auto_resolver",
        )

        log.info(
            "rca_engine.started",
            window_seconds=self.window_seconds,
            auto_resolve_seconds=self.auto_resolve_seconds,
        )

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        if not self._running:
            return

        self._running = False

        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        if self._auto_resolve_task:
            self._auto_resolve_task.cancel()
            try:
                await self._auto_resolve_task
            except asyncio.CancelledError:
                pass

        log.info("rca_engine.stopped")

    async def _bootstrap_window(self) -> None:
        """Load recent anomalies from database to initialize correlation window."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.window_seconds)

        query = """
            SELECT anomaly_id, detected_at, service, severity,
                   score as anomaly_score, contributing_features, kind as predicted_kind, incident_id
            FROM anomalies
            WHERE detected_at >= :cutoff
            ORDER BY detected_at DESC
            LIMIT 1000
        """

        rows = await self.db.fetch_all(query, {"cutoff": cutoff})

        for row in rows:
            anomaly = AnomalyEvent(
                anomaly_id=row["anomaly_id"],
                detected_at=row["detected_at"],
                service=row["service"],
                severity=row["severity"],
                score=row["anomaly_score"],
                contributing_features=row["contributing_features"] or [],
                kind=row["predicted_kind"],
                incident_id=row["incident_id"],
            )
            self.recent_anomalies.append(anomaly)

        log.info(
            "rca_engine.window_bootstrapped",
            anomaly_count=len(self.recent_anomalies),
            cutoff=cutoff.isoformat(),
        )

    async def _consume_anomalies(self) -> None:
        """Consume anomaly events and trigger correlation."""
        while self._running:
            try:
                async for event in self.bus.subscribe(
                    Stream.ANOMALIES,
                    group="rca-engine",
                    consumer=f"rca-{settings.env}",
                ):
                    if not self._running:
                        break

                    try:
                        anomaly = AnomalyEvent.model_validate(event.data)

                        # Add to sliding window
                        self.recent_anomalies.append(anomaly)
                        self._trim_window()

                        # Trigger correlation
                        await self._process_anomaly(anomaly)

                    except Exception as exc:
                        log.error(
                            "rca_engine.consume_error",
                            error=str(exc),
                            event_id=event.event_id,
                            exc_info=True,
                        )
            except Exception as exc:
                if not self._running:
                    break
                log.warning("rca_engine.consumer_restarting", error=str(exc))
                await asyncio.sleep(2)

    def _trim_window(self) -> None:
        """Remove anomalies older than window_seconds from memory."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.window_seconds)

        # Remove old anomalies
        self.recent_anomalies = [a for a in self.recent_anomalies if a.detected_at >= cutoff]

    async def _process_anomaly(self, anomaly: AnomalyEvent) -> None:
        """Correlate new anomaly and create/update incidents."""
        # Skip if already assigned to an incident
        if anomaly.incident_id:
            log.debug("rca_engine.anomaly_already_assigned", anomaly_id=anomaly.anomaly_id)
            return

        # Get unassigned anomalies in window
        unassigned = [a for a in self.recent_anomalies if not a.incident_id]

        if not unassigned:
            return

        # Run correlation
        hypotheses = await self.correlator.correlate_all(unassigned)

        if not hypotheses:
            log.debug(
                "rca_engine.no_correlation",
                anomaly_id=anomaly.anomaly_id,
                window_size=len(unassigned),
            )
            return

        # Create incident
        incident = await self.incident_manager.create_incident(unassigned, hypotheses)

        log.info(
            "rca_engine.incident_created",
            incident_id=incident.incident_id,
            anomaly_count=len(unassigned),
            hypothesis_count=len(hypotheses),
            primary_category=hypotheses[0].category if hypotheses else None,
            primary_confidence=hypotheses[0].confidence if hypotheses else None,
        )

        # Update in-memory anomalies with incident_id
        for a in self.recent_anomalies:
            if a.anomaly_id in incident.anomaly_ids:
                a.incident_id = incident.incident_id

    async def _auto_resolve_loop(self) -> None:
        """Periodically check for incidents that should be auto-resolved."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute

                await self.incident_manager.check_auto_resolve()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(
                    "rca_engine.auto_resolve_error",
                    error=str(exc),
                    exc_info=True,
                )

    async def get_status(self) -> dict[str, Any]:
        """Return engine status for health checks."""
        return {
            "running": self._running,
            "window_size": len(self.recent_anomalies),
            "window_seconds": self.window_seconds,
            "auto_resolve_seconds": self.auto_resolve_seconds,
            "unassigned_anomalies": len(
                [a for a in self.recent_anomalies if not a.incident_id]
            ),
        }
