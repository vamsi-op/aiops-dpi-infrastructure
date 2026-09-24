"""Rolling-origin backtesting, and the MAPE comparison that justifies the model (module 5).

The module's verification criterion is that recorded MAPE **beats a seasonal-naive baseline**. A
forecaster nobody has compared against a trivial alternative is an unvalidated guess, so this is
not a nicety - it is the evidence.

Rolling origin, not a single split
----------------------------------
One hold-out gives one number, and that number depends on which slice of history happened to be
last. Cut on a quiet Sunday and any model looks accurate; cut at a Friday peak and it looks poor.
Several origins spaced a day apart and the median error across them is a figure that survives
being re-run tomorrow.

MAPE, with a floor
------------------
Mean absolute percentage error is the right shape for this - a 2 tps error means something very
different on transaction-request-service (6 tps) than on central-ledger (26 tps) - but it divides
by the actual, so a near-zero trough produces an enormous percentage from a trivial absolute miss.
Actuals below `MIN_ACTUAL_TPS` are skipped, and the count of skipped points is reported so the
exclusion is visible rather than quietly improving the score.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from aiops_common.log import get_logger

from .forecaster import Observation, fit, forecast, seasonal_naive

log = get_logger(__name__)

# Origins to test, and how far apart. 3 x 24h covers three different days of the week without
# needing history the replay does not have.
BACKTEST_ORIGINS = 3
ORIGIN_SPACING_HOURS = 24

# Below this, a percentage error is dominated by the divisor rather than the miss.
MIN_ACTUAL_TPS = 1.0


@dataclass(frozen=True)
class HorizonAccuracy:
    horizon_hours: int
    model_mape: float | None
    baseline_mape: float | None
    points_scored: int
    points_skipped: int
    origins: int

    @property
    def beats_baseline(self) -> bool:
        if self.model_mape is None or self.baseline_mape is None:
            return False
        return self.model_mape < self.baseline_mape

    @property
    def improvement(self) -> float | None:
        """Relative reduction in error against the baseline, as a fraction."""
        if self.model_mape is None or not self.baseline_mape:
            return None
        return (self.baseline_mape - self.model_mape) / self.baseline_mape

    def as_dict(self) -> dict[str, float | int | bool | None]:
        return {
            "horizon_hours": self.horizon_hours,
            "model_mape": round(self.model_mape, 5) if self.model_mape is not None else None,
            "baseline_mape": (
                round(self.baseline_mape, 5) if self.baseline_mape is not None else None
            ),
            "beats_baseline": self.beats_baseline,
            "improvement": round(self.improvement, 5) if self.improvement is not None else None,
            "points_scored": self.points_scored,
            "points_skipped": self.points_skipped,
            "origins": self.origins,
        }


def _mape(pairs: list[tuple[float, float]]) -> tuple[float | None, int, int]:
    """Return (MAPE, scored, skipped) for (actual, predicted) pairs."""
    errors = []
    skipped = 0
    for actual, predicted in pairs:
        if actual < MIN_ACTUAL_TPS:
            skipped += 1
            continue
        errors.append(abs(actual - predicted) / actual)
    if not errors:
        return None, 0, skipped
    return statistics.fmean(errors), len(errors), skipped


def evaluate(
    service: str, history: list[Observation], horizons: list[int]
) -> dict[int, HorizonAccuracy]:
    """Backtest every horizon at several origins. Keyed by horizon."""
    results: dict[int, HorizonAccuracy] = {}
    if not history:
        return results

    ordered = sorted(history, key=lambda obs: obs.at)
    actuals = {obs.at.replace(minute=0, second=0, microsecond=0): obs.tps for obs in ordered}

    for horizon in horizons:
        model_pairs: list[tuple[float, float]] = []
        baseline_pairs: list[tuple[float, float]] = []
        origins_used = 0

        for index in range(BACKTEST_ORIGINS):
            # Each origin needs `horizon` hours of actuals after it to score against.
            offset = horizon + index * ORIGIN_SPACING_HOURS
            if offset >= len(ordered):
                continue
            cut = len(ordered) - offset
            train = ordered[:cut]
            origin_at = train[-1].at

            # Refit on the training slice only. Reusing a profile fitted on everything would leak
            # the hold-out into the factors and report an accuracy the model cannot reproduce in
            # production - the most common way a backtest flatters itself.
            profile = fit(service, train)
            if profile is None:
                continue
            origins_used += 1

            for at, predicted, _lower, _upper in forecast(
                profile, train, start=origin_at, hours=horizon
            ):
                actual = actuals.get(at)
                if actual is not None:
                    model_pairs.append((actual, predicted))

            for at, predicted in seasonal_naive(train, start=origin_at, hours=horizon):
                actual = actuals.get(at)
                if actual is not None:
                    baseline_pairs.append((actual, predicted))

        model_mape, scored, skipped = _mape(model_pairs)
        baseline_mape, _, _ = _mape(baseline_pairs)
        accuracy = HorizonAccuracy(
            horizon_hours=horizon,
            model_mape=model_mape,
            baseline_mape=baseline_mape,
            points_scored=scored,
            points_skipped=skipped,
            origins=origins_used,
        )
        results[horizon] = accuracy

        log.info(
            "backtest.horizon",
            monitored_service=service,
            horizon_hours=horizon,
            model_mape=round(model_mape, 4) if model_mape is not None else None,
            baseline_mape=round(baseline_mape, 4) if baseline_mape is not None else None,
            beats_baseline=accuracy.beats_baseline,
            origins=origins_used,
            scored=scored,
        )
        if model_mape is not None and baseline_mape is not None and not accuracy.beats_baseline:
            # Loud, because the module's stated criterion is exactly this comparison. Losing to
            # the baseline means the model is not earning its place and should be replaced, not
            # quietly shipped.
            log.warning(
                "backtest.lost_to_baseline",
                monitored_service=service,
                horizon_hours=horizon,
                model_mape=round(model_mape, 4),
                baseline_mape=round(baseline_mape, 4),
            )

    return results


def headroom_for(mape: float | None, *, floor: float) -> float:
    """Turn forecast accuracy into the capacity headroom a recommendation should carry.

    Provisioning for exactly the predicted peak means being under-provisioned half the time, by
    construction. The margin has to be at least as large as the model's typical error, so it is
    derived from the measured MAPE rather than picked - and `floor` keeps a minimum cushion even if
    a backtest reports suspiciously perfect accuracy.

    Module 2 measured what running out of capacity costs: at a load factor of 1.26, central-ledger
    served 100% errors with a 56-second p99. The asymmetry is the whole argument for headroom -
    one replica too many is cheap, one too few is an outage.
    """
    if mape is None:
        return floor
    return max(floor, min(mape, 1.0))
