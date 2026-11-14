"""Turn a predicted peak into a replica count (module 5).

This is where a forecast becomes an action. Module 6 reads `recommended_replicas` and
`scaling_action` and executes them, so the arithmetic here has operational consequences rather
than presentational ones.

Capacity comes from Prometheus, not from a constant
--------------------------------------------------
`dpi_capacity_tps` and `dpi_replicas` are exported by the services themselves, so
`tps_per_replica = capacity / replicas` is measured rather than assumed. Hard-coding it would
mean every scaling decision silently depended on a number nobody had checked since it was typed,
and module 2's whole point is that scaling genuinely relieves pressure - which only holds if the
capacity figure is the real one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aiops_common.log import get_logger
from aiops_common.promql import PrometheusClient

log = get_logger(__name__)

CAPACITY_QUERY = "dpi:capacity_tps"
REPLICAS_QUERY = "dpi:replicas"

# Bounds on any recommendation. The upper bound is a safety rail rather than a capacity limit: a
# forecast corrupted by bad data should not be able to ask for a hundred containers.
MIN_REPLICAS = 1
MAX_REPLICAS = 20

# Fallback when Prometheus cannot be reached, matching `PredictionEvent.tps_per_replica`'s own
# default so the two cannot disagree.
DEFAULT_TPS_PER_REPLICA = 50.0


@dataclass(frozen=True)
class ServiceCapacity:
    service: str
    tps_per_replica: float
    current_replicas: int
    # False when the figures came from the fallback rather than from Prometheus.
    measured: bool


@dataclass(frozen=True)
class Recommendation:
    current_replicas: int
    recommended_replicas: int
    tps_per_replica: float
    headroom: float
    required_capacity_tps: float

    @property
    def action(self) -> str:
        if self.recommended_replicas > self.current_replicas:
            return "scale_out"
        if self.recommended_replicas < self.current_replicas:
            return "scale_in"
        return "hold"


async def read_capacity(client: PrometheusClient) -> dict[str, ServiceCapacity]:
    """Measure `tps_per_replica` and the current replica count for every service."""
    capacities: dict[str, ServiceCapacity] = {}
    try:
        capacity_tps = await client.scalar_by_label(CAPACITY_QUERY, "service")
        replicas = await client.scalar_by_label(REPLICAS_QUERY, "service")
    except Exception as exc:  # noqa: BLE001 - a forecast without capacity is still useful
        log.warning("capacity.read_failed", error=str(exc))
        return capacities

    for service, capacity in capacity_tps.items():
        count = int(round(replicas.get(service, 1.0))) or 1
        per_replica = (
            capacity / count if count > 0 and capacity > 0 else DEFAULT_TPS_PER_REPLICA
        )
        capacities[service] = ServiceCapacity(
            service=service,
            tps_per_replica=per_replica,
            current_replicas=count,
            measured=capacity > 0,
        )

    log.info("capacity.read", services=len(capacities))
    return capacities


def fallback_capacity(service: str, current_replicas: int = 1) -> ServiceCapacity:
    return ServiceCapacity(
        service=service,
        tps_per_replica=DEFAULT_TPS_PER_REPLICA,
        current_replicas=max(MIN_REPLICAS, current_replicas),
        measured=False,
    )


def recommend(peak_tps: float, capacity: ServiceCapacity, *, headroom: float) -> Recommendation:
    """How many replicas the predicted peak needs, with headroom.

    `headroom` comes from the backtest, not from a constant - see `backtest.headroom_for`.
    Provisioning for exactly the predicted peak means being under-provisioned whenever the forecast
    errs high, which is half the time by construction.

    Rounded up, always. The asymmetry is the point: module 2 measured that a load factor of 1.26
    produced 100% errors and a 56-second p99, while 8 replicas against the same traffic gave 0.2%
    errors. One replica too many costs a little CPU; one too few costs the platform.
    """
    per_replica = max(capacity.tps_per_replica, 1e-6)
    required_capacity = peak_tps * (1.0 + headroom)
    needed = math.ceil(required_capacity / per_replica)
    recommended = min(max(needed, MIN_REPLICAS), MAX_REPLICAS)

    if needed > MAX_REPLICAS:
        # Worth a warning rather than a silent clamp: it means either the forecast is wrong or the
        # service genuinely cannot be scaled into its predicted load, and both need a human.
        log.warning(
            "capacity.recommendation_clamped",
            monitored_service=capacity.service,
            needed=needed,
            clamped_to=MAX_REPLICAS,
            peak_tps=round(peak_tps, 2),
        )

    return Recommendation(
        current_replicas=capacity.current_replicas,
        recommended_replicas=recommended,
        tps_per_replica=per_replica,
        headroom=headroom,
        required_capacity_tps=required_capacity,
    )
