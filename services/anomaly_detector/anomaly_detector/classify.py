"""Turn feature deviations into an `AnomalyKind`, a severity and a description (module 4).

This is the bridge between statistics and action. Module 6 selects a playbook by matching on
`AnomalyKind`, so a misclassification does not merely mislabel a chart - it picks the wrong
remediation. `restart_on_oom` against a traffic surge would restart a healthy service while the
real problem, insufficient capacity, goes untouched.

Direction is the subtle part
----------------------------
Deviations are signed, and for most signals only one direction is a problem. Latency two sigma
*below* baseline is a service having a good day, not an incident. Treating |deviation| as the
signal - the obvious implementation - would raise `latency_spike` on unusually fast responses.

Traffic is the exception and needs both: a surge threatens capacity, and a collapse means
callers have stopped arriving, which is often the first visible sign of an upstream outage.
Hence `AnomalyKind.TRAFFIC_SURGE` and `TRAFFIC_DROP` as separate kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from aiops_common.constants import AnomalyKind, Severity

# A feature is implicated once it exceeds this many sigma in the direction that matters.
# Two sigma is roughly the 98th percentile of a normal distribution, which pairs sensibly with
# `ANOMALY_CONTAMINATION = 0.02`.
IMPLICATION_SIGMA: Final[float] = 2.0

# Above this, one signal is dominant enough to name the kind on its own even if others are
# mildly elevated.
DOMINANT_SIGMA: Final[float] = 4.0

# Implicated signals beyond this and the event is multivariate regardless of which is largest.
#
# This matters for module 7 rather than module 6. The simulator's `bad_deploy` fault raises
# errors, latency and queue depth together, on purpose, because that composite signature is
# what has to be attributed to a *change* rather than to load. Labelling it `error_burst`
# because errors happened to deviate most would throw away the evidence that several signals
# moved at once.
MULTIVARIATE_COUNT: Final[int] = 3

# feature -> (kind, direction). +1 means high is bad, -1 means low is bad.
_HIGH_IS_BAD: Final[dict[str, AnomalyKind]] = {
    "error_ratio": AnomalyKind.ERROR_BURST,
    "latency_p99_seconds": AnomalyKind.LATENCY_SPIKE,
    "latency_p50_seconds": AnomalyKind.LATENCY_SPIKE,
    "memory_utilisation_ratio": AnomalyKind.MEMORY_PRESSURE,
    "cpu_utilisation_ratio": AnomalyKind.CPU_SATURATION,
    "queue_depth": AnomalyKind.QUEUE_BACKLOG,
    "settlement_lag_seconds": AnomalyKind.SETTLEMENT_DELAY,
    # Connection count rising alongside nothing else usually means callers are piling up
    # waiting, which surfaces as a backlog before it surfaces as latency.
    "active_connections": AnomalyKind.QUEUE_BACKLOG,
}

# Order used to break ties when two signals deviate by a similar amount. Ranked by how
# specific the resulting playbook is: restarting for memory pressure is a precise response,
# whereas "cpu is high" is a symptom of almost anything.
_KIND_PRIORITY: Final[tuple[AnomalyKind, ...]] = (
    AnomalyKind.MEMORY_PRESSURE,
    AnomalyKind.ERROR_BURST,
    AnomalyKind.TRAFFIC_SURGE,
    AnomalyKind.SETTLEMENT_DELAY,
    AnomalyKind.QUEUE_BACKLOG,
    AnomalyKind.LATENCY_SPIKE,
    AnomalyKind.TRAFFIC_DROP,
    AnomalyKind.CPU_SATURATION,
)

_HUMAN: Final[dict[str, str]] = {
    "tps": "transaction rate",
    "error_ratio": "error ratio",
    "latency_p50_seconds": "median latency",
    "latency_p99_seconds": "p99 latency",
    "cpu_utilisation_ratio": "CPU utilisation",
    "memory_utilisation_ratio": "memory utilisation",
    "queue_depth": "queue depth",
    "active_connections": "active connections",
    "settlement_lag_seconds": "settlement lag",
}


@dataclass(frozen=True)
class Classification:
    kind: AnomalyKind
    severity: Severity
    # Implicated features, most deviant first. Fills `AnomalyEvent.contributing_features`.
    contributing: list[str]
    description: str


def _implicated(deviations: dict[str, float]) -> list[tuple[str, float, AnomalyKind]]:
    """Features deviating in the direction that actually indicates a problem."""
    found: list[tuple[str, float, AnomalyKind]] = []
    for feature, deviation in deviations.items():
        if feature == "tps":
            if deviation >= IMPLICATION_SIGMA:
                found.append((feature, deviation, AnomalyKind.TRAFFIC_SURGE))
            elif deviation <= -IMPLICATION_SIGMA:
                found.append((feature, deviation, AnomalyKind.TRAFFIC_DROP))
            continue

        kind = _HIGH_IS_BAD.get(feature)
        if kind is not None and deviation >= IMPLICATION_SIGMA:
            found.append((feature, deviation, kind))

    # Most deviant first, by magnitude.
    found.sort(key=lambda item: abs(item[1]), reverse=True)
    return found


def _severity(score: float, threshold: float, implicated: int) -> Severity:
    """Map score to severity.

    Anchored on `threshold` rather than on fixed score values, so changing
    `ANOMALY_SCORE_THRESHOLD` moves the whole scale coherently instead of leaving the
    severity bands pointing somewhere the threshold no longer is.
    """
    # Below the alerting threshold the event is recorded but not actionable - `is_actionable`
    # on the schema already says so. Informational rather than silent, because a run of
    # sub-threshold anomalies is itself a signal worth being able to query.
    if score < threshold:
        return Severity.INFO
    # Well past the threshold, or several signals wrong at once. A cascade is more serious
    # than a single elevated metric even at the same score.
    midpoint = threshold + (1.0 - threshold) / 2.0
    if score >= midpoint or implicated >= MULTIVARIATE_COUNT:
        return Severity.CRITICAL
    return Severity.WARNING


def _describe(
    service: str, kind: AnomalyKind, ranked: list[tuple[str, float]], *, implicated: bool
) -> str:
    """Build the human-readable explanation stored on the event.

    ASCII only. "sigma" is spelled out rather than using the symbol, because this string is
    printed to consoles, written to logs and rendered in the dashboard - and a Windows console
    defaults to cp1252, where U+03C3 raises UnicodeEncodeError. That crashed the verification
    script twice before the cause was obvious. Same trap as the em dashes that broke the
    PowerShell scripts, one layer further in: stored data rather than source.
    """
    if not ranked:
        return (
            f"{service}: the model finds this sample unusual, but no individual signal "
            "deviates from its hour-of-day baseline."
        )

    parts = []
    for feature, deviation in ranked[:3]:
        name = _HUMAN.get(feature, feature)
        direction = "above" if deviation > 0 else "below"
        parts.append(f"{name} {abs(deviation):.1f} sigma {direction} baseline")
    joined = ", ".join(parts)

    if not implicated:
        # The forest decided, not the baseline. Say so rather than implying a threshold was
        # crossed, and still name the signals - an unexplained anomaly gets ignored.
        return (
            f"{service}: unusual combination of individually normal signals - none exceeds "
            f"{IMPLICATION_SIGMA:.0f} sigma. Largest movements: {joined}"
        )
    return f"{service}: {kind.value.replace('_', ' ')} - {joined}"


def classify(
    service: str, deviations: dict[str, float], score: float, threshold: float
) -> Classification:
    """Pick a kind, severity and description from signed sigma deviations."""
    implicated = _implicated(deviations)
    severity = _severity(score, threshold, len(implicated))

    if not implicated:
        # The forest flagged it but no individual signal crossed the implication threshold -
        # precisely the case an Isolation Forest exists to catch and a threshold rule cannot: a
        # combination that is abnormal even though every component is within range.
        #
        # `contributing_features` is still populated, with the largest movements regardless of
        # threshold. Leaving it empty was the original behaviour and it is worse than useless:
        # an anomaly that names no signals cannot be triaged, module 6 has nothing to match, and
        # the operator's only option is to go and read the raw metrics - which is the work this
        # platform exists to remove. The threshold governs which *kind* is claimed, not whether
        # an explanation is offered.
        kind = AnomalyKind.MULTIVARIATE
        ranked = sorted(deviations.items(), key=lambda kv: abs(kv[1]), reverse=True)
        top = [(feature, value) for feature, value in ranked[:3] if abs(value) > 0.0]
        return Classification(
            kind,
            severity,
            [feature for feature, _ in top],
            _describe(service, kind, top, implicated=False),
        )

    top_feature, top_deviation, top_kind = implicated[0]
    distinct_kinds = {k for _, _, k in implicated}

    tps_dev = deviations.get("tps", 0.0)
    has_traffic_surge = (
        AnomalyKind.TRAFFIC_SURGE in distinct_kinds and tps_dev >= IMPLICATION_SIGMA
    )
    has_dominant_memory = (
        AnomalyKind.MEMORY_PRESSURE in distinct_kinds
        and deviations.get("memory_utilisation_ratio", 0.0) >= DOMINANT_SIGMA
    )
    has_dominant_errors = (
        AnomalyKind.ERROR_BURST in distinct_kinds
        and deviations.get("error_ratio", 0.0) >= DOMINANT_SIGMA
    )

    if has_traffic_surge and not (has_dominant_memory or has_dominant_errors):
        # When traffic surges, latency, queue depth and CPU rise as natural symptoms of load.
        # The primary root driver is the traffic surge itself, requiring scale-out rather than restart.
        kind = AnomalyKind.TRAFFIC_SURGE
    elif abs(top_deviation) >= DOMINANT_SIGMA:
        # One signal is extreme. Name it, even if others are mildly elevated - they are
        # almost certainly consequences of this one.
        kind = top_kind
    elif len(implicated) >= MULTIVARIATE_COUNT:
        kind = AnomalyKind.MULTIVARIATE
    elif len(distinct_kinds) > 1:
        # Two comparable signals pointing at different kinds. Break the tie on how specific
        # the resulting remediation is, rather than on whichever deviated marginally more.
        kind = next(k for k in _KIND_PRIORITY if k in distinct_kinds)
    else:
        kind = top_kind

    return Classification(
        kind=kind,
        severity=severity,
        contributing=[feature for feature, _, _ in implicated],
        description=_describe(
            service,
            kind,
            [(feature, deviation) for feature, deviation, _ in implicated],
            implicated=True,
        ),
    )
