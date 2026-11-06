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
from datetime import UTC, datetime
from typing import Any

from aiops_common.bus import EventBus, Stream
from aiops_common.config import get_settings
from aiops_common.constants import (
    TABLE_ACTIONS,
    TABLE_INCIDENTS,
    ActionStatus,
    AiopsService,
    PlaybookName,
)
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, HealingAction, Incident

from .executor import ExecutorInterface
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
    ) -> None:
        self.db = db
        self.bus = bus
        self.executor = executor
        self.safety = safety
        self.snapshot = snapshot
        self.playbook_executor = playbook_executor
        self.settings = get_settings().healing

        self._anomaly_consumer_task: asyncio.Task[None] | None = None
        self._incident_consumer_task: asyncio.Task[None] | None = None
        self._prediction_consumer_task: asyncio.Task[None] | None = None
        self._running = False

        # Track recently handled anomalies to avoid duplicates
        self._handled_anomalies: set[str] = set()
        self._max_handled_cache = 1000
        # Track recently handled predictions to avoid duplicate scale-outs
        self._handled_predictions: set[str] = set()

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

        self._prediction_consumer_task = asyncio.create_task(
            self._consume_predictions(),
            name="healing_prediction_consumer",
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

        if self._prediction_consumer_task:
            self._prediction_consumer_task.cancel()
            try:
                await self._prediction_consumer_task
            except asyncio.CancelledError:
                pass

        log.info("healing_engine.stopped")

    async def _consume_anomalies(self) -> None:
        """Consume anomaly events and trigger healing actions."""
        while self._running:
            try:
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
                            stream_event=str(event),
                            exc_info=True,
                        )
            except Exception as exc:
                if not self._running:
                    break
                log.warning("healing_engine.anomaly_consumer_restarting", error=str(exc))
                await asyncio.sleep(2)

    async def _consume_incidents(self) -> None:
        """Consume incident events and trigger healing with RCA context."""
        while self._running:
            try:
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
                            stream_event=str(event),
                            exc_info=True,
                        )
            except Exception as exc:
                if not self._running:
                    break
                log.warning("healing_engine.incident_consumer_restarting", error=str(exc))
                await asyncio.sleep(2)

    async def _consume_predictions(self) -> None:
        """Consume traffic forecast events and trigger pre-emptive scaling.

        When the traffic predictor forecasts a TPS load significantly above the
        current baseline (peak_predicted_tps > current_tps * scale_ratio, default 1.5x)
        the engine synthesises a synthetic TRAFFIC_SURGE anomaly and routes it through
        the normal playbook + safety-rail pipeline so the action is fully auditable.
        """
        from datetime import UTC, datetime as _dt

        from aiops_common.constants import AnomalyKind, Severity
        from aiops_common.schemas import AnomalyEvent as _AnomalyEvent
        from aiops_common.schemas import PredictionEvent as _PredictionEvent

        # Ratio above which we decide to pre-emptively scale out.
        scale_ratio: float = getattr(self.settings, "preemptive_scale_ratio", 1.5)

        while self._running:
            try:
                async for event in self.bus.subscribe(
                    Stream.PREDICTIONS, group="healing-engine-pred", consumer="healing-pred-1"
                ):
                    if not self._running:
                        break

                    try:
                        prediction = _PredictionEvent.model_validate(event.data)

                        # Only act on the 1-hour horizon to avoid over-reacting
                        if prediction.horizon_hours != 1:
                            continue

                        # Skip if already handled this prediction
                        if prediction.prediction_id in self._handled_predictions:
                            continue

                        # Only act when the forecast recommends scaling out
                        if prediction.scaling_action != "scale_out":
                            continue

                        # Additional guard: peak must exceed current load by the configured ratio
                        if prediction.current_tps > 0:
                            ratio = prediction.peak_predicted_tps / prediction.current_tps
                            if ratio < scale_ratio:
                                log.debug(
                                    "healing_engine.prediction_below_threshold",
                                    service=prediction.service,
                                    ratio=round(ratio, 3),
                                    threshold=scale_ratio,
                                )
                                continue

                        log.info(
                            "healing_engine.preemptive_scale_triggered",
                            service=prediction.service,
                            horizon_hours=prediction.horizon_hours,
                            current_tps=prediction.current_tps,
                            peak_predicted_tps=prediction.peak_predicted_tps,
                            recommended_replicas=prediction.recommended_replicas,
                        )

                        # Synthesise a lightweight AnomalyEvent so we can reuse _process_anomaly
                        # and get the full safety-rail + audit-trail treatment.
                        synthetic = _AnomalyEvent(
                            service=prediction.service,
                            platform=prediction.platform,
                            severity=Severity.WARNING,
                            score=0.75,
                            kind=str(AnomalyKind.TRAFFIC_SURGE),
                            contributing_features=["predicted_tps", "recommended_replicas"],
                            detected_at=_dt.now(UTC),
                        )

                        await self._process_anomaly(synthetic, incident=None)

                        self._handled_predictions.add(prediction.prediction_id)
                        if len(self._handled_predictions) > self._max_handled_cache:
                            excess = len(self._handled_predictions) - self._max_handled_cache
                            for _ in range(excess):
                                self._handled_predictions.pop()

                    except Exception as exc:
                        log.error(
                            "healing_engine.prediction_consume_error",
                            error=str(exc),
                            stream_event=str(event),
                            exc_info=True,
                        )

            except Exception as exc:
                if not self._running:
                    break
                log.warning(
                    "healing_engine.prediction_consumer_restarting",
                    error=str(exc),
                )
                import asyncio as _asyncio
                await _asyncio.sleep(5)

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
        # Fetch previous actions for this incident (if any) to prevent repeating failed playbooks
        previous_actions: list[dict[str, Any]] = []
        if incident:
            try:
                query = f"""
                    SELECT action_id, playbook, status, result_message, parameters, created_at
                    FROM {TABLE_ACTIONS}
                    WHERE incident_id = :incident_id
                    ORDER BY created_at ASC
                """
                rows = await self.db.fetch_all(query, {"incident_id": incident.incident_id})
                previous_actions = [dict(r) for r in rows]
            except Exception as exc:  # noqa: BLE001
                log.warning("healing_engine.fetch_previous_actions_failed", error=str(exc))

        attempt_number = len(previous_actions) + 1

        # Capture telemetry snapshot before playbook selection so decision is telemetry-aware
        before_snapshot = await self.snapshot.capture(anomaly.service)

        # Select playbook based on root cause, live telemetry, and action history
        playbook, parameters = PlaybookRegistry.select_playbook(
            anomaly,
            incident,
            snapshot=before_snapshot,
            previous_actions=previous_actions,
        )

        parameters["attempt_number"] = attempt_number
        requires_manual = parameters.get("requires_manual_intervention", False)

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
            attempt_number=attempt_number,
            requires_manual_intervention=requires_manual,
            parameters=parameters,
            dry_run=not settings.healing.enabled,
            before_snapshot=before_snapshot,
        )

        # If playbook selection determined manual intervention is required
        if requires_manual:
            action.status = ActionStatus.REQUIRES_MANUAL_INTERVENTION
            action.result_message = parameters.get(
                "reason", "Automatic remediation stopped: manual intervention required."
            )
            action.execution_mode = "simulated"
            await self._persist_action(action)
            await self._publish_action(action)
            log.info(
                "healing_engine.requires_manual_intervention",
                action_id=action.action_id,
                service=anomaly.service,
                reason=action.result_message,
            )
            return

        # Check safety rails (includes maximum attempts per incident and cooldown)
        allowed, status, reason = await self.safety.check_all(
            anomaly.service,
            confidence,
            incident_id=incident.incident_id if incident else None,
            playbook=playbook,
        )

        if not allowed:
            # Action blocked by safety rail
            action.status = status
            action.result_message = reason
            action.execution_mode = "simulated"
            if status == ActionStatus.SKIPPED_MAX_ATTEMPTS:
                action.requires_manual_intervention = True
                action.parameters["requires_manual_intervention"] = True

            try:
                curr = await self.executor.get_replica_count(anomaly.service)
                action.actual_replicas_before = curr
                action.actual_replicas_after = curr
                if playbook == PlaybookName.SCALE_OUT_PREEMPTIVE:
                    action.simulated_replicas_after = curr + 1
                elif playbook == PlaybookName.SCALE_IN_IDLE:
                    action.simulated_replicas_after = max(1, curr - 1)
            except Exception as exc:  # noqa: BLE001
                log.debug("healing_engine.replica_count_failed", error=str(exc))

            action.parameters.update(
                {
                    "execution_mode": action.execution_mode,
                    "actual_replicas_before": action.actual_replicas_before,
                    "actual_replicas_after": action.actual_replicas_after,
                    "simulated_replicas_after": action.simulated_replicas_after,
                }
            )
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

        # Execute playbook
        action.status = ActionStatus.RUNNING
        action.started_at = datetime.now(UTC)

        log.info(
            "healing_engine.calling_playbook_executor",
            action_id=action.action_id,
            playbook=playbook,
            service=anomaly.service,
        )

        res = await self.playbook_executor.execute(
            playbook,
            anomaly.service,
            parameters,
        )

        success = res[0]
        message = res[1]
        metadata = getattr(res, "metadata", {})

        log.info(
            "healing_engine.playbook_executor_returned",
            action_id=action.action_id,
            success=success,
            message=message,
            metadata=metadata,
        )

        # Update action status and metadata
        action.finished_at = datetime.now(UTC)
        action.result_message = message

        if "status" in metadata:
            action.status = metadata["status"]
        else:
            action.status = ActionStatus.SUCCEEDED if success else ActionStatus.FAILED

        action.execution_mode = metadata.get(
            "execution_mode",
            "real"
            if not action.dry_run and action.status == ActionStatus.SUCCEEDED
            else "simulated",
        )
        action.actual_replicas_before = metadata.get("actual_replicas_before")
        action.actual_replicas_after = metadata.get("actual_replicas_after")
        action.simulated_replicas_after = metadata.get("simulated_replicas_after")

        action.parameters.update(
            {
                "execution_mode": action.execution_mode,
                "actual_replicas_before": action.actual_replicas_before,
                "actual_replicas_after": action.actual_replicas_after,
                "simulated_replicas_after": action.simulated_replicas_after,
                **{k: v for k, v in metadata.items() if k not in action.parameters},
            }
        )

        if success and action.status == ActionStatus.SUCCEEDED:
            # Configurable recovery stabilization wait
            settle_seconds = 5
            log.info(
                "healing_engine.waiting_recovery",
                seconds=settle_seconds,
                service=anomaly.service,
            )
            await asyncio.sleep(settle_seconds)

            # Check container health where applicable
            container_healthy = True
            if hasattr(self.executor, "client") and self.executor.client:
                try:
                    containers = self.executor.client.containers.list(
                        filters={"label": f"com.docker.compose.service={anomaly.service}"}
                    )
                    if not containers:
                        all_c = self.executor.client.containers.list()
                        containers = [c for c in all_c if anomaly.service in c.name]
                    if containers:
                        for c in containers:
                            c.reload()
                            if c.status not in ("running", "restarting"):
                                container_healthy = False
                                break
                except Exception as exc:  # noqa: BLE001
                    log.warning("healing_engine.container_health_check_failed", error=str(exc))

            # Capture post-action metric snapshot
            action.after_snapshot = await self.snapshot.capture(
                anomaly.service, window_seconds=60
            )

            # Actual Recovery Verification
            recovered, rec_reason, rec_details = self.snapshot.verify_recovery(
                service=anomaly.service,
                before=action.before_snapshot,
                after=action.after_snapshot,
                playbook=playbook,
                anomaly_kind=getattr(anomaly, "kind", None),
                container_healthy=container_healthy,
            )

            action.parameters["recovery_verification"] = {
                "verified": recovered,
                "reason": rec_reason,
                "container_healthy": container_healthy,
                "details": rec_details,
            }

            if recovered:
                action.status = ActionStatus.SUCCEEDED
                action.result_message = f"{message} (Recovery verified: {rec_reason})"
                await self.safety.record_action_executed(
                    anomaly.service,
                    success=True,
                    incident_id=incident.incident_id if incident else None,
                    playbook=playbook,
                )
                log.info(
                    "healing_engine.recovery_verified",
                    action_id=action.action_id,
                    service=anomaly.service,
                    reason=rec_reason,
                )
            else:
                action.status = ActionStatus.FAILED
                action.result_message = f"{message} (Recovery failed: {rec_reason})"
                await self.safety.record_action_executed(
                    anomaly.service,
                    success=False,
                    incident_id=incident.incident_id if incident else None,
                    playbook=playbook,
                )
                log.warning(
                    "healing_engine.recovery_failed",
                    action_id=action.action_id,
                    service=anomaly.service,
                    reason=rec_reason,
                )
                max_att = getattr(self.settings, "max_attempts_per_incident", 2)
                if attempt_number >= max_att:
                    action.requires_manual_intervention = True
                    action.parameters["requires_manual_intervention"] = True
                    action.result_message += (
                        f" (Reached maximum attempts {max_att}; manual intervention required)"
                    )

        # Update associated incident status and link action
        if incident:
            try:
                new_incident_status = (
                    "mitigating" if action.status == ActionStatus.SUCCEEDED else "open"
                )
                update_inc_sql = f"""
                    UPDATE {TABLE_INCIDENTS}
                    SET action_ids = array_append(action_ids, :action_id),
                        status = :status
                    WHERE incident_id = :incident_id
                """
                await self.db.execute(
                    update_inc_sql,
                    {
                        "action_id": action.action_id,
                        "status": new_incident_status,
                        "incident_id": incident.incident_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("healing_engine.update_incident_failed", error=str(exc))

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
            message=action.result_message,
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
            return f"Anomaly detected: {anomaly.kind} " f"(score: {anomaly.score:.2f})"

    async def _persist_action(self, action: HealingAction) -> None:
        """Save action to healing_actions table with full audit trail."""
        import json

        params = {
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
        }

        # Try inserting with dedicated execution_mode and replica audit columns
        try:
            query = f"""
                INSERT INTO {TABLE_ACTIONS} (
                    action_id, created_at, started_at, finished_at,
                    playbook, target_service, target_instance, platform,
                    incident_id, anomaly_id, trigger_reason, confidence,
                    status, dry_run, execution_mode,
                    actual_replicas_before, actual_replicas_after, simulated_replicas_after,
                    parameters, result_message,
                    before_snapshot, after_snapshot
                ) VALUES (
                    :action_id, :created_at, :started_at, :finished_at,
                    :playbook, :target_service, :target_instance, :platform,
                    :incident_id, :anomaly_id, :trigger_reason, :confidence,
                    :status, :dry_run, :execution_mode,
                    :actual_replicas_before, :actual_replicas_after, :simulated_replicas_after,
                    CAST(:parameters AS jsonb), :result_message,
                    CAST(:before_snapshot AS jsonb), CAST(:after_snapshot AS jsonb)
                )
                ON CONFLICT (action_id) DO UPDATE SET
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    status = EXCLUDED.status,
                    execution_mode = EXCLUDED.execution_mode,
                    actual_replicas_before = EXCLUDED.actual_replicas_before,
                    actual_replicas_after = EXCLUDED.actual_replicas_after,
                    simulated_replicas_after = EXCLUDED.simulated_replicas_after,
                    result_message = EXCLUDED.result_message,
                    parameters = EXCLUDED.parameters,
                    after_snapshot = EXCLUDED.after_snapshot
            """
            await self.db.execute(
                query,
                {
                    **params,
                    "execution_mode": action.execution_mode or "simulated",
                    "actual_replicas_before": action.actual_replicas_before,
                    "actual_replicas_after": action.actual_replicas_after,
                    "simulated_replicas_after": action.simulated_replicas_after,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("healing_engine.insert_with_replica_columns_failed", error=str(exc))
            # Fallback if table doesn't have the dedicated replica columns yet
            query_fallback = f"""
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
                    :status, :dry_run, CAST(:parameters AS jsonb), :result_message,
                    CAST(:before_snapshot AS jsonb), CAST(:after_snapshot AS jsonb)
                )
                ON CONFLICT (action_id) DO UPDATE SET
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    status = EXCLUDED.status,
                    result_message = EXCLUDED.result_message,
                    parameters = EXCLUDED.parameters,
                    after_snapshot = EXCLUDED.after_snapshot
            """
            await self.db.execute(query_fallback, params)

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
