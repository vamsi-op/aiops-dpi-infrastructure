"""How many pods serve this service, so each simulator takes its share of the traffic.

Each simulator process models the whole service. When the healing engine scales a service out,
the new pod would generate the service's full traffic on top of the existing pod's, and since
Prometheus sums rates across pods, a scale-out looked like a doubled traffic spike instead of
relief. Real pods behind a load balancer split the load; this module makes the simulator do the
same.

The pod count comes from DNS, which both platforms already provide:

* Kubernetes: a headless Service (`<service>-peers`) resolves to one address per ready pod.
* Docker Compose: the service name resolves to every container carrying the service alias,
  which the Docker executor adds to the replicas it starts.

`SIM_PEERS_HOST` names the host to resolve. Unset, the count stays at 1, which is exactly the
previous behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable

from aiops_common.log import get_logger

log = get_logger(__name__)

POLL_SECONDS = 15.0


class PeerCounter:
    def __init__(
        self,
        apply: Callable[[int], int],
        *,
        host: str | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._apply = apply
        self.host = host if host is not None else os.getenv("SIM_PEERS_HOST", "").strip()
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self.peers = 1

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    async def count_once(self) -> int:
        """Resolve the host and apply the number of distinct addresses (at least 1)."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(self.host, None, family=socket.AF_INET)
            peers = max(1, len({info[4][0] for info in infos}))
        except OSError as exc:
            # Keep the last known count: a DNS hiccup must not suddenly triple the load.
            log.debug("sim.peers_lookup_failed", host=self.host, error=str(exc))
            return self.peers
        if peers != self.peers:
            log.info("sim.peers_changed", host=self.host, previous=self.peers, peers=peers)
        self.peers = self._apply(peers)
        return self.peers

    async def _run(self) -> None:
        while True:
            await self.count_once()
            await asyncio.sleep(self._poll_seconds)

    def start(self) -> None:
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="sim-peers")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
