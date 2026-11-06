"""Docker Compose scaling adapter for the AIOps self-healing engine.

Handles real container scale-out and scale-in via the Docker Python SDK,
managing replica numbering, compose metadata labels, network aliases, and
port collision avoidance without requiring external CLI dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any

from docker import DockerClient
from docker.errors import DockerException

from aiops_common.log import get_logger

log = get_logger(__name__)


class DockerComposeScaler:
    """Scales Docker Compose services up and down via the Docker SDK.

    Replicas are created with standard Docker Compose labels (`com.docker.compose.service`,
    `com.docker.compose.project`, `com.docker.compose.container-number`) and attached to
    the shared network with the service name alias, enabling instant Docker DNS load balancing.
    Host port collisions are prevented by only binding host ports on the primary replica.
    """

    def __init__(self, client: DockerClient) -> None:
        self.client = client
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, service: str) -> asyncio.Lock:
        """Concurrency protection: one scaling action per service at any given time."""
        if service not in self._locks:
            self._locks[service] = asyncio.Lock()
        return self._locks[service]

    def get_replicas(self, service: str) -> list[Any]:
        """List all active running containers for a service, sorted by container number."""
        try:
            # Primary lookup: Docker Compose service label
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.service={service}"}
            )
            if not containers:
                # Fallback: substring in container name
                all_containers = self.client.containers.list()
                containers = [c for c in all_containers if service in c.name]

            # Sort by container number label if available, else by name
            def _sort_key(c: Any) -> tuple[int, str]:
                labels = getattr(c, "labels", {}) or {}
                num = labels.get("com.docker.compose.container-number", "1")
                try:
                    return (int(num), c.name)
                except (ValueError, TypeError):
                    return (999, c.name)

            return sorted(containers, key=_sort_key)
        except DockerException as exc:
            log.error("scaler.get_replicas_failed", service=service, error=str(exc))
            return []

    async def get_replica_count(self, service: str) -> int:
        """Return the current number of running replicas for a service."""
        replicas = self.get_replicas(service)
        return len(replicas)

    async def scale_out(
        self,
        service: str,
        target_replicas: int,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Scale out a service by adding real container replicas up to target_replicas.

        Args:
            service: Target Compose service name (e.g., 'central-ledger')
            target_replicas: Desired total replica count

        Returns:
            (success, message, metadata)
        """
        async with self._get_lock(service):
            current_replicas = self.get_replicas(service)
            current_count = len(current_replicas)

            if current_count >= target_replicas:
                return (
                    True,
                    f"Service '{service}' already has {current_count} replica(s) (>= target {target_replicas})",
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": current_count,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )

            if not current_replicas:
                return (
                    False,
                    f"Cannot scale out '{service}': no existing template container found",
                    {
                        "actual_replicas_before": 0,
                        "actual_replicas_after": 0,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )

            template = current_replicas[0]
            try:
                template_attrs = template.attrs
                config = template_attrs.get("Config", {})
                host_config = template_attrs.get("HostConfig", {})
                network_settings = template_attrs.get("NetworkSettings", {})

                image = config.get("Image") or template.image.id
                env = config.get("Env", [])
                template_labels = config.get("Labels", {}) or {}

                project = template_labels.get("com.docker.compose.project", "aiops-dpi")

                # Determine next available container number
                existing_numbers = set()
                for c in current_replicas:
                    labels = getattr(c, "labels", {}) or {}
                    num = labels.get("com.docker.compose.container-number")
                    if num and str(num).isdigit():
                        existing_numbers.add(int(num))

                next_num = 1
                while next_num in existing_numbers:
                    next_num += 1

                replica_name = f"{project}-{service}-{next_num}"
                new_labels = dict(template_labels)
                new_labels["com.docker.compose.service"] = service
                new_labels["com.docker.compose.project"] = project
                new_labels["com.docker.compose.container-number"] = str(next_num)
                new_labels["com.docker.compose.oneoff"] = "False"

                # Identify target network and aliases
                networks = network_settings.get("Networks", {})
                target_net = "aiops-net"
                if networks:
                    target_net = next(iter(networks.keys()))

                # Extract resource limits from template if set
                nano_cpus = host_config.get("NanoCPUs")
                mem_limit = host_config.get("Memory")

                # Prepare host config without duplicating fixed host port bindings
                # Docker Compose allows inter-container DNS load balancing via network aliases
                create_host_config_kwargs: dict[str, Any] = {
                    "restart_policy": {"Name": "unless-stopped"},
                }
                if nano_cpus:
                    create_host_config_kwargs["nano_cpus"] = nano_cpus
                if mem_limit:
                    create_host_config_kwargs["mem_limit"] = mem_limit

                # Create container
                log.info(
                    "scaler.creating_replica",
                    service=service,
                    name=replica_name,
                    target_replicas=target_replicas,
                    network=target_net,
                )

                created_container = self.client.containers.create(
                    image=image,
                    name=replica_name,
                    environment=env,
                    labels=new_labels,
                    network=target_net,
                    **create_host_config_kwargs,
                )

                # Ensure service alias is registered on the bridge network for DNS round-robin
                try:
                    network_obj = self.client.networks.get(target_net)
                    # Disconnect default and connect with explicit alias if needed
                    network_obj.disconnect(created_container)
                    network_obj.connect(created_container, aliases=[service])
                except Exception as net_exc:  # noqa: BLE001
                    log.warning("scaler.network_alias_notice", error=str(net_exc))

                # Start the replica
                created_container.start()

                # Health verification: poll container status for up to 15 seconds
                verified = False
                for _ in range(15):
                    await asyncio.sleep(1)
                    created_container.reload()
                    state = created_container.attrs.get("State", {})
                    status = state.get("Status", "").lower()
                    health = state.get("Health", {}).get("Status", "").lower()

                    if status == "running" and health != "unhealthy":
                        verified = True
                        break
                    elif status in {"exited", "dead"}:
                        break

                if not verified:
                    try:
                        created_container.stop(timeout=5)
                        created_container.remove(force=True)
                    except Exception as clean_exc:  # noqa: BLE001
                        log.debug(
                            "scaler.cleanup_unverified_replica_failed", error=str(clean_exc)
                        )
                    return (
                        False,
                        f"New replica '{replica_name}' failed to reach running/healthy state within 15s",
                        {
                            "actual_replicas_before": current_count,
                            "actual_replicas_after": current_count,
                            "simulated_replicas_after": target_replicas,
                            "execution_mode": "real",
                        },
                    )

                new_count = current_count + 1
                msg = f"Scaled out '{service}' from {current_count} to {new_count} replica(s) (created '{replica_name}')"
                log.info(
                    "scaler.scale_out_success",
                    service=service,
                    new_count=new_count,
                    replica=replica_name,
                )

                return (
                    True,
                    msg,
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": new_count,
                        "simulated_replicas_after": new_count,
                        "execution_mode": "real",
                        "created_container": replica_name,
                    },
                )

            except Exception as exc:
                log.error(
                    "scaler.scale_out_failed", service=service, error=str(exc), exc_info=True
                )
                return (
                    False,
                    f"Docker scaling error: {exc}",
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": current_count,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )

    async def scale_in(
        self,
        service: str,
        target_replicas: int,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Scale in a service by removing one surplus container replica down to target_replicas.

        Always preserves replica 1 (primary) and selects the highest-numbered surplus replica
        for graceful shutdown and removal.

        Args:
            service: Target Compose service name
            target_replicas: Desired total replica count (must be >= 1)

        Returns:
            (success, message, metadata)
        """
        async with self._get_lock(service):
            current_replicas = self.get_replicas(service)
            current_count = len(current_replicas)

            if current_count <= target_replicas:
                return (
                    True,
                    f"Service '{service}' already has {current_count} replica(s) (<= target {target_replicas})",
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": current_count,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )

            if current_count <= 1:
                return (
                    False,
                    f"Cannot scale in '{service}': minimum replica threshold (1) reached",
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": current_count,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )

            # Select surplus replica to remove: highest container number / newest non-primary
            victim = current_replicas[-1]
            victim_name = victim.name

            try:
                log.info("scaler.stopping_replica", service=service, name=victim_name)
                # Graceful stop with 10s timeout
                victim.stop(timeout=10)
                victim.remove(force=True)

                # Verify remaining replicas are healthy
                remaining = self.get_replicas(service)
                new_count = len(remaining)

                msg = f"Scaled in '{service}' from {current_count} to {new_count} replica(s) (removed '{victim_name}')"
                log.info(
                    "scaler.scale_in_success",
                    service=service,
                    new_count=new_count,
                    removed=victim_name,
                )

                return (
                    True,
                    msg,
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": new_count,
                        "simulated_replicas_after": new_count,
                        "execution_mode": "real",
                        "removed_container": victim_name,
                    },
                )

            except Exception as exc:  # noqa: BLE001
                log.error(
                    "scaler.scale_in_failed",
                    service=service,
                    container=victim_name,
                    error=str(exc),
                )
                return (
                    False,
                    f"Docker scale-in error on '{victim_name}': {exc}",
                    {
                        "actual_replicas_before": current_count,
                        "actual_replicas_after": current_count,
                        "simulated_replicas_after": target_replicas,
                        "execution_mode": "real",
                    },
                )
