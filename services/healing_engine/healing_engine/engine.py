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
import inspect
import json
from datetime import UTC, datetime
from typing import Any

from aiops_common.bus import EventBus, Stream
from aiops_common.config import get_settings
from aiops_common.constants import (
    TABLE_ACTIONS,
    TABLE_INCIDENTS,
    ActionStatus,
    AiopsService,
    AnomalyKind,
    PlaybookName,
)
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, HealingAction, Incident
from aiops_common.topology import get_topology

from .executor import ExecutorInterface
from .playbooks import PlaybookExecutor, PlaybookRegistry
from .safety import SafetyRails
from .snapshot import MetricSnapshot

SERVICE_NAME = str(AiopsService.HEALING_ENGINE)

log = get_logger(SERVICE_NAME)
settings = get_settings()


# How long the anomaly path waits for RCA to attach an incident before acting on the symptom
# alone. Bounded so a dead RCA engine delays remediation by seconds, never blocks it.
RCA_WAIT_SECONDS = 15.0


def _transitive_dependencies(service: str) -> set[str]:
    """Every service `service` calls, directly or indirectly."""
    return get_topology().transitive_dependencies(service)


ESCALATION_FLAG_TTL_SECONDS = 6 * 3600
# Recovery re-checks after a failed first verification (see execute path).
VERIFY_RETRIES = 2
VERIFY_RETRY_SECONDS = 20


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
            except Exception as exc:  # noqa: BLE001 - supervisor: any failure restarts the consumer
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
            except Exception as exc:  # noqa: BLE001 - supervisor: any failure restarts the consumer
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
        from datetime import UTC
        from datetime import datetime as _dt

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

            except Exception as exc:  # noqa: BLE001 - supervisor: any failure restarts the consumer
                if not self._running:
                    break
                log.warning(
                    "healing_engine.prediction_consumer_restarting",
                    error=str(exc),
                )
                import asyncio as _asyncio

                await _asyncio.sleep(5)

    async def _fetch_anomaly(self, anomaly_id: str) -> AnomalyEvent | None:
        """Fetch the complete anomaly record from the database.

        The whole record, evidence included. This used to select seven columns, so the copy
        the incident path decided on had no feature_snapshot, feature_deviations or threshold.
        That copy usually reached the decision first - the stream path waits for RCA's
        incident - and every rule that reads the evidence saw nothing: a traffic surge was
        restarted, and the correct scale-out, decided a second later from the full stream
        copy, was blocked by the cooldown the restart had claimed.
        """
        query = """
            SELECT anomaly_id, detected_at, window_start, service, platform, kind, severity,
                   score, raw_score, threshold, contributing_features, feature_snapshot,
                   feature_deviations, model_name, model_version, description, incident_id
            FROM anomalies
            WHERE anomaly_id = :anomaly_id
        """

        row = await self.db.fetch_one(query, {"anomaly_id": anomaly_id})

        if not row:
            return None

        record = dict(row)
        for column in ("feature_snapshot", "feature_deviations"):
            if isinstance(record.get(column), str):
                record[column] = json.loads(record[column])
        record["contributing_features"] = record.get("contributing_features") or []
        return AnomalyEvent.model_validate({k: v for k, v in record.items() if v is not None})

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
        # Act only on current information. The engine consumes Redis Streams through a
        # consumer group, so after any downtime it receives the whole backlog. Observed: after
        # being stopped overnight it restarted all five services within 20 s in response to
        # anomalies up to 9 hours old, and wrote ~600 action records in a minute. A stale
        # anomaly describes a state that has already changed; remediating it is guesswork.
        age = (datetime.now(UTC) - anomaly.detected_at).total_seconds()
        max_age = getattr(settings.healing, "max_event_age_seconds", 120)
        if age > max_age:
            log.debug(
                "healing_engine.stale_anomaly_ignored",
                service=anomaly.service,
                age_seconds=round(age),
                max_age_seconds=max_age,
            )
            return

        # Only act on anomalies past the alerting threshold. RCA gives a single-service
        # incident a fixed 0.90 confidence ("this service is the one affected"), and that was
        # used as the action confidence - so an info-level 0.52 blip got a service restarted.
        # Confidence about *which* service is not evidence that anything is wrong with it.
        # A crash the orchestrator already recovered (detector._report_restart): reported as an
        # incident for a human, never acted on - restarting it again would only add downtime.
        if anomaly.kind == AnomalyKind.SERVICE_RESTART:
            log.info("healing_engine.service_restart_reported_only", service=anomaly.service)
            return
        if not anomaly.is_actionable:
            log.debug(
                "healing_engine.non_actionable_anomaly_ignored",
                service=anomaly.service,
                score=round(anomaly.score, 3),
                threshold=anomaly.threshold,
            )
            return

        # An anomaly usually reaches healing before RCA has correlated it. For a cascade victim
        # that meant no incident, so the cascade rule could not fire and the victim was
        # restarted. If an open incident already contains one of this service's dependencies,
        # this anomaly belongs to it.
        if incident is None:
            incident = await self._open_incident_for_dependencies(anomaly.service)
        if incident is None:
            # Give RCA a moment to diagnose. It links each anomaly to its incident within
            # seconds; acting on the bare symptom first chose a restart for a bad deploy, and
            # the restart's cooldown then blocked the rollback RCA arrived at moments later.
            incident = await self._await_rca_incident(anomaly.anomaly_id)

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
        # Live TPS and capacity override the 5-minute averages for the decision itself.
        decision_snapshot = {
            **before_snapshot,
            **await self.snapshot.live_load(anomaly.service),
        }

        # Select playbook based on root cause, live telemetry, and action history
        playbook, parameters = PlaybookRegistry.select_playbook(
            anomaly,
            incident,
            snapshot=decision_snapshot,
            previous_actions=previous_actions,
        )

        parameters["attempt_number"] = attempt_number
        requires_manual = parameters.get("requires_manual_intervention", False)

        # The service the playbook acts on. Usually the anomalous one - but a cascade's
        # anomalies land on its *victims*, and `circuit_break_cascade` must act on the root
        # cause RCA named (trip its callers' breakers, restart it). Acting on the victim
        # would trip breakers on the wrong edges and restart a healthy service. Keying the
        # safety claim and cooldown on the root also stops each victim from remediating the
        # same root separately.
        target = anomaly.service
        # The key the safety rails (claim, cooldown) use. Normally the target itself.
        safety_target = target
        if playbook == PlaybookName.CIRCUIT_BREAK_CASCADE and parameters.get(
            "downstream_target"
        ):
            target = str(parameters["downstream_target"])
            parameters["victim_service"] = anomaly.service
            # Tripping callers' breakers is a different action from remediating the root, so
            # it gets its own cooldown key. Sharing the root's key meant that right after the
            # root was restarted every breaker attempt was blocked "in cooldown" - observed:
            # 28 blocked, breakers opened 6 minutes into the cascade. The root is restarted
            # by this playbook only if it was not remediated recently.
            safety_target = f"{target}:breakers"
            root_in_cooldown, _ = await self.safety._check_cooldown(target)
            parameters["restart_root"] = not root_in_cooldown

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
            target_service=target,
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
                service=target,
                reason=action.result_message,
            )
            return

        # Check safety rails (includes maximum attempts per incident and cooldown)
        allowed, status, reason = await self.safety.check_all(
            safety_target,
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
                # Escalate once per incident. Every later anomaly in the incident hits the
                # same cap; recording each one wrote 37 identical rows in five minutes.
                if incident is not None and not await self._first_escalation(
                    incident.incident_id
                ):
                    log.debug(
                        "healing_engine.max_attempts_already_escalated",
                        incident_id=incident.incident_id,
                        service=target,
                    )
                    return
                action.requires_manual_intervention = True
                action.parameters["requires_manual_intervention"] = True

            try:
                curr = await self.executor.get_replica_count(target)
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
                service=target,
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
            service=target,
        )

        res = await self.playbook_executor.execute(
            playbook,
            target,
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
            # Two metrics-agent samples (15 s apart) must land after the action, or the
            # "after" snapshot has nothing post-action to judge.
            settle_seconds = 35
            log.info(
                "healing_engine.waiting_recovery",
                seconds=settle_seconds,
                service=target,
            )
            await asyncio.sleep(settle_seconds)

            # Check container health where applicable
            # Executor-specific: Docker container status, or Kubernetes pod availability.
            container_healthy = True
            try:
                verdict: Any = self.executor.service_healthy(target)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
                if isinstance(verdict, bool):
                    container_healthy = verdict
            except Exception as exc:  # noqa: BLE001
                log.warning("healing_engine.container_health_check_failed", error=str(exc))

            # Capture post-action metric snapshot
            # From when the action *finished*: a Docker restart takes 15-20 s, and samples
            # taken while the old process was still stopping described the fault.
            action.after_snapshot = await self.snapshot.capture(
                target, since=action.finished_at
            )

            # Actual Recovery Verification
            recovered, rec_reason, rec_details = self.snapshot.verify_recovery(
                service=target,
                before=action.before_snapshot,
                after=action.after_snapshot,
                playbook=playbook,
                anomaly_kind=getattr(anomaly, "kind", None),
                container_healthy=container_healthy,
            )

            # Re-check before declaring failure, on the newest samples only. While a
            # container restarts, Prometheus keeps serving its last scraped gauges for
            # minutes, so the first post-action sample can still read the fault (observed:
            # a rollback marked failed on queue depth 60, read 0 thirty seconds later).
            for _ in range(VERIFY_RETRIES):
                if recovered:
                    break
                await asyncio.sleep(VERIFY_RETRY_SECONDS)
                action.after_snapshot = await self.snapshot.capture(
                    target, window_seconds=VERIFY_RETRY_SECONDS
                )
                recovered, rec_reason, rec_details = self.snapshot.verify_recovery(
                    service=target,
                    before=action.before_snapshot,
                    after=action.after_snapshot,
                    playbook=playbook,
                    anomaly_kind=getattr(anomaly, "kind", None),
                    container_healthy=container_healthy,
                )
            if (
                playbook == PlaybookName.CIRCUIT_BREAK_CASCADE
                and not parameters.get("restart_root", True)
                and success
            ):
                # Breakers only: the action's own effect is "callers stopped waiting on the
                # root", which the playbook already confirmed. Judging it by the root's p99
                # seconds later is meaningless - that is a 5-minute rate window still full of
                # the fault's requests (observed 0.274 s, marked failed, incident stuck in
                # "manual intervention"). Service recovery is judged by the SLO-window
                # auto-resolve, which waits for a clean 120 s.
                recovered = True
                rec_reason = "breakers opened on all reachable callers; service recovery judged by SLO auto-resolve"

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
                    safety_target,
                    success=True,
                    incident_id=incident.incident_id if incident else None,
                    playbook=playbook,
                )
                log.info(
                    "healing_engine.recovery_verified",
                    action_id=action.action_id,
                    service=target,
                    reason=rec_reason,
                )
            else:
                action.status = ActionStatus.FAILED
                action.result_message = f"{message} (Recovery failed: {rec_reason})"
                await self.safety.record_action_executed(
                    safety_target,
                    success=False,
                    incident_id=incident.incident_id if incident else None,
                    playbook=playbook,
                )
                log.warning(
                    "healing_engine.recovery_failed",
                    action_id=action.action_id,
                    service=target,
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
                        -- A resolved incident stays resolved: an action that finishes after
                        -- the incident closed (a long restart, a manual resolve) used to
                        -- reopen it as "mitigating", where nothing would ever close it again.
                        status = CASE WHEN status = 'resolved' THEN status ELSE :status END
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
            service=target,
            playbook=playbook,
            status=action.status,
            success=success,
            message=action.result_message,
        )

    async def _await_rca_incident(self, anomaly_id: str) -> Incident | None:
        """Wait up to RCA_WAIT_SECONDS for RCA to attach this anomaly to an incident."""
        deadline = asyncio.get_running_loop().time() + RCA_WAIT_SECONDS
        while True:
            try:
                row = await self.db.fetch_one(
                    f"""
                    SELECT i.incident_id, i.opened_at, i.status, i.severity, i.platform,
                           i.title, i.affected_services, i.anomaly_ids, i.hypotheses,
                           i.ai_summary
                    FROM anomalies a JOIN {TABLE_INCIDENTS} i ON i.incident_id = a.incident_id
                    WHERE a.anomaly_id = :anomaly_id
                    """,
                    {"anomaly_id": anomaly_id},
                )
            except Exception as exc:  # noqa: BLE001 - diagnosis is best-effort
                log.warning("healing_engine.rca_wait_failed", error=str(exc))
                return None
            if row:
                return self._incident_from_row(row)
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(1.0)

    def _incident_from_row(self, row: Any) -> Incident | None:
        data = dict(row)
        hypotheses = data.get("hypotheses") or []
        if isinstance(hypotheses, str):
            hypotheses = json.loads(hypotheses)
        data["hypotheses"] = hypotheses
        data["affected_services"] = list(data.get("affected_services") or [])
        data["anomaly_ids"] = list(data.get("anomaly_ids") or [])
        try:
            return Incident.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("healing_engine.incident_parse_failed", error=str(exc))
            return None

    async def _open_incident_for_dependencies(self, service: str) -> Incident | None:
        """The newest open incident involving a service that `service` depends on."""
        dependencies = sorted(_transitive_dependencies(service))
        if not dependencies:
            return None
        try:
            row = await self.db.fetch_one(
                """
                SELECT incident_id, opened_at, status, severity, platform, title,
                       affected_services, anomaly_ids, hypotheses, ai_summary
                FROM incidents
                WHERE status IN ('open', 'mitigating')
                  AND affected_services && :dependencies
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                {"dependencies": dependencies},
            )
        except Exception as exc:  # noqa: BLE001 - context is best-effort, never blocking
            log.warning("healing_engine.incident_lookup_failed", error=str(exc))
            return None
        if not row:
            return None
        return self._incident_from_row(row)

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

    async def _first_escalation(self, incident_id: str) -> bool:
        """True the first time an incident hits the attempt cap (atomic across engines)."""
        try:
            return await self.bus.claim_flag(
                f"aiops:healing:escalated:{incident_id}",
                "1",
                ttl_seconds=ESCALATION_FLAG_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - recording a duplicate beats losing one
            log.warning("healing_engine.escalation_flag_failed", error=str(exc))
            return True

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
