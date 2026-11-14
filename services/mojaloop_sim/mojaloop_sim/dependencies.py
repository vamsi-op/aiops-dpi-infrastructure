"""Downstream dependencies and per-dependency circuit breakers.

Why this exists
---------------
Each simulated service used to be an island: a failing central-ledger did not slow down the
quoting-service that calls it, so a "cascade" only existed if a chaos experiment injected
faults into several services by hand. Real DPI incidents cascade on their own - a ledger
under pressure makes every caller wait, and their callers wait on them.

So each simulator now polls its downstream dependencies (`MOJALOOP_DEPENDENCIES`) over HTTP
and inherits their degradation:

* **closed breaker** - the caller waits on the dependency. It inherits its share of the
  dependency's *excess* latency (above that service's own load-adjusted normal) and of its
  excess error ratio (timeouts and failed calls propagate). That is the cascade.
* **open breaker** - the caller stops calling the dependency and fails fast. The work that
  needed it is deferred (Mojaloop's APIs are asynchronous; FSPs retry), so the caller
  inherits no latency and no errors - only a small deferred-work backlog. The cascade stops
  at this hop instead of spreading further upstream.
* **half-open** - after the open period one probe is let through: if the dependency looks
  healthy the breaker closes, otherwise it opens again.

Breakers are opened by the AIOps healing engine's `circuit_break_cascade` playbook
(`POST /sim/circuits/{dependency}`), which is the self-healing behaviour the project brief
asks for, and close themselves once the dependency has recovered.

A dependency that has never answered is ignored rather than treated as down. Running one
simulator on its own (tests, a laptop without the stack) must not make it believe every
dependency has failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import httpx

from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

from .profiles import get_profile
from .traffic import FaultEffects

log = get_logger(__name__)

POLL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 2.0
DEFAULT_OPEN_SECONDS = 120
# A caller waiting on a dependency that does not answer at all eventually times out.
UNREACHABLE_LATENCY_SECONDS = 2.0
# Consecutive unanswered polls before a dependency counts as down. A routine restart (a
# remediation, a rolling deploy) leaves it unreachable for a few seconds; treating that as an
# outage pushed 50% errors into every caller, and the 5-minute rate window then kept the
# callers flagged for five minutes after the restart had already succeeded.
UNREACHABLE_AFTER_POLLS = 3
# Excess below these is measurement noise, not degradation - the dependency's /sim state is
# rounded, and inheriting rounding error would make healthy callers drift off their baseline.
LATENCY_DEADBAND_RATIO = 0.05
ERROR_DEADBAND = 0.001
# Deferred work an open breaker adds to the caller's queue: visible, but well inside the
# queue-depth SLO, because deferring is the point of failing fast.
DEFERRED_QUEUE_PER_OPEN_BREAKER = 2.0


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class DependencyHealth:
    """What the last poll of one dependency found."""

    reachable: bool = False
    seen_healthy: bool = False  # has it ever answered? Never-seen dependencies are ignored.
    missed_polls: int = 0
    excess_latency_seconds: float = 0.0
    excess_error_ratio: float = 0.0
    observed_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return (
            self.reachable and self.excess_latency_seconds == 0 and self.excess_error_ratio == 0
        )


@dataclass
class CircuitBreaker:
    dependency: str
    state: BreakerState = BreakerState.CLOSED
    open_until: datetime | None = None
    open_seconds: int = DEFAULT_OPEN_SECONDS
    opened_by: str = ""
    trips: int = 0
    changed_at: datetime = field(default_factory=utc_now)

    def open(self, *, seconds: int, by: str, now: datetime | None = None) -> None:
        now = now or utc_now()
        self.state = BreakerState.OPEN
        self.open_seconds = max(1, int(seconds))
        self.open_until = now + timedelta(seconds=self.open_seconds)
        self.opened_by = by
        self.trips += 1
        self.changed_at = now

    def close(self, *, now: datetime | None = None) -> None:
        self.state = BreakerState.CLOSED
        self.open_until = None
        self.changed_at = now or utc_now()

    def advance(self, *, now: datetime, dependency_healthy: bool | None) -> None:
        """Move OPEN -> HALF_OPEN when the period ends, then let the probe decide."""
        if self.state is BreakerState.OPEN and self.open_until and now >= self.open_until:
            self.state = BreakerState.HALF_OPEN
            self.changed_at = now
        if self.state is BreakerState.HALF_OPEN and dependency_healthy is not None:
            if dependency_healthy:
                self.close(now=now)
            else:
                self.open(seconds=self.open_seconds, by="half-open probe", now=now)
                self.trips -= 1  # a re-open after a failed probe is not a new trip

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency": self.dependency,
            "state": str(self.state),
            "open_until": self.open_until.isoformat() if self.open_until else None,
            "opened_by": self.opened_by,
            "trips": self.trips,
            "changed_at": self.changed_at.isoformat(),
        }


class DependencyMonitor:
    """Polls one service's downstream dependencies and holds its circuit breakers."""

    def __init__(
        self,
        service: str,
        dependencies: tuple[str, ...],
        *,
        url_template: str | None = None,
        enabled: bool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.service = service
        self.dependencies = tuple(str(d) for d in dependencies)
        self.url_template: str = (
            url_template or os.getenv("SIM_DEPENDENCY_URL") or "http://{service}:8000"
        )
        if enabled is None:
            enabled = os.getenv("SIM_DEPENDENCIES", "true").lower() not in ("0", "false", "no")
        self.enabled = enabled and bool(self.dependencies)
        self.health: dict[str, DependencyHealth] = {
            d: DependencyHealth() for d in self.dependencies
        }
        self.breakers: dict[str, CircuitBreaker] = {
            d: CircuitBreaker(d) for d in self.dependencies
        }
        self._client = client
        self._task: asyncio.Task[None] | None = None

    # -- observation ------------------------------------------------------
    def observe(self, dependency: str, payload: dict[str, Any] | None, now: datetime) -> None:
        """Record one poll result. `payload` is the dependency's `GET /dependency/probe`, or None if it
        did not answer."""
        health = self.health[dependency]
        health.observed_at = now
        state = (payload or {}).get("state")
        if not state:
            health.missed_polls += 1
            if health.missed_polls < UNREACHABLE_AFTER_POLLS:
                return  # a blip (e.g. a restart in progress): keep the last known state
            health.reachable = False
            if health.seen_healthy:
                # It answered before and does not now: callers time out on it.
                health.excess_latency_seconds = UNREACHABLE_LATENCY_SECONDS
                health.excess_error_ratio = 1.0
            return

        profile = get_profile(dependency)
        load = float(state.get("load_factor", 0.0))
        normal_p50 = profile.base_latency_seconds * (1.0 + 0.35 * load)
        excess_latency = max(0.0, float(state.get("latency_p50_seconds", 0.0)) - normal_p50)
        if excess_latency < normal_p50 * LATENCY_DEADBAND_RATIO:
            excess_latency = 0.0
        excess_error = max(0.0, float(state.get("error_ratio", 0.0)) - profile.base_error_ratio)
        if excess_error < ERROR_DEADBAND:
            excess_error = 0.0

        health.reachable = True
        health.seen_healthy = True
        health.missed_polls = 0
        health.excess_latency_seconds = excess_latency
        health.excess_error_ratio = min(1.0, excess_error)

    def advance_breakers(self, now: datetime) -> None:
        for dependency, breaker in self.breakers.items():
            health = self.health[dependency]
            verdict = health.healthy if health.observed_at is not None else None
            breaker.advance(now=now, dependency_healthy=verdict)

    # -- effect on this service -------------------------------------------
    def effects(self) -> FaultEffects:
        """What this service inherits from its dependencies right now."""
        out = FaultEffects()
        if not self.dependencies:
            return out
        share = 1.0 / len(self.dependencies)
        for dependency in self.dependencies:
            health = self.health[dependency]
            if not health.seen_healthy:
                continue
            if self.breakers[dependency].state is BreakerState.CLOSED:
                out.extra_latency_seconds += share * health.excess_latency_seconds
                out.extra_error_ratio += share * health.excess_error_ratio
            elif not health.healthy:
                # Open or probing: fail fast, defer the work.
                out.extra_queue_depth += DEFERRED_QUEUE_PER_OPEN_BREAKER
        out.extra_error_ratio = min(1.0, out.extra_error_ratio)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dependencies": {
                d: {
                    "reachable": h.reachable,
                    "seen": h.seen_healthy,
                    "excess_latency_seconds": round(h.excess_latency_seconds, 4),
                    "excess_error_ratio": round(h.excess_error_ratio, 5),
                    "breaker": self.breakers[d].to_dict(),
                }
                for d, h in self.health.items()
            },
        }

    # -- polling loop -----------------------------------------------------
    async def poll_once(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS)
        try:
            for dependency in self.dependencies:
                url = self.url_template.format(service=dependency) + "/dependency/probe"
                try:
                    response = await client.get(url)
                    # A 503 is a failed call, but its body still carries the dependency's
                    # state; only no answer at all means unreachable.
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    payload = None
                self.observe(dependency, payload, utc_now())
            self.advance_breakers(utc_now())
        finally:
            if self._client is None:
                await client.aclose()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("deps.poll_failed", monitored_service=self.service)
            await asyncio.sleep(POLL_SECONDS)

    def start(self) -> None:
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name=f"deps-{self.service}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def combine(a: FaultEffects, b: FaultEffects) -> FaultEffects:
    """Sum two effect sets (multipliers multiply)."""
    return FaultEffects(
        extra_latency_seconds=a.extra_latency_seconds + b.extra_latency_seconds,
        extra_error_ratio=min(1.0, a.extra_error_ratio + b.extra_error_ratio),
        extra_cpu_ratio=a.extra_cpu_ratio + b.extra_cpu_ratio,
        memory_growth_per_tick=a.memory_growth_per_tick + b.memory_growth_per_tick,
        extra_queue_depth=a.extra_queue_depth + b.extra_queue_depth,
        extra_settlement_lag_seconds=a.extra_settlement_lag_seconds
        + b.extra_settlement_lag_seconds,
        traffic_multiplier=a.traffic_multiplier * b.traffic_multiplier,
    )
