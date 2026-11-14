"""Traffic and resource model for a simulated DPI service.

Two jobs:

1. **Produce a load curve worth forecasting.** Module 5 has to predict traffic 1, 6 and 24
   hours ahead and beat a seasonal-naive baseline. That is only a meaningful exercise if
   the signal genuinely dominates the noise, so the daily and weekly shapes here are
   deliberately strong (roughly 2.7x trough to peak) and the noise is capped at +/-12%.

2. **Make every other signal a consequence of load.** Latency, CPU, memory, error rate and
   queue depth are all derived from one `load_factor = tps / capacity`. That correlation is
   the whole point: an Isolation Forest over independent random columns learns nothing
   useful, whereas correlated columns let it detect "latency is high *for this traffic
   level*", which is the actual job.

The second property is also what makes modules 5 and 6 provable rather than cosmetic.
Capacity is `tps_per_replica * replicas`, so a scale-out really does reduce load_factor,
which really does bring latency and queue depth back down. A remediation can be shown to
have worked.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime

from .profiles import ServiceProfile

# Relative traffic weight per weekday, Monday = 0. Payments are weekday business:
# Friday peaks, the weekend is much quieter.
WEEKDAY_WEIGHTS: tuple[float, ...] = (1.05, 1.08, 1.06, 1.04, 1.10, 0.72, 0.58)

# Load at which the system starts to visibly suffer. Below this, latency is flat and
# errors stay at baseline; above it, both climb steeply. Mirrors queueing behaviour, where
# waiting time grows sharply as utilisation approaches saturation.
KNEE = 0.80

# Per-second dynamics of the stateful signals. Expressed per second so a caller that steps
# the model less often (the history replay) can compound them over the elapsed time.
WALK_KEEP_PER_SECOND = 0.92  # traffic drift keeps 92% of its deviation from 1.0 each second
WALK_SHOCK_SD = 0.012  # ... and gains this much noise each second
MEMORY_PULL_PER_SECOND = 0.05  # memory closes 5% of the gap to its load target each second


def daily_multiplier(hour_of_day: float) -> float:
    """Two-peak daily shape: a mid-morning rise and a larger evening peak.

    Sum of two Gaussians on a floor. Ranges from about 0.31 in the pre-dawn trough to
    about 0.86 at the evening peak - a 2.7x swing, which is realistic for retail payments
    and large enough for a forecaster to lock onto.
    """
    morning = 0.45 * math.exp(-((hour_of_day - 11.0) ** 2) / (2 * 2.6**2))
    evening = 0.55 * math.exp(-((hour_of_day - 19.5) ** 2) / (2 * 2.2**2))
    return 0.30 + morning + evening


def weekly_multiplier(weekday: int) -> float:
    """Relative weight for a day of the week, Monday = 0."""
    return WEEKDAY_WEIGHTS[weekday % 7]


def seasonal_multiplier(moment: datetime) -> float:
    """Combined daily and weekly shape. A pure function of time, so it is reproducible.

    Being time-derived rather than random means all five services move together without
    needing to coordinate, and a backtest over historical timestamps gives the same curve
    the live simulator produced.
    """
    hour = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
    return daily_multiplier(hour) * weekly_multiplier(moment.weekday())


@dataclass
class ServiceState:
    """The instantaneous simulated state of one service, ready to be exported."""

    tps: float
    load_factor: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    latency_p99_seconds: float
    error_ratio: float
    cpu_ratio: float
    memory_ratio: float
    queue_depth: float
    active_connections: float
    settlement_lag_seconds: float
    quote_latency_seconds: float
    verification_latency_seconds: float
    replicas: int
    capacity_tps: float


class TrafficModel:
    """Generates the current state of one simulated service.

    Holds a small amount of state between ticks - a mean-reverting random walk and a
    memory level - so the output has realistic short-term persistence instead of being
    independent noise on every sample.
    """

    def __init__(
        self,
        profile: ServiceProfile,
        *,
        base_tps: float,
        seasonality: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._profile = profile
        self._base_tps = base_tps
        self._seasonality = seasonality
        self._rng = rng or random.Random()

        self._replicas = profile.replicas
        # Mean-reverting multiplier: drifts slowly, pulled back toward 1.0. Gives traffic
        # believable persistence without letting it wander far enough to swamp the
        # seasonal signal a forecaster needs.
        self._walk = 1.0
        # Memory is stateful in a way CPU is not: it climbs and does not immediately fall,
        # which is what makes a leak detectable as a trend rather than a spike.
        self._memory = profile.base_memory_ratio

    # -- topology ---------------------------------------------------------
    @property
    def replicas(self) -> int:
        return self._replicas

    def scale_to(self, replicas: int) -> int:
        """Change the replica count, as a scaling playbook would.

        Capacity is derived from this, so scaling has a real effect on every downstream
        signal. Module 6 acts on it and module 8 shows the result.
        """
        self._replicas = max(1, min(replicas, 50))
        return self._replicas

    @property
    def capacity_tps(self) -> float:
        return self._profile.tps_per_replica * self._replicas

    # -- the model --------------------------------------------------------
    def _advance_walk(self, elapsed_seconds: float) -> None:
        # Ornstein-Uhlenbeck style: pull toward 1.0, then add a small shock. Per second the
        # deviation keeps 92% and gains N(0, 0.012); over `elapsed_seconds` that compounds to
        # keep = 0.92^dt with the matching variance, so a coarse step lands where `dt`
        # one-second steps would have (in distribution). dt=1 is exactly the original update.
        keep = WALK_KEEP_PER_SECOND**elapsed_seconds
        shock_sd = WALK_SHOCK_SD * math.sqrt((1.0 - keep**2) / (1.0 - WALK_KEEP_PER_SECOND**2))
        self._walk = 1.0 + (self._walk - 1.0) * keep + self._rng.gauss(0.0, shock_sd)
        self._walk = max(0.88, min(1.12, self._walk))

    def sample(
        self, moment: datetime, faults: FaultEffects, *, elapsed_seconds: float = 1.0
    ) -> ServiceState:
        """Compute the current state, with any active fault effects applied.

        `elapsed_seconds` is the time since the previous call. The live simulator ticks every
        second and leaves it at 1. `scripts/replay_history.py` calls once per 60 s or 900 s
        and must pass the step: the stateful parts (traffic drift, memory) are per-second
        dynamics, and stepping them once per call made memory lag load by hours in the
        training data while tracking it within seconds live - a gap the anomaly detector
        then flagged on every live sample.
        """
        profile = self._profile
        elapsed_seconds = max(1.0, elapsed_seconds)
        self._advance_walk(elapsed_seconds)

        # --- traffic ---
        season = seasonal_multiplier(moment) if self._seasonality else 0.70
        tps = self._base_tps * profile.traffic_share * season * self._walk
        tps *= faults.traffic_multiplier
        tps = max(0.0, tps)

        capacity = self.capacity_tps
        load = tps / capacity if capacity > 0 else 0.0

        # --- latency ---
        # Flat until the knee, then grows cubically. The cubic term is what turns a
        # traffic surge into a latency incident rather than a proportional slowdown.
        pressure = max(0.0, load - KNEE) / (1.0 - KNEE + 1e-9)
        latency_scale = 1.0 + 0.35 * load + 4.0 * pressure**3
        p50 = profile.base_latency_seconds * latency_scale + faults.extra_latency_seconds

        # Lognormal tail: p95 and p99 pull away from the median as sigma grows, which is
        # how real request latency behaves.
        sigma = profile.latency_sigma * (1.0 + 0.6 * pressure)
        p95 = p50 * math.exp(1.645 * sigma)
        p99 = p50 * math.exp(2.326 * sigma)

        # --- errors ---
        # Baseline, plus a steep rise once saturated (timeouts, rejected work).
        error_ratio = profile.base_error_ratio + 0.35 * pressure**2
        error_ratio = min(1.0, error_ratio + faults.extra_error_ratio)

        # --- saturation ---
        cpu = profile.base_cpu_ratio + 0.70 * load + self._rng.gauss(0.0, 0.015)
        cpu = max(0.02, min(1.0, cpu + faults.extra_cpu_ratio))

        # Memory tracks load loosely and drifts. A memory fault adds a monotonic climb,
        # which is the signature module 4 must catch as `memory_pressure`.
        target_memory = profile.base_memory_ratio + 0.18 * load
        # 5% of the gap per second, compounded over the elapsed time (dt=1: exactly 5%).
        self._memory += (target_memory - self._memory) * (
            1.0 - (1.0 - MEMORY_PULL_PER_SECOND) ** elapsed_seconds
        )
        self._memory += faults.memory_growth_per_tick
        self._memory = max(0.05, min(1.0, self._memory))

        # --- queueing ---
        # Nothing queues until the knee; past it, depth grows quadratically.
        queue = 240.0 * pressure**2 + faults.extra_queue_depth
        connections = 6.0 * tps / max(1, self._replicas) + 4.0 * self._replicas

        # --- DPI-specific business signals ---
        # Settlement falls behind when the ledger is under pressure. This is the metric a
        # generic infrastructure monitor would never look at, and the one a payments
        # operator cares about most.
        settlement_lag = (
            (1.2 + 22.0 * pressure**2 + faults.extra_settlement_lag_seconds)
            if profile.emits_settlement_lag
            else 0.0
        )
        quote_latency = p50 * 1.6 if profile.emits_quote_latency else 0.0
        verification_latency = p50 * 1.25 if profile.emits_verification_latency else 0.0

        return ServiceState(
            tps=tps,
            load_factor=load,
            latency_p50_seconds=p50,
            latency_p95_seconds=p95,
            latency_p99_seconds=p99,
            error_ratio=error_ratio,
            cpu_ratio=cpu,
            memory_ratio=self._memory,
            queue_depth=max(0.0, queue),
            active_connections=connections,
            settlement_lag_seconds=settlement_lag,
            quote_latency_seconds=quote_latency,
            verification_latency_seconds=verification_latency,
            replicas=self._replicas,
            capacity_tps=capacity,
        )


@dataclass
class FaultEffects:
    """The combined effect of all currently active faults.

    Kept as a plain value object so `TrafficModel.sample()` stays pure with respect to
    fault handling: the registry decides what is active and expired, the model only
    applies the totals.
    """

    extra_latency_seconds: float = 0.0
    extra_error_ratio: float = 0.0
    extra_cpu_ratio: float = 0.0
    memory_growth_per_tick: float = 0.0
    extra_queue_depth: float = 0.0
    extra_settlement_lag_seconds: float = 0.0
    traffic_multiplier: float = 1.0

    @classmethod
    def none(cls) -> FaultEffects:
        return cls()
