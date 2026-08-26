"""Display-level health classification for the service grid.

These thresholds answer "what colour is this tile right now?". They are deliberately
separate from the anomaly detector's ML thresholds, which answer the harder question of
whether behaviour is *unusual for this service at this time of day*.

Both are useful and they disagree on purpose. A service running at 95% memory every
night at settlement is not anomalous, but an operator still wants to see amber. A
service at 40% memory when it is normally at 12% is anomalous, but the tile stays green
because nothing is actually at risk yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["healthy", "degraded", "critical", "stale", "unknown"]


@dataclass(frozen=True)
class Thresholds:
    """Warning and critical trip points for each display signal."""

    error_ratio_warning: float = 0.01  # 1% of requests failing
    error_ratio_critical: float = 0.05  # 5% is a user-visible outage
    latency_p99_warning_seconds: float = 1.0
    latency_p99_critical_seconds: float = 3.0
    cpu_warning: float = 0.75
    cpu_critical: float = 0.90
    memory_warning: float = 0.80
    memory_critical: float = 0.92
    queue_warning: float = 50.0
    queue_critical: float = 200.0
    # Mojaloop-specific: settlement falling behind is a business problem before it is
    # ever an infrastructure one.
    settlement_lag_warning_seconds: float = 30.0
    settlement_lag_critical_seconds: float = 120.0


DEFAULT_THRESHOLDS = Thresholds()

_SEVERITY_ORDER: dict[Status, int] = {
    "unknown": 0,
    "stale": 1,
    "healthy": 2,
    "degraded": 3,
    "critical": 4,
}


def _classify(
    value: float, warning: float, critical: float, label: str
) -> tuple[Status, str | None]:
    if value >= critical:
        return "critical", f"{label} at {value:.3g} (critical above {critical:.3g})"
    if value >= warning:
        return "degraded", f"{label} at {value:.3g} (warning above {warning:.3g})"
    return "healthy", None


def evaluate(
    snapshot: dict[str, float],
    *,
    stale: bool = False,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> tuple[Status, list[str]]:
    """Return the worst status across all signals, plus the reasons for it.

    Reasons are returned so the UI can explain a colour instead of just showing it.
    """
    if stale:
        return "stale", ["no samples received recently"]

    checks = (
        (
            snapshot.get("error_ratio", 0.0),
            thresholds.error_ratio_warning,
            thresholds.error_ratio_critical,
            "error ratio",
        ),
        (
            snapshot.get("latency_p99_seconds", 0.0),
            thresholds.latency_p99_warning_seconds,
            thresholds.latency_p99_critical_seconds,
            "p99 latency",
        ),
        (
            snapshot.get("cpu_utilisation_ratio", 0.0),
            thresholds.cpu_warning,
            thresholds.cpu_critical,
            "CPU",
        ),
        (
            snapshot.get("memory_utilisation_ratio", 0.0),
            thresholds.memory_warning,
            thresholds.memory_critical,
            "memory",
        ),
        (
            snapshot.get("queue_depth", 0.0),
            thresholds.queue_warning,
            thresholds.queue_critical,
            "queue depth",
        ),
        (
            snapshot.get("settlement_lag_seconds", 0.0),
            thresholds.settlement_lag_warning_seconds,
            thresholds.settlement_lag_critical_seconds,
            "settlement lag",
        ),
    )

    worst: Status = "healthy"
    reasons: list[str] = []
    for value, warning, critical, label in checks:
        status, reason = _classify(value, warning, critical, label)
        if reason:
            reasons.append(reason)
        if _SEVERITY_ORDER[status] > _SEVERITY_ORDER[worst]:
            worst = status
    return worst, reasons


def worst_of(statuses: list[Status]) -> Status:
    """Roll several service statuses up into one platform-level status."""
    if not statuses:
        return "unknown"
    return max(statuses, key=lambda status: _SEVERITY_ORDER[status])
