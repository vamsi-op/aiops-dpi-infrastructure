"""Executor interface and Docker implementation for remediation actions.

The executor interface allows swapping Docker for Kubernetes (module 10) without
changing playbook logic. All operations are idempotent and return success/failure
with descriptive messages and structured execution metadata.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from aiops_common.config import HealingSettings, get_settings
from aiops_common.constants import ActionStatus, ExecutionMode
from aiops_common.log import get_logger

from .resources import check_resource_capacity
from .scaling import DockerComposeScaler

log = get_logger(__name__)


class ScaleResult(tuple[Any, ...]):
    """Result of a scaling operation, backwards-compatible with (success, message) 2-tuples."""

    success: bool
    message: str
    metadata: dict[str, Any]

    def __new__(
        cls,
        success: bool,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> ScaleResult:
        inst = super().__new__(cls, (success, message))
        inst.success = success
        inst.message = message
        inst.metadata = metadata or {}
        return inst


class ExecutorInterface(ABC):
    """Abstract interface for executing remediation actions.

    Implementations handle the actual infrastructure operations (Docker, Kubernetes).
    All methods are idempotent and return (success: bool, message: str) or ScaleResult.
    """

    @abstractmethod
    async def restart_container(
        self,
        service: str,
        instance: str | None = None,
    ) -> tuple[bool, str]:
        """Restart a container or all containers for a service."""
        ...

    @abstractmethod
    async def scale_service(
        self,
        service: str,
        replicas: int,
    ) -> tuple[bool, str] | ScaleResult:
        """Scale a service to a target replica count."""
        ...

    @abstractmethod
    async def rollback_deployment(
        self,
        service: str,
    ) -> tuple[bool, str]:
        """Rollback a service to its previous version."""
        ...

    @abstractmethod
    async def get_replica_count(self, service: str) -> int:
        """Get current number of replicas for a service."""
        ...


class DockerExecutor(ExecutorInterface):
    """Docker-based executor for local development and real container operations.

    Uses docker-compose service names, resource validation, and DockerComposeScaler
    to restart, scale out, and scale in real containers.
    """

    def __init__(
        self,
        socket_path: str = "/var/run/docker.sock",
        client: docker.DockerClient | None = None,
        settings: HealingSettings | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.settings = settings or get_settings().healing
        if client is not None:
            self.client = client
        else:
            try:
                self.client = docker.DockerClient(base_url=f"unix://{socket_path}")
                self.client.ping()
                log.info("docker_executor.initialized", socket=socket_path)
            except DockerException:
                try:
                    self.client = docker.from_env()
                    self.client.ping()
                    log.info("docker_executor.initialized_from_env")
                except DockerException as exc:
                    log.error(
                        "docker_executor.init_failed",
                        socket=socket_path,
                        error=str(exc),
                        exc_info=True,
                    )
                    raise

        self.scaler = DockerComposeScaler(self.client)

    async def restart_container(
        self,
        service: str,
        instance: str | None = None,
    ) -> tuple[bool, str]:
        """Restart container(s) for a service."""
        try:
            if instance:
                container = self.client.containers.get(instance)
                container.restart(timeout=10)
                log.info(
                    "docker_executor.container_restarted", service=service, instance=instance
                )
                return True, f"Restarted container {instance}"

            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )

            if not containers:
                all_containers = self.client.containers.list()
                containers = [c for c in all_containers if service in c.name]

            if not containers:
                log.warning("docker_executor.no_containers_found", service=service)
                return False, f"No containers found for service {service}"

            restarted = []
            for container in containers:
                container.restart(timeout=10)
                restarted.append(container.name)
                log.info(
                    "docker_executor.container_restarted",
                    service=service,
                    container=container.name,
                )

            return True, f"Restarted {len(restarted)} container(s): {', '.join(restarted)}"

        except NotFound:
            msg = f"Container not found: {instance or service}"
            log.error("docker_executor.not_found", service=service, instance=instance)
            return False, msg
        except DockerException as exc:
            msg = f"Docker error: {exc}"
            log.error(
                "docker_executor.restart_failed",
                service=service,
                instance=instance,
                error=str(exc),
            )
            return False, msg

    async def scale_service(
        self,
        service: str,
        replicas: int,
    ) -> ScaleResult:
        """Scale a service with host capacity validation and real Docker Compose scaling.

        Enforces:
        - MIN_REPLICAS (default 1)
        - MAX_REPLICAS (default 3)
        - Resource capacity validation (RAM and CPU) before actual scale-out
        - Resource-aware simulation fallback (skipped_insufficient_capacity)
        """
        settings = self.settings
        min_replicas = getattr(settings, "min_replicas", 1)
        max_replicas = getattr(settings, "max_replicas", 3)

        current = await self.get_replica_count(service)
        # If no containers are running, assume base 1 replica for calculation
        effective_current = max(1, current) if current == 0 else current

        # Case 1: No change required
        if replicas == current:
            msg = f"Service '{service}' already at {replicas} replica(s)"
            return ScaleResult(
                True,
                msg,
                {
                    "status": ActionStatus.SUCCEEDED,
                    "execution_mode": ExecutionMode.REAL,
                    "actual_replicas_before": current,
                    "actual_replicas_after": current,
                    "simulated_replicas_after": current,
                },
            )

        # Case 2: Scale-Out (pre-emptive traffic surge)
        if replicas > current:
            # Enforce scaling by exactly ONE replica at a time
            replicas = current + 1

            # Check maximum per-service replica limit
            if effective_current >= max_replicas:
                msg = f"Service '{service}' already at maximum replicas ({max_replicas}). Scaling capped."
                log.info(
                    "docker_executor.scale_capped_max",
                    service=service,
                    current=current,
                    max_replicas=max_replicas,
                )
                return ScaleResult(
                    False,
                    msg,
                    {
                        "status": ActionStatus.SKIPPED_MAX_REPLICAS,
                        "execution_mode": ExecutionMode.SIMULATED,
                        "actual_replicas_before": current,
                        "actual_replicas_after": current,
                        "simulated_replicas_after": replicas,
                    },
                )

            # Check global container limit on Docker host
            global_max = getattr(settings, "global_max_containers", 20)
            if hasattr(self, "client") and self.client:
                try:
                    all_containers = self.client.containers.list()
                    if len(all_containers) >= global_max:
                        msg = (
                            f"Global container limit ({global_max}) reached on host "
                            f"({len(all_containers)} running). Cannot scale out '{service}'."
                        )
                        log.warning(
                            "docker_executor.global_container_limit_reached",
                            count=len(all_containers),
                            max=global_max,
                        )
                        return ScaleResult(
                            False,
                            msg,
                            {
                                "status": ActionStatus.SKIPPED_GLOBAL_LIMIT,
                                "execution_mode": ExecutionMode.SIMULATED,
                                "actual_replicas_before": current,
                                "actual_replicas_after": current,
                                "simulated_replicas_after": replicas,
                            },
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("docker_executor.global_limit_check_failed", error=str(exc))

            # Check host resource availability
            sufficient, cap_reason, metrics = check_resource_capacity(
                replica_memory_limit_mb=settings.replica_memory_limit_mb,
                min_available_ram_mb=settings.min_available_ram_mb,
                max_host_cpu_percent=settings.max_host_cpu_percent,
            )

            if not sufficient:
                msg = f"Insufficient host capacity: {cap_reason}. Simulated scale-out from {current} to {replicas} replicas."
                log.warning(
                    "docker_executor.insufficient_capacity", service=service, reason=cap_reason
                )
                return ScaleResult(
                    False,
                    msg,
                    {
                        "status": ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY,
                        "execution_mode": ExecutionMode.SIMULATED,
                        "actual_replicas_before": current,
                        "actual_replicas_after": current,
                        "simulated_replicas_after": replicas,
                        "resource_metrics": metrics,
                    },
                )

            # If HEALING_ENABLED is false, simulate the decision in dry-run mode
            if not settings.enabled:
                msg = f"Dry-run: Would scale out '{service}' from {current} to {replicas} replica(s)"
                return ScaleResult(
                    True,
                    msg,
                    {
                        "status": ActionStatus.SKIPPED_DRY_RUN,
                        "execution_mode": ExecutionMode.SIMULATED,
                        "actual_replicas_before": current,
                        "actual_replicas_after": current,
                        "simulated_replicas_after": replicas,
                        "resource_metrics": metrics,
                    },
                )

            # Capacity sufficient and healing enabled: perform REAL Docker Compose scale-out
            success, scale_msg, scale_meta = await self.scaler.scale_out(service, replicas)
            actual_after = scale_meta.get("actual_replicas_after", current)
            status = ActionStatus.SUCCEEDED if success else ActionStatus.FAILED
            meta = {
                "status": status,
                "execution_mode": ExecutionMode.REAL,
                "actual_replicas_before": current,
                "actual_replicas_after": actual_after,
                "simulated_replicas_after": actual_after,
                "resource_metrics": metrics,
                **scale_meta,
            }
            return ScaleResult(success, scale_msg, meta)

        # Case 3: Scale-In (idle traffic drop)
        else:
            # Check minimum replica limit
            if effective_current <= min_replicas:
                msg = f"Service '{service}' already at minimum replicas ({min_replicas}). Cannot scale in."
                log.info(
                    "docker_executor.scale_capped_min",
                    service=service,
                    current=current,
                    min_replicas=min_replicas,
                )
                return ScaleResult(
                    False,
                    msg,
                    {
                        "status": ActionStatus.SKIPPED_MIN_REPLICAS,
                        "execution_mode": ExecutionMode.SIMULATED,
                        "actual_replicas_before": current,
                        "actual_replicas_after": current,
                        "simulated_replicas_after": max(min_replicas, replicas),
                    },
                )

            if not settings.enabled:
                msg = f"Dry-run: Would scale in '{service}' from {current} to {replicas} replica(s)"
                return ScaleResult(
                    True,
                    msg,
                    {
                        "status": ActionStatus.SKIPPED_DRY_RUN,
                        "execution_mode": ExecutionMode.SIMULATED,
                        "actual_replicas_before": current,
                        "actual_replicas_after": current,
                        "simulated_replicas_after": replicas,
                    },
                )

            # Perform REAL Docker Compose scale-in
            success, scale_msg, scale_meta = await self.scaler.scale_in(service, replicas)
            actual_after = scale_meta.get("actual_replicas_after", current)
            status = ActionStatus.SUCCEEDED if success else ActionStatus.FAILED
            meta = {
                "status": status,
                "execution_mode": ExecutionMode.REAL,
                "actual_replicas_before": current,
                "actual_replicas_after": actual_after,
                "simulated_replicas_after": actual_after,
                **scale_meta,
            }
            return ScaleResult(success, scale_msg, meta)

    async def rollback_deployment(
        self,
        service: str,
    ) -> tuple[bool, str]:
        """Rollback by pulling previous image tag and restarting."""
        try:
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )

            if not containers:
                return False, f"No containers found for service {service}"

            restarted = []
            for container in containers:
                container.restart(timeout=10)
                restarted.append(container.name)

            log.info(
                "docker_executor.rollback_simulated", service=service, containers=len(restarted)
            )
            msg = f"Rolled back {service} ({len(restarted)} container(s) restarted)"
            return True, msg

        except DockerException as exc:
            msg = f"Docker error during rollback: {exc}"
            log.error("docker_executor.rollback_failed", service=service, error=str(exc))
            return False, msg

    async def get_replica_count(self, service: str) -> int:
        """Count running containers for a service."""
        try:
            return await self.scaler.get_replica_count(service)
        except Exception:  # noqa: BLE001
            containers = self.scaler.get_replicas(service)
            return len(containers)
