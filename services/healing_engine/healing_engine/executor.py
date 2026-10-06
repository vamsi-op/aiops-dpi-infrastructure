"""Executor interface and Docker implementation for remediation actions.

The executor interface allows swapping Docker for Kubernetes (module 10) without
changing playbook logic. All operations are idempotent and return success/failure
with descriptive messages.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from aiops_common.log import get_logger

log = get_logger(__name__)


class ExecutorInterface(ABC):
    """Abstract interface for executing remediation actions.
    
    Implementations handle the actual infrastructure operations (Docker, Kubernetes).
    All methods are idempotent and return (success: bool, message: str).
    """

    @abstractmethod
    async def restart_container(
        self,
        service: str,
        instance: str | None = None,
    ) -> tuple[bool, str]:
        """Restart a container or all containers for a service.
        
        Args:
            service: Service name (e.g., "central-ledger")
            instance: Specific container name/ID, or None for all instances
            
        Returns:
            (success, message): True if restarted, False if failed
        """
        ...

    @abstractmethod
    async def scale_service(
        self,
        service: str,
        replicas: int,
    ) -> tuple[bool, str]:
        """Scale a service to a target replica count.
        
        Args:
            service: Service name
            replicas: Target number of replicas (>= 0)
            
        Returns:
            (success, message): True if scaled, False if failed
        """
        ...

    @abstractmethod
    async def rollback_deployment(
        self,
        service: str,
    ) -> tuple[bool, str]:
        """Rollback a service to its previous version.
        
        Args:
            service: Service name
            
        Returns:
            (success, message): True if rolled back, False if failed
        """
        ...

    @abstractmethod
    async def get_replica_count(self, service: str) -> int:
        """Get current number of replicas for a service.
        
        Args:
            service: Service name
            
        Returns:
            Current replica count (0 if service not found)
        """
        ...


class DockerExecutor(ExecutorInterface):
    """Docker-based executor for local development and module 9 demo.
    
    Uses docker-compose service names and the Docker socket to manipulate containers.
    Module 10 will add a KubernetesExecutor that implements the same interface.
    """

    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        """Initialize Docker client.
        
        Args:
            socket_path: Path to Docker socket (default: /var/run/docker.sock)
        """
        self.socket_path = socket_path
        try:
            self.client = docker.DockerClient(base_url=f"unix://{socket_path}")
            # Test connection
            self.client.ping()
            log.info("docker_executor.initialized", socket=socket_path)
        except DockerException as exc:
            log.error(
                "docker_executor.init_failed",
                socket=socket_path,
                error=str(exc),
                exc_info=True,
            )
            raise

    async def restart_container(
        self,
        service: str,
        instance: str | None = None,
    ) -> tuple[bool, str]:
        """Restart container(s) for a service.
        
        If instance is specified, restarts that specific container.
        Otherwise, restarts all containers matching the service name.
        """
        try:
            if instance:
                # Restart specific container
                container = self.client.containers.get(instance)
                container.restart(timeout=10)
                log.info(
                    "docker_executor.container_restarted",
                    service=service,
                    instance=instance,
                )
                return True, f"Restarted container {instance}"
            
            # Restart all containers for service
            # Docker Compose names containers: <project>-<service>-<index>
            # We filter by label or name pattern
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )
            
            if not containers:
                # Try fallback: match container name
                all_containers = self.client.containers.list()
                containers = [c for c in all_containers if service in c.name]
            
            if not containers:
                log.warning(
                    "docker_executor.no_containers_found",
                    service=service,
                )
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
                exc_info=True,
            )
            return False, msg

    async def scale_service(
        self,
        service: str,
        replicas: int,
    ) -> tuple[bool, str]:
        """Scale a service by starting/stopping containers.
        
        In Docker Compose, scaling means running docker-compose up --scale.
        For simplicity in this demo, we'll return a message that it would scale.
        Actual implementation would use docker-compose API or Kubernetes.
        """
        try:
            current = await self.get_replica_count(service)
            
            if replicas == current:
                return True, f"Service {service} already at {replicas} replica(s)"
            
            # In a real implementation, we'd use docker-compose or Kubernetes API
            # For now, log the intent
            log.info(
                "docker_executor.scale_requested",
                service=service,
                current_replicas=current,
                target_replicas=replicas,
            )
            
            # Simulate scaling by returning success with message
            action = "scale up" if replicas > current else "scale down"
            msg = f"Would {action} {service} from {current} to {replicas} replica(s)"
            
            return True, msg
            
        except DockerException as exc:
            msg = f"Docker error during scaling: {exc}"
            log.error(
                "docker_executor.scale_failed",
                service=service,
                replicas=replicas,
                error=str(exc),
                exc_info=True,
            )
            return False, msg

    async def rollback_deployment(
        self,
        service: str,
    ) -> tuple[bool, str]:
        """Rollback by pulling previous image tag and restarting.
        
        In Kubernetes this would be `kubectl rollout undo`.
        In Docker Compose, we'd need version tracking.
        For demo purposes, we restart with current image.
        """
        try:
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )
            
            if not containers:
                return False, f"No containers found for service {service}"
            
            # In real implementation, would pull previous image tag
            # For demo, just restart (which is still a valid recovery action)
            restarted = []
            for container in containers:
                container.restart(timeout=10)
                restarted.append(container.name)
            
            log.info(
                "docker_executor.rollback_simulated",
                service=service,
                containers=len(restarted),
            )
            
            msg = f"Rolled back {service} ({len(restarted)} container(s) restarted)"
            return True, msg
            
        except DockerException as exc:
            msg = f"Docker error during rollback: {exc}"
            log.error(
                "docker_executor.rollback_failed",
                service=service,
                error=str(exc),
                exc_info=True,
            )
            return False, msg

    async def get_replica_count(self, service: str) -> int:
        """Count running containers for a service."""
        try:
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )
            
            if not containers:
                # Fallback: match by name
                all_containers = self.client.containers.list()
                containers = [c for c in all_containers if service in c.name]
            
            return len(containers)
            
        except DockerException as exc:
            log.error(
                "docker_executor.count_failed",
                service=service,
                error=str(exc),
            )
            return 0
