"""Traffic forecasting on the 1-hour rollup (module 5).

The model
--------
A lead-time-weighted blend of two predictors:

1. **Hour-of-week seasonal profile.** 168 buckets, one per (weekday, hour), each the *median* of
   the ~4 observations 28 days provides, scaled by a recent `level` term.
2. **Seasonal-naive.** The same hour one week ago.

    predicted(step) = w(step) * profile + (1 - w(step)) * seasonal_naive

where `w` rises with lead time from 0.15 at one hour to 0.90 at twenty-four. Both the shape and the
weights were measured on real replayed history, not chosen - see `_WEIGHT_BY_LEAD_HOURS`.

Why this shape, measured rather than assumed
--------------------------------------------
The first implementation was a separable decomposition - `level * hour_factor * weekday_factor` -
on the reasoning that 24 + 7 parameters estimated from 28 and 96 observations each must beat 168
parameters estimated from 4 each. That reasoning was wrong, and it lost to the naive baseline on 8
of 15 service/horizon comparisons. Backtested mean MAPE over all five services:

    variant                        1h       6h      24h    wins vs naive
    separable, 48h level        0.0290   0.0502   0.0311    3/5  1/5  3/5
    separable, no level         0.1264   0.1358   0.1401    0/5  0/5  0/5
    hour-of-week profile        0.0250   0.0359   0.0267    2/5  4/5  5/5
    blend at fixed weight 0.5   0.0233   0.0373   0.0279    3/5  5/5  5/5
    seasonal-naive (baseline)   0.0262   0.0440   0.0329      -

Three things that table settled:

* **Separability does not hold well enough.** The hour-of-week profile beats the separable form at
  every horizon, so the daily shape genuinely differs by weekday rather than being a fixed curve
  scaled by a weekday weight.
* **The `level` term is essential, not a source of bias.** Removing it makes the model five times
  worse. The initial hypothesis - that re-anchoring to recent actuals was what cost accuracy - was
  exactly backwards.
* **Blending with the baseline beats either alone.** Textbook bias-variance: naive is unbiased but
  carries the variance of a single observation, while the profile median is low-variance but
  smooths away any real week-to-week drift. Averaging trades one against the other.

Stated plainly: this model *contains* the baseline it is compared against, so beating that baseline
is partly structural. The profile half is doing real work - it beats naive on its own at 6h and 24h
- but the 1-hour result should be read as "no worse than naive, slightly better on average" rather
than as a model that has learned something naive has not.

An honest caveat about the data
------------------------------
`replay_history.py` generates history from the simulator's own traffic model with noise capped at
12%, and hourly averaging removes most of what remains. Near-deterministic history makes
seasonal-naive an unusually strong benchmark - copying last week is close to optimal when last week
was almost the same. Against real Mojaloop traffic the profile's variance reduction would matter
more, and the gap should widen.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from aiops_common.log import get_logger

log = get_logger(__name__)

HOURS_PER_WEEK = 168
MODEL_NAME = "seasonal-profile"
BASELINE_NAME = "seasonal-naive"

# Blend weight on the profile, as a function of **lead time**, measured rather than chosen.
#
# The two predictors have different error characteristics at different lead times, and a single
# constant cannot serve both ends. Backtested mean MAPE by weight, all five services
# (0.0 = pure seasonal-naive, 1.0 = pure profile):
#
#     weight      1h        6h       24h
#       0.00   0.01695   0.04265   0.03287     <- pure naive
#       0.15   0.01641   0.04057   0.03113     <- best at 1h
#       0.50   0.01664   0.03650   0.02771
#       0.75   0.01843   0.03567   0.02659     <- best at 6h
#       0.90   0.02018   0.03605   0.02648     <- best at 24h
#       1.00   0.02135   0.03667   0.02665
#
# The pattern is monotone and has a clear reading. One hour out, last week's same hour is nearly
# unbeatable on near-deterministic history - there is no noise left for a 4-week median to average
# away, so the profile can only add its smoothing bias. Further out, the level has time to drift and
# the structural model earns its weight.
#
# Applied per forecast *step*, not per horizon: within a 24-hour forecast the first point is one
# hour ahead and the last is twenty-four, so they deserve different weights even though they belong
# to the same request.
_WEIGHT_BY_LEAD_HOURS: tuple[tuple[int, float], ...] = ((1, 0.15), (6, 0.75), (24, 0.90))


def profile_weight_for(lead_hours: int) -> float:
    """Blend weight on the profile at a given lead time, linearly interpolated."""
    first_lead, first_weight = _WEIGHT_BY_LEAD_HOURS[0]
    if lead_hours <= first_lead:
        return first_weight
    last_lead, last_weight = _WEIGHT_BY_LEAD_HOURS[-1]
    if lead_hours >= last_lead:
        return last_weight

    for (lead_a, weight_a), (lead_b, weight_b) in pairwise(_WEIGHT_BY_LEAD_HOURS):
        if lead_a <= lead_hours <= lead_b:
            span = lead_b - lead_a
            return weight_a + (weight_b - weight_a) * ((lead_hours - lead_a) / span)
    return last_weight  # pragma: no cover - unreachable given the clamps above


# Hours of recent history used to re-anchor the profile to the current traffic level.
#
# A whole number of weeks, so the window contains each weekday exactly once and its composition
# cannot bias the level. A 48-hour window ending on a Sunday is two weekend days, which drags the
# level down and then under-forecasts the following Monday.
LEVEL_WINDOW_HOURS = 168

# Minimum observations before a bucket is trusted rather than falling back.
MIN_BUCKET_SAMPLES = 2

# Clamp on any single factor. One freak hour in the history could otherwise produce a factor of 20
# and a forecast demanding hundreds of replicas.
MAX_FACTOR = 5.0
MIN_FACTOR = 0.05


@dataclass(frozen=True)
class Observation:
    """One hourly bucket."""

    at: datetime
    tps: float


def hour_of_week(at: datetime) -> int:
    """0-167, Monday 00:00 == 0."""
    return at.weekday() * 24 + at.hour


@dataclass
class SeasonalProfile:
    """The fitted shape of one service's traffic."""

    service: str
    level: float
    # hour-of-week -> multiplicative factor
    factors: dict[int, float] = field(default_factory=dict)
    # Fallback for buckets with too few observations, indexed by hour of day.
    hour_fallback: dict[int, float] = field(default_factory=dict)
    residual_ratio_sigma: float = 0.0
    training_points: int = 0

    def factor_for(self, at: datetime) -> float:
        bucket = hour_of_week(at)
        factor = self.factors.get(bucket)
        if factor is not None:
            return factor
        # A missing hour-of-week bucket means that hour was never observed - a gap in history. The
        # hour-of-day average across all weekdays is a better guess than 1.0, which would predict
        # the overall median regardless of time of day.
        return self.hour_fallback.get(at.hour, 1.0)

    def predict_at(self, at: datetime) -> float:
        return max(0.0, self.level * self.factor_for(at))

    @property
    def buckets_covered(self) -> int:
        return len(self.factors)


