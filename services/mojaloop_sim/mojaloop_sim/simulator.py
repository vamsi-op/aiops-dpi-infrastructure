"""Runtime for one simulated Mojaloop service.

Owns the traffic model, the fault registry and the Prometheus collectors, and drives them
from a single background tick. Everything the HTTP layer needs is reachable from here, so
the routes stay thin.

Why a background ticker rather than metrics computed on scrape: Prometheus counters must
increase monotonically and independently of how often they are read. Computing traffic on
demand would make `rate()` depend on the scrape interval, and two scrapers would see
different histories. A fixed 1-second tick decouples the simulation from the observer.
"""

from __future__ import annotations

import asyncio
import contextlib
import random

from aiops_common.constants import MOJALOOP_DEPENDENCIES
from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

from .dependencies import DependencyMonitor, combine
from .faults import FaultKind, FaultRegistry
from .metrics import SimulatorMetrics
from .profiles import ServiceProfile, get_profile
from .traffic import ServiceState, TrafficModel

log = get_logger(__name__)

# One second gives Prometheus (scraping every 15 s) plenty of resolution while keeping CPU
# per container negligible.
TICK_SECONDS = 1.0

# Emit a summary log line this often. Every tick would be 5 lines/second across the
# deployment, which buries the interesting entries and wastes Loki storage.
LOG_EVERY_TICKS = 15


class Simulator:
    """Simulates one Mojaloop microservice: traffic, resources, faults and metrics."""

    def __init__(
        self,
        service: str,
        *,
        base_tps: float,
        seasonality: bool = True,
        seed: int | None = None,
    ) -> None:
        self.profile: ServiceProfile = get_profile(service)
        self.service = self.profile.name

        # Seeding per service name keeps runs reproducible while still giving each service
        # its own noise, so they do not jitter in lockstep.
        rng = random.Random(seed if seed is not None else hash(self.service) & 0xFFFF)
        # Shared so the HTTP layer can make the same random decisions (e.g. whether a
        # simulated request fails) without reaching into another object's internals.
        self.rng = rng

        self.model = TrafficModel(
            self.profile, base_tps=base_tps, seasonality=seasonality, rng=rng
        )
        self.faults = FaultRegistry(self.service, tick_seconds=TICK_SECONDS)
        # Downstream calls and their circuit breakers: how a degraded dependency cascades
        # into this service, and how an open breaker stops it (see dependencies.py).
        self.dependencies = DependencyMonitor(
            self.service, MOJALOOP_DEPENDENCIES.get(self.service, ())
        )
        self.metrics = SimulatorMetrics(self.profile, rng=rng)

        self.last_state: ServiceState | None = None
        self.started_at = utc_now()
        self._ticks = 0
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name=f"sim-{self.service}")
            self.dependencies.start()
            log.info(
                "sim.started",
                monitored_service=self.service,
                replicas=self.model.replicas,
                capacity_tps=round(self.model.capacity_tps, 1),
                description=self.profile.description,
            )

    async def stop(self) -> None:
        self._stopping.set()
        await self.dependencies.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("sim.stopped", monitored_service=self.service, ticks=self._ticks)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                # One bad tick must not kill the simulation - Prometheus would see the
                # target go stale and we would be debugging the wrong problem.
                log.exception("sim.tick_failed", monitored_service=self.service)
            await asyncio.sleep(TICK_SECONDS)

    # -- the tick ----------------------------------------------------------
    def tick(self) -> ServiceState:
        """Advance the simulation one step and update the exported metrics."""
        now = utc_now()
        active = self.faults.active(now)
        effects = combine(self.faults.effects(now), self.dependencies.effects())

        state = self.model.sample(now, effects)
        self.last_state = state
        self._ticks += 1

        self.metrics.record(
            state,
            tick_seconds=TICK_SECONDS,
            active_kinds={str(fault.kind) for fault in active},
        )

        self._maybe_log(state, active_kinds=[str(f.kind) for f in active])
        return state

    def _maybe_log(self, state: ServiceState, *, active_kinds: list[str]) -> None:
        """Emit structured logs for Loki: a periodic summary, plus warnings when degraded.

        Log level is driven by the simulated state, so the log stream is genuinely
        correlated with the metrics. Module 7 can use that correlation as RCA evidence
        rather than treating logs as decoration.
        """
        degraded = state.error_ratio > 0.05 or state.load_factor > 1.0
        if not degraded and self._ticks % LOG_EVERY_TICKS != 0:
            return

        fields = {
            "monitored_service": self.service,
            "tps": round(state.tps, 2),
            "load_factor": round(state.load_factor, 3),
            "latency_p99_seconds": round(state.latency_p99_seconds, 4),
            "error_ratio": round(state.error_ratio, 5),
            "cpu_ratio": round(state.cpu_ratio, 3),
            "memory_ratio": round(state.memory_ratio, 3),
            "queue_depth": round(state.queue_depth, 1),
            "replicas": state.replicas,
        }
        if active_kinds:
            fields["active_faults"] = ",".join(active_kinds)

        if state.error_ratio > 0.10:
            log.error("txn.failure_rate_high", **fields)
        elif degraded:
            log.warning("txn.degraded", **fields)
        else:
            log.info("txn.processed", **fields)

    # -- operations the platform performs on this service ------------------
    def scale(self, replicas: int) -> int:
        """Change the replica count, as a scaling playbook would.

        Capacity is derived from this, so the effect propagates to latency, CPU, errors and
        queue depth on the next tick. That is what lets module 6 prove a scale-out actually
        resolved an incident.
        """
        previous = self.model.replicas
        current = self.model.scale_to(replicas)
        log.info(
            "sim.scaled",
            monitored_service=self.service,
            from_replicas=previous,
            to_replicas=current,
            capacity_tps=round(self.model.capacity_tps, 1),
        )
        return current

    def snapshot(self) -> dict[str, object]:
        """Current simulated state, for debugging and for the dashboard's chaos page."""
        state = self.last_state
        return {
            "service": self.service,
            "description": self.profile.description,
            "started_at": self.started_at.isoformat(),
            "ticks": self._ticks,
            "replicas": self.model.replicas,
            "capacity_tps": round(self.model.capacity_tps, 2),
            "traffic_share": self.profile.traffic_share,
            "state": None
            if state is None
            else {
                "tps": round(state.tps, 2),
                "load_factor": round(state.load_factor, 3),
                "latency_p50_seconds": round(state.latency_p50_seconds, 4),
                "latency_p99_seconds": round(state.latency_p99_seconds, 4),
                "error_ratio": round(state.error_ratio, 5),
                "cpu_ratio": round(state.cpu_ratio, 3),
                "memory_ratio": round(state.memory_ratio, 3),
                "queue_depth": round(state.queue_depth, 1),
                "settlement_lag_seconds": round(state.settlement_lag_seconds, 2),
            },
            "active_faults": [fault.to_dict() for fault in self.faults.active()],
            "dependencies": self.dependencies.snapshot(),
            "injectable_faults": [str(kind) for kind in FaultKind],
        }
