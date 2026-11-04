"""Main healing engine - consumes anomalies and incidents, executes playbooks.

The engine:
1. Consumes anomaly and incident streams from Redis
2. Selects appropriate playbook based on anomaly kind and incident hypothesis
3. Checks all safety rails before execution
4. Captures before/after metric snapshots
5. Executes playbook via executor interface
6. Persists action to healing_actions table
7. Publishes action to aiops.actions stream

All actions are recorded, including skipped ones, so safety rail effectiveness is auditable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from aiops_common.bus import EventBus, Stream
from aiops_common.config import get_settings
from aiops_common.constants import TABLE_ACTIONS, AiopsService, ActionStatus
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, HealingAction, Incident

from .executor import DockerExecutor, ExecutorInterface
from .playbooks import PlaybookExecutor, PlaybookRegistry
from .safety import SafetyRails
from .snapshot import MetricSnapshot

SERVICE_NAME = str(AiopsService.HEALING_ENGINE)

log = get_logger(SERVICE_NAME)
settings = get_settings()


class HealingEngine:
    """Main healing engine that processes anomalies and incidents."""

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        executor: ExecutorInterface,
        safety: SafetyRails,
        snapshot: MetricSnapshot,
        playbook_executor: PlaybookExecutor,
    ):
        self.db = db
        self.bus = bus
        self.executor = executor
        self.safety = safety
        self.snapshot = snapshot
        self.playbook_executor = playbook_executor

        self._anomaly_consumer_task: asyncio.Task[None] | None = None
        self._incident_consumer_task: asyncio.Task[None] | None = None
        self._running = False

        # Track recently handled anomalies to avoid duplicates
        self._handled_anomalies: set[str] = set()
        self._max_handled_cache = 1000

    async def start(self) -> None:
        """Start consuming anomaly and incident streams."""
        if self._running:
            log.warning("healing_engine.already_running")
            return

        self._running = True

        # Start consumer tasks
        self._anomaly_consumer_task = asyncio.create_task(
            self._consume_anomalies(),
            name="healing_anomaly_consumer",
        )

        self._incident_consumer_task = asyncio.create_task(
            self._consume_incidents(),
            name="healing_incident_consumer",
        )

        log.info(
            "healing_engine.started",
            enabled=settings.healing.enabled,
            max_actions_per_hour=settings.healing.max_actions_per_hour,
            cooldown_seconds=settings.healing.cooldown_seconds,
            min_confidence=settings.healing.min_confidence,
        )

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        if not self._running:
            return

        self._running = False

        if self._anomaly_consumer_task:
            self._anomaly_consumer_task.cancel()
            try:
                await self._anomaly_consumer_task
            except asyncio.CancelledError:
                pass

        if self._incident_consumer_task:
            self._incident_consumer_task.cancel()
            try:
                await self._incident_consumer_task
            except asyncio.CancelledError:
                pass

        log.info("healing_engine.stopped")

    async def _consume_anomalies(self) -> None:
        """Consume anomaly events and trigger healing actions."""
        async for event in self.bus.subscribe(
            Stream.ANOMALIES, group="healing-engine", consumer="healing-1"
        ):
            if not self._running:
                break

            try:
                anomaly = AnomalyEvent.model_validate(event.data)

                # Skip if already handled
                if anomaly.anomaly_id in self._handled_anomalies:
                    log.debug(
                        "healing_engine.anomaly_already_handled",
                        anomaly_id=anomaly.anomaly_id,
                    )
                    continue

                log.info(
                    "healing_engine.anomaly_received",
                    anomaly_id=anomaly.anomaly_id,
                    service=anomaly.service,
                    kind=anomaly.kind,
                    score=anomaly.score,
                )

                # Process anomaly
                await self._process_anomaly(anomaly, incident=None)

                # Mark as handled
                self._handled_anomalies.add(anomaly.anomaly_id)
                if len(self._handled_anomalies) > self._max_handled_cache:
                    # Trim oldest entries (approximate FIFO)
                    excess = len(self._handled_anomalies) - self._max_handled_cache
                    for _ in range(excess):
                        self._handled_anomalies.pop()

            except Exception as exc:
                log.error(
                    "healing_engine.anomaly_consume_error",
                    error=str(exc),
                    event=event,
                    exc_info=True,
                )

    async def _consume_incidents(self) -> None:
        """Consume incident events and trigger healing with RCA context."""
        async for event in self.bus.subscribe(
            Stream.INCIDENTS, group="healing-engine", consumer="healing-1"
        ):
            if not self._running:
                break

            try:
                incident = Incident.model_validate(event.data)

                # Only process newly opened incidents
                if incident.status != "open":
                    continue

                # Process incident - get primary anomaly and heal with incident context
                if incident.anomaly_ids:
                    # Fetch the first anomaly for this incident
                    anomaly_id = incident.anomaly_ids[0]
                    anomaly = await self._fetch_anomaly(anomaly_id)
                    
                    if anomaly:
                        await self._process_anomaly(anomaly, incident=incident)

            except Exception as exc:
                log.error(
                    "healing_engine.incident_consume_error",
                    error=str(exc),
                    event=event,
                    exc_info=True,
                )

    async def _fetch_anomaly(self, anomaly_id: str) -> AnomalyEvent | None:
        """Fetch anomaly from database."""
        query = """
            SELECT anomaly_id, detected_at, service, severity,
                   score, contributing_features, kind, incident_id
            FROM anomalies
            WHERE anomaly_id = :anomaly_id
        """
        
        row = await self.db.fetch_one(query, {"anomaly_id": anomaly_id})
        
        if not row:
            return None
        
        return AnomalyEvent(
            anomaly_id=row["anomaly_id"],
            detected_at=row["detected_at"],
            service=row["service"],
            severity=row["severity"],
            score=row["score"],
            contributing_features=row["contributing_features"] or [],
            kind=row["kind"],
            incident_id=row["incident_id"],
        )

    async def _process_anomaly(
        self,
        anomaly: AnomalyEvent,
        incident: Incident | None,
    ) -> None:
        """Process an anomaly and execute healing action if appropriate.
        
        Args:
            anomaly: The anomaly event
            incident: Optional incident with RCA hypotheses
        """
        # Select playbook
        playbook, parameters = PlaybookRegistry.select_playbook(anomaly, incident)

        # Determine confidence
        confidence = 0.0
        if incident and incident.hypotheses:
            confidence = incident.hypotheses[0].confidence
        else:
            # For anomaly-only healing, use anomaly score as confidence proxy
            confidence = min(1.0, anomaly.score)

        # Create action record
        action = HealingAction(
            playbook=playbook,
            target_service=anomaly.service,
            platform=settings.dpi_platform,
            incident_id=incident.incident_id if incident else None,
            anomaly_id=anomaly.anomaly_id,
            trigger_reason=self._build_trigger_reason(anomaly, incident),
            confidence=confidence,
            parameters=parameters,
            dry_run=not settings.healing.enabled,
        )

        # Check safety rails
        allowed, status, reason = await self.safety.check_all(
            anomaly.service,
            confidence,
        )

        if not allowed:
            # Action blocked by safety rail
            action.status = status
            action.result_message = reason
            await self._persist_action(action)
            await self._publish_action(action)
            
            log.info(
                "healing_engine.action_blocked",
                action_id=action.action_id,
                service=anomaly.service,
                playbook=playbook,
                status=status,
                reason=reason,
            )
            return

        # Safety checks passed - capture before snapshot
        action.before_snapshot = await self.snapshot.capture(anomaly.service)

        # Execute playbook
        action.status = ActionStatus.RUNNING
        action.started_at = datetime.now(timezone.utc)
        
        # Skip persisting RUNNING status to avoid async deadlock
        # We'll persist once at the end with final status
        
        log.info(
            "healing_engine.calling_playbook_executor",
            action_id=action.action_id,
            playbook=playbook,
            service=anomaly.service,
        )

        success, message = await self.playbook_executor.execute(
            playbook,
            anomaly.service,
            parameters,
        )
        
        log.info(
            "healing_engine.playbook_executor_returned",
            action_id=action.action_id,
            success=success,
            message=message,
        )

        # Update action status
        action.finished_at = datetime.now(timezone.utc)
        action.status = ActionStatus.SUCCEEDED if success else ActionStatus.FAILED
        action.result_message = message

        if success:
            # Wait for system to stabilize, then capture after snapshot
            await asyncio.sleep(30)  # 30 second settle time
            action.after_snapshot = await self.snapshot.capture(anomaly.service)
            
            # Record action in safety rails
            await self.safety.record_action_executed(anomaly.service)

        # Persist and publish
        await self._persist_action(action)
        await self._publish_action(action)

        log.info(
            "healing_engine.action_completed",
            action_id=action.action_id,
            service=anomaly.service,
            playbook=playbook,
            status=action.status,
            success=success,
            message=message,
        )

    def _build_trigger_reason(
        self,
        anomaly: AnomalyEvent,
        incident: Incident | None,
    ) -> str:
        """Build human-readable trigger reason."""
        if incident and incident.hypotheses:
            primary = incident.hypotheses[0]
            return f"Incident {incident.incident_id}: {primary.summary[:100]}"
        else:
            return (
                f"Anomaly detected: {anomaly.kind} "
                f"(score: {anomaly.score:.2f})"
            )

    async def _persist_action(self, action: HealingAction) -> None:
        """Save action to healing_actions table."""
        import json
        
        query = f"""
            INSERT INTO {TABLE_ACTIONS} (
                action_id, created_at, started_at, finished_at,
                playbook, target_service, target_instance, platform,
                incident_id, anomaly_id, trigger_reason, confidence,
                status, dry_run, parameters, result_message,
                before_snapshot, after_snapshot
            ) VALUES (
                :action_id, :created_at, :started_at, :finished_at,
                :playbook, :target_service, :target_instance, :platform,
                :incident_id, :anomaly_id, :trigger_reason, :confidence,
                :status, :dry_run, :parameters::jsonb, :result_message,
                :before_snapshot::jsonb, :after_snapshot::jsonb
            )
            ON CONFLICT (action_id) DO UPDATE SET
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status,
                result_message = EXCLUDED.result_message,
                after_snapshot = EXCLUDED.after_snapshot
        """

        await self.db.execute(
            query,
            {
                "action_id": action.action_id,
                "created_at": action.created_at,
                "started_at": action.started_at,
                "finished_at": action.finished_at,
                "playbook": action.playbook,
                "target_service": action.target_service,
                "target_instance": action.target_instance,
                "platform": action.platform,
                "incident_id": action.incident_id,
                "anomaly_id": action.anomaly_id,
                "trigger_reason": action.trigger_reason,
                "confidence": action.confidence,
                "status": action.status,
                "dry_run": action.dry_run,
                "parameters": json.dumps(action.parameters),
                "result_message": action.result_message,
                "before_snapshot": json.dumps(action.before_snapshot),
                "after_snapshot": json.dumps(action.after_snapshot),
            },
        )

    async def _publish_action(self, action: HealingAction) -> None:
        """Publish action to aiops.actions stream for dashboard."""
        await self.bus.publish(Stream.ACTIONS, action.model_dump(mode="json"))

    async def get_status(self) -> dict[str, Any]:
        """Return engine status for health checks."""
        return {
            "running": self._running,
            "enabled": settings.healing.enabled,
            "max_actions_per_hour": settings.healing.max_actions_per_hour,
            "cooldown_seconds": settings.healing.cooldown_seconds,
            "min_confidence": settings.healing.min_confidence,
            "handled_anomalies": len(self._handled_anomalies),
        }