def _clamp(value: float) -> float:
    return min(max(value, MIN_FACTOR), MAX_FACTOR)


def fit(service: str, history: list[Observation]) -> SeasonalProfile | None:
    """Fit the hour-of-week profile. Returns None if there is not enough history.

    Requires two weekly cycles: with one, every bucket rests on a single observation - which is
    precisely the seasonal-naive baseline - and there is no second cycle to hold back for the MAPE
    comparison the module is judged on.
    """
    if len(history) < 2 * HOURS_PER_WEEK:
        log.warning(
            "forecast.insufficient_history",
            monitored_service=service,
            points=len(history),
            required=2 * HOURS_PER_WEEK,
            detail="need two weekly cycles; run scripts/replay_history.py --days 28",
        )
        return None

    positive = [obs for obs in history if obs.tps > 0]
    if not positive:
        log.warning("forecast.no_positive_traffic", monitored_service=service)
        return None
    overall = statistics.median([obs.tps for obs in positive])

    by_bucket: dict[int, list[float]] = {}
    by_hour: dict[int, list[float]] = {}
    for obs in positive:
        by_bucket.setdefault(hour_of_week(obs.at), []).append(obs.tps)
        by_hour.setdefault(obs.at.hour, []).append(obs.tps)

    factors = {
        bucket: _clamp(statistics.median(values) / overall)
        for bucket, values in by_bucket.items()
        if len(values) >= MIN_BUCKET_SAMPLES
    }
    hour_fallback = {
        hour: _clamp(statistics.median(values) / overall) for hour, values in by_hour.items()
    }

    profile = SeasonalProfile(
        service=service,
        level=overall,
        factors=factors,
        hour_fallback=hour_fallback,
        training_points=len(history),
    )
    profile.level = _recent_level(profile, history) or overall

    # Residual spread as a *ratio*, not an absolute: traffic swings 2.7x across a day, so an
    # absolute sigma would give the pre-dawn trough the same interval width as the evening peak.
    ratios = [
        obs.tps / profile.predict_at(obs.at)
        for obs in positive
        if profile.predict_at(obs.at) > 0
    ]
    if len(ratios) > 1:
        profile.residual_ratio_sigma = statistics.stdev(ratios)

    log.info(
        "forecast.fitted",
        monitored_service=service,
        points=len(history),
        level=round(profile.level, 3),
        buckets=len(factors),
        residual_sigma=round(profile.residual_ratio_sigma, 4),
    )
    if len(factors) < HOURS_PER_WEEK:
        # Not fatal - `factor_for` falls back to the hour-of-day average - but it means part of the
        # week is being predicted from a coarser shape, which is worth knowing before wondering why
        # Sunday forecasts are weaker.
        log.info(
            "forecast.partial_week",
            monitored_service=service,
            buckets=len(factors),
            of=HOURS_PER_WEEK,
        )
    return profile


