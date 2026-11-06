"""Playbook registry and implementations.

Maps anomaly kinds and incident root causes to specific remediation playbooks.
Each playbook knows how to:
1. Decide if it applies to a given anomaly/incident
2. Execute remediation via the executor interface
3. Return success/failure with descriptive message

Playbooks are idempotent and safe to retry.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, Incident

from .executor import ExecutorInterface, ScaleResult

log = get_logger(__name__)

DEFAULT_SERVICE_CAPACITY_TPS: Final[dict[str, float]] = {
    "central-ledger": 25.0,
    "quoting-service": 20.0,
    "account-lookup-service": 25.0,
    "ml-api-adapter": 20.0,
    "transaction-request-service": 12.0,
}


class PlaybookRegistry:
    """Maps anomaly kinds, telemetry signals, and incident root causes to specific playbooks.

    Distinguishes:
    1. Process / container failure -> restart_on_oom
    2. Traffic overload (tps >= capacity threshold) -> scale_out_preemptive
    3. Persistent queue backlog (consumer stall) -> clear_queue_backlog (1st try) or scale-out / manual intervention
    4. Downstream dependency failure -> avoid restarting upstream caller
    5. CPU / Memory exhaustion -> restart_on_oom
    6. Deployment change -> rollback_bad_deploy
    """

    # Anomaly kind -> default playbook fallback mapping
    ANOMALY_PLAYBOOK_MAP: ClassVar[dict[AnomalyKind, PlaybookName]] = {
        AnomalyKind.MEMORY_PRESSURE: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.CPU_SATURATION: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.QUEUE_BACKLOG: PlaybookName.CLEAR_QUEUE_BACKLOG,
        AnomalyKind.TRAFFIC_SURGE: PlaybookName.SCALE_OUT_PREEMPTIVE,
        AnomalyKind.TRAFFIC_DROP: PlaybookName.SCALE_IN_IDLE,
        AnomalyKind.ERROR_BURST: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.LATENCY_SPIKE: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.SETTLEMENT_DELAY: PlaybookName.CLEAR_QUEUE_BACKLOG,
        AnomalyKind.MULTIVARIATE: PlaybookName.RESTART_ON_OOM,
    }

    @classmethod
    def select_playbook(
        cls,
        anomaly: AnomalyEvent,
        incident: Incident | None = None,
        snapshot: dict[str, float] | None = None,
        previous_actions: list[dict[str, Any]] | None = None,
    ) -> tuple[PlaybookName, dict[str, Any]]:
        """Select appropriate playbook based on root-cause analysis, telemetry, and action history.

        Args:
            anomaly: The anomaly event
            incident: Optional incident with root cause hypotheses
            snapshot: Current telemetry snapshot (tps, capacity, queue_depth, cpu, mem)
            previous_actions: Prior remediation attempts for this incident/service

        Returns:
            (playbook_name, parameters): Playbook to execute and its parameters
        """
        parameters: dict[str, Any] = {
            "anomaly_kind": getattr(anomaly, "kind", None)
            or getattr(anomaly, "predicted_kind", None)
            or AnomalyKind.MULTIVARIATE,
            "anomaly_score": getattr(anomaly, "score", None)
            or getattr(anomaly, "anomaly_score", 0.0),
        }

        # -------------------------------------------------------------
        # 1. Inspect Previous Actions: Prevent Repeated Failed Restarts
        # -------------------------------------------------------------
        prior_failed_restart = False
        prior_scale_out_attempted = False
        if previous_actions:
            for act in previous_actions:
                act_playbook = act.get("playbook")
                act_status = act.get("status")
                if (
                    act_playbook
                    in (PlaybookName.CLEAR_QUEUE_BACKLOG, PlaybookName.RESTART_ON_OOM)
                    and act_status == "failed"
                ):
                    prior_failed_restart = True
                if act_playbook == PlaybookName.SCALE_OUT_PREEMPTIVE:
                    prior_scale_out_attempted = True

        # -------------------------------------------------------------
        # 2. Priority: Deployment Changes -> Rollback
        # -------------------------------------------------------------
        if incident and incident.hypotheses:
            primary = incident.hypotheses[0]
            if primary.category == RootCauseCategory.CHANGE:
                log.info(
                    "playbook.selected_from_incident",
                    playbook=PlaybookName.ROLLBACK_BAD_DEPLOY,
                    reason="Change hypothesis detected",
                    confidence=primary.confidence,
                )
                parameters["root_cause"] = "bad_deployment"
                parameters["reason"] = f"Preceding deployment detected: {primary.summary}"
                return PlaybookName.ROLLBACK_BAD_DEPLOY, parameters

            # -------------------------------------------------------------
            # 3. Priority: Downstream Dependency Failure
            # -------------------------------------------------------------
            if primary.category == RootCauseCategory.SERVICE:
                # If the root cause is a different downstream service, do not blindly restart upstream
                if primary.target and primary.target != anomaly.service:
                    log.info(
                        "playbook.downstream_dependency_detected",
                        service=anomaly.service,
                        downstream_root=primary.target,
                    )
                    parameters["root_cause"] = "downstream_dependency_failure"
                    parameters["downstream_target"] = primary.target
                    parameters["reason"] = (
                        f"Downstream service '{primary.target}' failure cascading to {anomaly.service}"
                    )
                    return PlaybookName.CIRCUIT_BREAK_CASCADE, parameters

        # -------------------------------------------------------------
        # 4. Telemetry-based Root Cause Diagnosis (Live Metrics)
        # -------------------------------------------------------------
        snap = snapshot or {}
        tps = snap.get("tps", 0.0)
        capacity = snap.get("capacity_tps", 0.0)
        queue_depth = snap.get("queue_depth", 0.0)
        cpu = snap.get("cpu_utilisation_ratio", 0.0)
        mem = snap.get("memory_utilisation_ratio", 0.0)
        error_ratio = snap.get("error_ratio", 0.0)

        # Fallback to nominal service capacity if not explicitly provided in snapshot
        if capacity <= 0.0:
            capacity = DEFAULT_SERVICE_CAPACITY_TPS.get(anomaly.service, 30.0)

        load_factor = (tps / capacity) if capacity > 0 else (cpu if cpu > 0 else 0.0)
        kind = parameters["anomaly_kind"]

        # Case A: Traffic Overload
        # When incoming traffic nears or exceeds replica capacity (load_factor >= 0.70)
        # or when traffic surge is identified and memory is not at saturation
        # or when queue depth is high and CPU is elevated under load without memory exhaustion
        is_traffic_overload = (
            kind == AnomalyKind.TRAFFIC_SURGE
            or (capacity > 0 and tps >= 0.70 * capacity)
            or (load_factor >= 0.70 and (queue_depth > 0 or cpu > 0.60))
            or (queue_depth > 10.0 and cpu > 0.60 and mem < 0.85)
        ) and mem < 0.90

        if is_traffic_overload:
            parameters["root_cause"] = "traffic_overload"
            parameters["load_factor"] = round(load_factor, 2)
            parameters["target_replicas"] = "+1"
            parameters["reason"] = (
                f"Traffic overload: TPS ({tps:.1f}) exceeds capacity threshold ({capacity:.1f}) "
                f"with load factor {load_factor:.2f}"
            )
            log.info(
                "playbook.selected_traffic_overload",
                service=anomaly.service,
                parameters=parameters,
            )
            return PlaybookName.SCALE_OUT_PREEMPTIVE, parameters

        # Case B: Persistent Queue Backlog with prior failed restart
        if (
            kind in (AnomalyKind.QUEUE_BACKLOG, AnomalyKind.SETTLEMENT_DELAY)
            or queue_depth > 5.0
        ):
            if prior_failed_restart or (cpu > 0.60 and mem < 0.85):
                # Under active processing load or when restart previously failed, scale out to drain backlog
                if not prior_scale_out_attempted:
                    # Escalate to scaling out an additional worker replica to drain the persistent backlog
                    parameters["root_cause"] = "persistent_queue_backlog_escalated"
                    parameters["target_replicas"] = "+1"
                    parameters["reason"] = (
                        f"Queue backlog ({queue_depth:.1f} items, CPU {cpu*100:.0f}%): "
                        f"scaling out worker replica to drain consumer queue."
                    )
                    log.info(
                        "playbook.escalated_to_scale_out",
                        service=anomaly.service,
                        parameters=parameters,
                    )
                    return PlaybookName.SCALE_OUT_PREEMPTIVE, parameters
                else:
                    # Both restart and scale-out failed or already attempted: require manual intervention
                    parameters["root_cause"] = "persistent_backlog_unresolvable"
                    parameters["requires_manual_intervention"] = True
                    parameters["reason"] = (
                        f"Persistent queue backlog ({queue_depth:.1f}): prior restart and scale-out attempts failed. "
                        f"Stopping automated remediation. Manual intervention required."
                    )
                    return PlaybookName.CLEAR_QUEUE_BACKLOG, parameters
            else:
                parameters["root_cause"] = "queue_backlog"
                parameters["reason"] = (
                    f"Queue backlog detected ({queue_depth:.1f} items). Clearing in-memory backlog."
                )
                return PlaybookName.CLEAR_QUEUE_BACKLOG, parameters

        # Case C: CPU or Memory Exhaustion
        if (
            mem > 0.85
            or cpu > 0.85
            or kind in (AnomalyKind.MEMORY_PRESSURE, AnomalyKind.CPU_SATURATION)
        ):
            parameters["root_cause"] = "resource_exhaustion"
            parameters["reason"] = f"Resource exhaustion: CPU={cpu:.1%}, Memory={mem:.1%}"
            return PlaybookName.RESTART_ON_OOM, parameters

        # Case D: Process Crash / High Error Rate
        if error_ratio > 0.5 or (tps <= 0.5 and error_ratio > 0.2):
            parameters["root_cause"] = "process_failure"
            parameters["reason"] = f"Process failure or high error burst ({error_ratio:.1%})"
            return PlaybookName.RESTART_ON_OOM, parameters

        # Case E: Traffic Drop (Idle)
        if kind == AnomalyKind.TRAFFIC_DROP:
            parameters["root_cause"] = "traffic_drop"
            parameters["target_replicas"] = "-1"
            parameters["reason"] = "Sustained traffic drop: scaling in idle surplus capacity"
            return PlaybookName.SCALE_IN_IDLE, parameters

        # Default Fallback based on anomaly kind
        playbook = cls.ANOMALY_PLAYBOOK_MAP.get(kind, PlaybookName.RESTART_ON_OOM)
        parameters["root_cause"] = "general_anomaly"
        parameters["reason"] = (
            f"Anomaly {kind} detected (score: {parameters['anomaly_score']:.2f})"
        )
        return playbook, parameters


class PlaybookExecutor:
    """Executes playbooks using the provided executor interface."""

    def __init__(self, executor: ExecutorInterface):
        self.executor = executor

    async def execute(
        self,
        playbook: PlaybookName,
        target_service: str,
        parameters: dict[str, Any],
    ) -> tuple[bool, str]:
        """Execute a playbook.

        Args:
            playbook: Which playbook to run
            target_service: Service to heal
            parameters: Playbook-specific parameters

        Returns:
            (success, message): True if succeeded, False if failed, plus message
        """
        if playbook == PlaybookName.RESTART_ON_OOM:
            success, msg = await self._restart_on_oom(target_service)
            return ScaleResult(success, msg, {"execution_mode": "real"})

        elif playbook == PlaybookName.CIRCUIT_BREAK_CASCADE:
            success, msg = await self._circuit_break_cascade(target_service)
            return ScaleResult(success, msg, {"execution_mode": "real"})

        elif playbook == PlaybookName.ROLLBACK_BAD_DEPLOY:
            success, msg = await self._rollback_bad_deploy(target_service)
            return ScaleResult(success, msg, {"execution_mode": "real"})

        elif playbook == PlaybookName.SCALE_OUT_PREEMPTIVE:
            return await self._scale_out_preemptive(target_service, parameters)

        elif playbook == PlaybookName.SCALE_IN_IDLE:
            return await self._scale_in_idle(target_service, parameters)

        elif playbook == PlaybookName.CLEAR_QUEUE_BACKLOG:
            success, msg = await self._clear_queue_backlog(target_service)
            return ScaleResult(success, msg, {"execution_mode": "real"})

        else:
            log.error("playbook.unknown", playbook=playbook)
            return ScaleResult(
                False, f"Unknown playbook: {playbook}", {"execution_mode": "simulated"}
            )

    async def _restart_on_oom(self, service: str) -> tuple[bool, str]:
        """Restart container(s) to clear memory pressure or general faults.

        Applies to: MEMORY_PRESSURE, CPU_SATURATION, ERROR_BURST, LATENCY_SPIKE, MULTIVARIATE
        """
        log.info("playbook.restart_on_oom", service=service)
        return await self.executor.restart_container(service)

    async def _circuit_break_cascade(self, service: str) -> tuple[bool, str]:
        """Remediate the root dependency in a cascade to break the failure chain.

        Applies to: SERVICE root cause with cascade pattern. Note: network-level
        traffic rejection/proxy circuit breaking is not configured on this container path;
        restarting the root dependency clears the failure chain.
        """
        log.info("playbook.circuit_break_cascade", service=service)
        success, msg = await self.executor.restart_container(service)
        return (
            success,
            f"{msg} (Remediated root dependency; network circuit breaker proxy not present)",
        )

    async def _rollback_bad_deploy(self, service: str) -> tuple[bool, str]:
        """Rollback to previous version when deployment preceded anomaly.

        Applies to: CHANGE root cause hypothesis
        """
        log.info("playbook.rollback_bad_deploy", service=service)
        return await self.executor.rollback_deployment(service)

    async def _scale_out_preemptive(
        self,
        service: str,
        parameters: dict[str, Any],
    ) -> tuple[bool, str]:
        """Scale out service to handle traffic surge.

        Applies to: TRAFFIC_SURGE
        """
        current = await self.executor.get_replica_count(service)
        target = current + 1  # Increment by 1

        log.info(
            "playbook.scale_out",
            service=service,
            current_replicas=current,
            target_replicas=target,
        )

        return await self.executor.scale_service(service, target)

    async def _scale_in_idle(
        self,
        service: str,
        parameters: dict[str, Any],
    ) -> tuple[bool, str]:
        """Scale in service when traffic drops.

        Applies to: TRAFFIC_DROP
        """
        current = await self.executor.get_replica_count(service)
        target = max(1, current - 1)  # Decrement by 1, minimum 1

        log.info(
            "playbook.scale_in",
            service=service,
            current_replicas=current,
            target_replicas=target,
        )

        return await self.executor.scale_service(service, target)

    async def _clear_queue_backlog(self, service: str) -> tuple[bool, str]:
        """Restart service to clear queue backlog or settlement delays.

        Applies to: QUEUE_BACKLOG, SETTLEMENT_DELAY
        """
        log.info("playbook.clear_queue_backlog", service=service)
        # Restarting clears in-memory queues; for persistent queues would need different approach
        return await self.executor.restart_container(service)
