"""Fault injection with guaranteed expiry.

The fault names are the values of `aiops_common.constants.ChaosExperiment`, because this
is the contract module 9's chaos agent will drive. Two extra kinds (`queue_backlog`,
`settlement_delay`) exist for failure modes the anomaly detector needs to recognise but
which are not standalone chaos experiments.

**Every fault expires.** Expiry is evaluated lazily whenever the active set is read, so
there is no background task that could die and leave a fault stuck on. A fault that
outlives its window would leave the whole platform in a broken state and waste everyone's
time during a demo, so this is enforced rather than encouraged - including a hard cap on
requested duration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from aiops_common.constants import ChaosExperiment
from aiops_common.log import get_logger
from aiops_common.schemas import new_id, utc_now

from .traffic import FaultEffects

log = get_logger(__name__)

# No fault may run longer than this, whatever the caller asks for. A forgotten injection
# should degrade one service for minutes, not for the rest of the afternoon.
MAX_DURATION_SECONDS = 1_800
DEFAULT_DURATION_SECONDS = 120


class FaultKind(StrEnum):
    """Injectable failure modes.

    The first seven mirror `ChaosExperiment` one-for-one. `KILL_SERVICE` is handled by the
    route layer (it exits the process so the container restarts) rather than as an effect.
    """

    NETWORK_LATENCY = ChaosExperiment.NETWORK_LATENCY
    ERROR_INJECTION = ChaosExperiment.ERROR_INJECTION
    MEMORY_HOG = ChaosExperiment.MEMORY_HOG
    CPU_HOG = ChaosExperiment.CPU_HOG
    TRAFFIC_SPIKE = ChaosExperiment.TRAFFIC_SPIKE
    BAD_DEPLOY = ChaosExperiment.BAD_DEPLOY
    KILL_SERVICE = ChaosExperiment.KILL_SERVICE
    # Not chaos experiments in their own right, but distinct anomaly signatures.
    QUEUE_BACKLOG = "queue_backlog"
    SETTLEMENT_DELAY = "settlement_delay"


# Sensible magnitude per kind, so a caller can just name the fault. Units differ by kind
# and are documented on each effect below.
DEFAULT_MAGNITUDE: dict[FaultKind, float] = {
    FaultKind.NETWORK_LATENCY: 0.4,  # extra seconds on every request
    FaultKind.ERROR_INJECTION: 0.15,  # absolute error ratio added
    FaultKind.MEMORY_HOG: 0.95,  # memory ratio to climb toward
    FaultKind.CPU_HOG: 0.55,  # extra CPU ratio
    FaultKind.TRAFFIC_SPIKE: 3.0,  # traffic multiplier
    FaultKind.BAD_DEPLOY: 1.0,  # scale factor on a composite effect
    FaultKind.KILL_SERVICE: 0.0,  # no effect; handled by exiting
    FaultKind.QUEUE_BACKLOG: 400.0,  # extra queued items
    FaultKind.SETTLEMENT_DELAY: 45.0,  # extra seconds of settlement lag
}


@dataclass
class Fault:
    """One active fault injection."""

    fault_id: str
    kind: FaultKind
    magnitude: float
    started_at: datetime
    expires_at: datetime
    injected_by: str = "unknown"
    notes: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.expires_at - self.started_at).total_seconds()

    def remaining_seconds(self, now: datetime | None = None) -> float:
        return max(0.0, (self.expires_at - (now or utc_now())).total_seconds())

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def elapsed_fraction(self, now: datetime | None = None) -> float:
        """How far through its lifetime this fault is, 0.0 to 1.0."""
        total = self.duration_seconds
        if total <= 0:
            return 1.0
        elapsed = ((now or utc_now()) - self.started_at).total_seconds()
        return max(0.0, min(1.0, elapsed / total))

    def to_dict(self) -> dict[str, Any]:
        return {
            "fault_id": self.fault_id,
            "kind": str(self.kind),
            "magnitude": self.magnitude,
            "started_at": self.started_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "remaining_seconds": round(self.remaining_seconds(), 1),
            "injected_by": self.injected_by,
            "notes": self.notes,
            "parameters": self.parameters,
        }


class FaultRegistry:
    """Holds the active faults for one simulated service and reduces them to effects."""

    def __init__(self, service: str, *, tick_seconds: float = 1.0) -> None:
        self._service = service
        self._tick_seconds = tick_seconds
        self._faults: dict[str, Fault] = {}

    # -- lifecycle --------------------------------------------------------
    def inject(
        self,
        kind: FaultKind,
        *,
        magnitude: float | None = None,
        duration_seconds: int = DEFAULT_DURATION_SECONDS,
        injected_by: str = "unknown",
        notes: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> Fault:
        """Register a fault. Duration is clamped; a second fault of the same kind replaces
        the first, so repeated injection cannot stack into something unrecoverable."""
        duration = max(1, min(int(duration_seconds), MAX_DURATION_SECONDS))
        now = utc_now()

        # Replace rather than accumulate. Two overlapping memory leaks are not a scenario
        # anyone wants to debug, and stacking would let a caller exceed the safety caps.
        for existing_id, existing in list(self._faults.items()):
            if existing.kind is kind:
                del self._faults[existing_id]

        fault = Fault(
            fault_id=new_id("fault"),
            kind=kind,
            magnitude=DEFAULT_MAGNITUDE[kind] if magnitude is None else magnitude,
            started_at=now,
            expires_at=now + timedelta(seconds=duration),
            injected_by=injected_by,
            notes=notes,
            parameters=parameters or {},
        )
        self._faults[fault.fault_id] = fault
        log.warning(
            "sim.fault_injected",
            monitored_service=self._service,
            kind=str(kind),
            magnitude=fault.magnitude,
            duration_seconds=duration,
            injected_by=injected_by,
        )
        return fault

    def clear(self, fault_id: str | None = None) -> int:
        """Remove one fault, or all of them. Returns how many were removed."""
        if fault_id is None:
            removed = len(self._faults)
            self._faults.clear()
            if removed:
                log.info("sim.faults_cleared", monitored_service=self._service, count=removed)
            return removed
        if self._faults.pop(fault_id, None) is not None:
            log.info("sim.fault_cleared", monitored_service=self._service, fault_id=fault_id)
            return 1
        return 0

    def active(self, now: datetime | None = None) -> list[Fault]:
        """Active faults, pruning anything expired.

        Expiry is applied here rather than on a timer: if this method is never called the
        faults have no effect anyway, so there is no path by which an expired fault keeps
        acting.
        """
        moment = now or utc_now()
        for fault_id, fault in list(self._faults.items()):
            if fault.is_expired(moment):
                del self._faults[fault_id]
                log.info(
                    "sim.fault_expired",
                    monitored_service=self._service,
                    kind=str(fault.kind),
                    fault_id=fault_id,
                )
        return sorted(self._faults.values(), key=lambda f: f.started_at)

    def has(self, kind: FaultKind) -> bool:
        return any(fault.kind is kind for fault in self.active())

    # -- reduction to effects ---------------------------------------------
    def effects(self, now: datetime | None = None) -> FaultEffects:
        """Collapse the active faults into a single set of numbers for the traffic model."""
        effects = FaultEffects()
        moment = now or utc_now()

        for fault in self.active(moment):
            match fault.kind:
                case FaultKind.NETWORK_LATENCY:
                    # Ramp in over the first 20% of the window; a step change in latency
                    # looks synthetic, and a gradual onset is harder (more realistic) to
                    # detect.
                    ramp = min(1.0, fault.elapsed_fraction(moment) / 0.2)
                    effects.extra_latency_seconds += fault.magnitude * ramp

                case FaultKind.ERROR_INJECTION:
                    effects.extra_error_ratio += fault.magnitude

                case FaultKind.MEMORY_HOG:
                    # Linear climb toward the target over the fault's lifetime. Expressed
                    # as growth per tick so the model integrates it and the result is a
                    # monotonic trend rather than a spike.
                    span = max(1.0, fault.duration_seconds)
                    ticks = span / self._tick_seconds
                    effects.memory_growth_per_tick += max(0.0, fault.magnitude) / ticks

                case FaultKind.CPU_HOG:
                    effects.extra_cpu_ratio += fault.magnitude

                case FaultKind.TRAFFIC_SPIKE:
                    # Smooth rise and fall, so the surge has a shape a forecaster could
                    # plausibly have missed rather than an instantaneous jump.
                    shape = math.sin(math.pi * fault.elapsed_fraction(moment))
                    effects.traffic_multiplier *= 1.0 + (fault.magnitude - 1.0) * shape

                case FaultKind.QUEUE_BACKLOG:
                    effects.extra_queue_depth += fault.magnitude

                case FaultKind.SETTLEMENT_DELAY:
                    effects.extra_settlement_lag_seconds += fault.magnitude

                case FaultKind.BAD_DEPLOY:
                    # A bad release is not one clean signal - it is errors and latency
                    # together, starting abruptly at the deploy. That combination is
                    # exactly what module 7 must attribute to a `change` rather than to
                    # load, so it is modelled as a composite.
                    scale = fault.magnitude
                    effects.extra_error_ratio += 0.12 * scale
                    effects.extra_latency_seconds += 0.18 * scale
                    effects.extra_queue_depth += 60.0 * scale

                case FaultKind.KILL_SERVICE:
                    # Handled by the route layer exiting the process.
                    pass

        return effects