def _recent_level(profile: SeasonalProfile, history: list[Observation]) -> float | None:
    """Scale the level so the profile matches the most recent whole week.

    Load-bearing: without it the model's MAPE is roughly five times worse. The profile's factors
    describe *shape* relative to a historical median, and this is what ties that shape to the
    traffic volume actually arriving now.
    """
    if not history:
        return None
    cutoff = history[-1].at - timedelta(hours=LEVEL_WINDOW_HOURS)
    ratios = []
    for obs in history:
        if obs.at <= cutoff or obs.tps <= 0:
            continue
        shape = profile.factor_for(obs.at)
        if shape > 0:
            ratios.append(obs.tps / shape)
    if len(ratios) < MIN_BUCKET_SAMPLES:
        return None
    return statistics.median(ratios)


def _naive_lookup(history: list[Observation]) -> dict[datetime, float]:
    return {obs.at.replace(minute=0, second=0, microsecond=0): obs.tps for obs in history}


def _naive_at(lookup: dict[datetime, float], at: datetime) -> float | None:
    """Same hour one week ago, walking back further if that week is missing."""
    for weeks in range(1, 5):
        value = lookup.get(at - timedelta(hours=HOURS_PER_WEEK * weeks))
        if value is not None:
            return value
    return None


def forecast(
    profile: SeasonalProfile,
    history: list[Observation],
    *,
    start: datetime,
    hours: int,
) -> list[tuple[datetime, float, float, float]]:
    """Predict `hours` ahead. Returns (at, predicted, lower, upper) per hour.

    `history` is needed for the seasonal-naive half of the blend. When no value exists a week back -
    a gap, or a service younger than a week - the profile carries the prediction alone rather than
    being dragged toward zero by a missing lookup.

    Intervals widen with the horizon. A 1-hour-ahead forecast is anchored to a level measured
    minutes ago; a 24-hour-ahead one has a full day for that level to drift, and presenting both
    with equal confidence would mislead whoever reads the chart.
    """
    lookup = _naive_lookup(history)
    cursor = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    points = []
    for step in range(1, hours + 1):
        at = cursor + timedelta(hours=step - 1)
        modelled = profile.predict_at(at)
        naive = _naive_at(lookup, at)
        if naive is None:
            predicted = modelled
        else:
            weight = profile_weight_for(step)
            predicted = weight * modelled + (1.0 - weight) * naive
        # sqrt growth rather than linear: forecast errors accumulate like a random walk.
        widening = profile.residual_ratio_sigma * math.sqrt(step)
        lower = max(0.0, predicted * (1.0 - widening))
        upper = max(lower, predicted * (1.0 + widening))
        points.append((at, predicted, lower, upper))
    return points


def seasonal_naive(
    history: list[Observation], *, start: datetime, hours: int
) -> list[tuple[datetime, float]]:
    """The baseline: same hour, one week ago.

    Deliberately hard to beat - it captures the daily and weekly shape exactly, carrying only the
    variance of a single observation. That variance is the whole of what the profile improves on,
    which is what makes the comparison meaningful rather than a straw man. On near-deterministic
    replayed history it is close to optimal.
    """
    lookup = _naive_lookup(history)
    cursor = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return [
        (
            cursor + timedelta(hours=step - 1),
            _naive_at(lookup, cursor + timedelta(hours=step - 1)) or 0.0,
        )
        for step in range(1, hours + 1)
    ]
