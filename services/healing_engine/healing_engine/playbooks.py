"""Playbook registry and implementations.

Maps anomaly kinds and incident root causes to specific remediation playbooks.
Each playbook knows how to:
1. Decide if it applies to a given anomaly/incident
2. Execute remediation via the executor interface
3. Return success/failure with descriptive message

Playbooks are idempotent and safe to retry.
"""

from __future__ import annotations

from typing import Any

from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, Incident

from .executor import ExecutorInterface

log = get_logger(__name__)


class PlaybookRegistry:
    """Maps anomaly kinds and root causes to playbook names.
    
    Selection priority:
    1. If incident has RootCauseCategory.CHANGE hypothesis -> rollback
    2. If incident has SERVICE hypothesis with cascade -> circuit breaker
    3. Else map anomaly kind to playbook
    """

    # Anomaly kind -> default playbook mapping
    ANOMALY_PLAYBOOK_MAP = {
        AnomalyKind.MEMORY_PRESSURE: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.CPU_SATURATION: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.QUEUE_BACKLOG: PlaybookName.CLEAR_QUEUE_BACKLOG,
        AnomalyKind.TRAFFIC_SURGE: PlaybookName.SCALE_OUT_PREEMPTIVE,
        AnomalyKind.TRAFFIC_DROP: PlaybookName.SCALE_IN_IDLE,
        AnomalyKind.ERROR_BURST: PlaybookName.RESTART_ON_OOM,  # Often memory/resource related
        AnomalyKind.LATENCY_SPIKE: PlaybookName.RESTART_ON_OOM,
        AnomalyKind.SETTLEMENT_DELAY: PlaybookName.CLEAR_QUEUE_BACKLOG,
        AnomalyKind.MULTIVARIATE: PlaybookName.RESTART_ON_OOM,  # Generic fallback
    }

    @classmethod
    def select_playbook(
        cls,
        anomaly: AnomalyEvent,
        incident: Incident | None = None,
    ) -> tuple[PlaybookName, dict[str, Any]]:
        """Select appropriate playbook based on anomaly and incident context.
        
        Args:
            anomaly: The anomaly event
            incident: Optional incident with root cause hypotheses
            
        Returns:
            (playbook_name, parameters): Playbook to execute and its parameters
        """
        # Priority 1: Incident-based selection
        if incident and incident.hypotheses:
            primary = incident.hypotheses[0]
            
            # Change -> rollback
            if primary.category == RootCauseCategory.CHANGE:
                log.info(
                    "playbook.selected_from_incident",
                    playbook=PlaybookName.ROLLBACK_BAD_DEPLOY,
                    reason="Change hypothesis detected",
                    confidence=primary.confidence,
                )
                return PlaybookName.ROLLBACK_BAD_DEPLOY, {}
            
            # Service cascade -> circuit breaker
            if primary.category == RootCauseCategory.SERVICE:
                if "cascade" in primary.summary.lower() or "dependency" in primary.summary.lower():
                    log.info(
                        "playbook.selected_from_incident",
                        playbook=PlaybookName.CIRCUIT_BREAK_CASCADE,
                        reason="Cascade detected",
                        confidence=primary.confidence,
                    )
                    return PlaybookName.CIRCUIT_BREAK_CASCADE, {}
        
        # Priority 2: Anomaly kind mapping
        playbook = cls.ANOMALY_PLAYBOOK_MAP.get(
            anomaly.predicted_kind,
            PlaybookName.RESTART_ON_OOM,  # Default fallback
        )
        
        # Add parameters based on anomaly
        parameters: dict[str, Any] = {
            "anomaly_kind": anomaly.predicted_kind,
            "anomaly_score": anomaly.anomaly_score,
        }
        
        # Scale out/in: add target replica count based on traffic
        if playbook == PlaybookName.SCALE_OUT_PREEMPTIVE:
            parameters["target_replicas"] = "+1"  # Increment by 1
        elif playbook == PlaybookName.SCALE_IN_IDLE:
            parameters["target_replicas"] = "-1"  # Decrement by 1, min 1
        
        log.info(
            "playbook.selected_from_anomaly",
            playbook=playbook,
            anomaly_kind=anomaly.predicted_kind,
            parameters=parameters,
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
            return await self._restart_on_oom(target_service)
        
        elif playbook == PlaybookName.CIRCUIT_BREAK_CASCADE:
            return await self._circuit_break_cascade(target_service)
        
        elif playbook == PlaybookName.ROLLBACK_BAD_DEPLOY:
            return await self._rollback_bad_deploy(target_service)
        
        elif playbook == PlaybookName.SCALE_OUT_PREEMPTIVE:
            return await self._scale_out_preemptive(target_service, parameters)
        
        elif playbook == PlaybookName.SCALE_IN_IDLE:
            return await self._scale_in_idle(target_service, parameters)
        
        elif playbook == PlaybookName.CLEAR_QUEUE_BACKLOG:
            return await self._clear_queue_backlog(target_service)
        
        else:
            log.error("playbook.unknown", playbook=playbook)
            return False, f"Unknown playbook: {playbook}"

    async def _restart_on_oom(self, service: str) -> tuple[bool, str]:
        """Restart container(s) to clear memory pressure or general faults.
        
        Applies to: MEMORY_PRESSURE, CPU_SATURATION, ERROR_BURST, LATENCY_SPIKE, MULTIVARIATE
        """
        log.info("playbook.restart_on_oom", service=service)
        return await self.executor.restart_container(service)

    async def _circuit_break_cascade(self, service: str) -> tuple[bool, str]:
        """Restart the root service in a cascade to break the failure chain.
        
        Applies to: SERVICE root cause with cascade pattern
        """
        log.info("playbook.circuit_break_cascade", service=service)
        # For cascades, restart the root service (passed as target_service)
        return await self.executor.restart_container(service)

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
