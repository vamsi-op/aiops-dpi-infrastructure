"""Traffic prediction behaviour (module 5).

The headline assertion is the module's own criterion: the model must beat a seasonal-naive
baseline. That is what `test_model_beats_seasonal_naive_on_all_horizons` exists for, and it is not
a formality - a forecaster nobody has compared against a trivial alternative is an unvalidated
guess.

History is synthesised with the same structure the simulator generates - a daily curve times a
weekday weight - so the fixture exercises the seasonality the model is supposed to find. The live
path is covered by `scripts/check_traffic_predictor.py`.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from traffic_predictor.backtest import MIN_ACTUAL_TPS, evaluate, headroom_for
from traffic_predictor.capacity import (
    DEFAULT_TPS_PER_REPLICA,
    MAX_REPLICAS,
    MIN_REPLICAS,
    ServiceCapacity,
    fallback_capacity,
    recommend,
)
from traffic_predictor.forecaster import (
    HOURS_PER_WEEK,
    LEVEL_WINDOW_HOURS,
    MAX_FACTOR,
    Observation,
    fit,
    forecast,
    hour_of_week,
    profile_weight_for,
    seasonal_naive,
)

SERVICE = "central-ledger"
BASE_TPS = 26.0

# Mirrors mojaloop_sim.traffic: a two-peak daily curve times a weekday weight. Friday 1.10 against
# Sunday 0.58 is the 1.9x swing module 5 has to learn.
WEEKDAY_WEIGHTS = (1.05, 1.08, 1.06, 1.04, 1.10, 0.72, 0.58)


def _shape(at: datetime) -> float:
    daily = 1.0 + 0.45 * math.sin(2 * math.pi * ((at.hour - 3) / 24.0))
    return daily * WEEKDAY_WEIGHTS[at.weekday()]


def history(
    *, days: int = 28, noise: float = 0.06, seed: int = 11, end: datetime | None = None
) -> list[Observation]:
    """Hourly observations with the simulator's seasonal structure plus noise."""
    rng = random.Random(seed)
    finish = end or datetime(2026, 9, 21, 23, 0, tzinfo=UTC)
    start = finish - timedelta(hours=days * 24 - 1)
    out = []
    cursor = start
    while cursor <= finish:
        value = BASE_TPS * _shape(cursor) * rng.uniform(1 - noise, 1 + noise)
        out.append(Observation(at=cursor, tps=round(value, 4)))
        cursor += timedelta(hours=1)
    return out


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------
def test_fit_requires_two_weekly_cycles() -> None:
    """One week cannot validate weekly seasonality, so fitting on it is refused.

    With a single cycle every weekday factor rests on one observation and there is no second cycle
    to hold back - which would make the MAPE comparison this module is judged on impossible.
    """
    assert fit(SERVICE, history(days=7)) is None
    assert fit(SERVICE, history(days=14)) is not None


def test_fit_covers_the_whole_week() -> None:
    profile = fit(SERVICE, history())
    assert profile is not None
    # 28 days gives 4 observations per hour-of-week bucket, so all 168 should be populated.
    assert profile.buckets_covered == HOURS_PER_WEEK
    assert len(profile.hour_fallback) == 24


def test_fit_recovers_the_daily_shape() -> None:
    profile = fit(SERVICE, history())
    assert profile is not None

    # Compare the same weekday at its generated peak and trough hours.
    peak = profile.factor_for(datetime(2026, 9, 16, 9, tzinfo=UTC))
    trough = profile.factor_for(datetime(2026, 9, 16, 21, tzinfo=UTC))
    assert peak > trough
    assert 1.8 < peak / trough < 3.5


def test_fit_recovers_the_weekday_shape() -> None:
    """The reason 28 days of history was worth generating.

    Compared at a fixed hour so only the weekday differs - with an hour-of-week profile there is no
    separate weekday factor to inspect, which is the point: the daily shape is allowed to differ by
    day rather than being one curve scaled by a weight.
    """
    profile = fit(SERVICE, history())
    assert profile is not None

    friday = profile.factor_for(datetime(2026, 9, 18, 12, tzinfo=UTC))
    sunday = profile.factor_for(datetime(2026, 9, 20, 12, tzinfo=UTC))
    assert friday > sunday
    # Against the generated 1.10 / 0.58 = 1.90.
    assert 1.6 < friday / sunday < 2.2


def test_factors_are_clamped() -> None:
    # One freak hour must not produce a factor of 20 and a forecast demanding hundreds of replicas.
    spiked = history()
    for index in range(-6, 0):
        spiked[index] = Observation(at=spiked[index].at, tps=BASE_TPS * 500)
    profile = fit(SERVICE, spiked)
    assert profile is not None
    assert all(value <= MAX_FACTOR for value in profile.factors.values())


def test_missing_bucket_falls_back_to_hour_of_day() -> None:
    """A gap in history must not predict the overall median regardless of time of day."""
    profile = fit(SERVICE, history())
    assert profile is not None
    bucket = hour_of_week(datetime(2026, 9, 16, 9, tzinfo=UTC))
    del profile.factors[bucket]

    fallback = profile.factor_for(datetime(2026, 9, 16, 9, tzinfo=UTC))
    assert fallback == profile.hour_fallback[9]
    assert fallback != 1.0


def test_level_tracks_a_recent_shift() -> None:
    """The term that stops this being a lookup table.

    A genuine change in traffic volume must be picked up, not forecast away as noise.
    """
    base = history()
    quiet = fit(SERVICE, base)
    assert quiet is not None

    # The level window is a whole week (168h), so the shift has to cover it to move the median.
    # A 48-hour shift inside a 168-hour window leaves the median where it was - which is the point
    # of using whole weeks: the window's weekday composition cannot bias the level.
    doubled = [
        Observation(
            at=obs.at,
            tps=obs.tps * 2 if index >= len(base) - LEVEL_WINDOW_HOURS else obs.tps,
        )
        for index, obs in enumerate(base)
    ]
    shifted = fit(SERVICE, doubled)
    assert shifted is not None
    assert shifted.level > quiet.level * 1.5


def test_fit_ignores_zero_traffic_buckets() -> None:
    # A service that was down contributes no information about its normal shape, and treating an
    # outage as a low-traffic hour would drag the profile toward it.
    with_gaps = history()
    for index in range(100, 140):
        with_gaps[index] = Observation(at=with_gaps[index].at, tps=0.0)
    profile = fit(SERVICE, with_gaps)
    assert profile is not None
    assert profile.level > BASE_TPS * 0.5


def test_fit_returns_none_when_there_is_no_traffic() -> None:
    dead = [Observation(at=obs.at, tps=0.0) for obs in history()]
    assert fit(SERVICE, dead) is None


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------
def test_forecast_produces_one_point_per_hour_on_hour_boundaries() -> None:
    rows = history()
    profile = fit(SERVICE, rows)
    assert profile is not None
    points = forecast(profile, rows, start=datetime(2026, 9, 21, 23, 30, tzinfo=UTC), hours=24)

    assert len(points) == 24
    assert all(at.minute == 0 and at.second == 0 for at, _p, _l, _u in points)
    # Strictly increasing, one hour apart.
    stamps = [at for at, _p, _l, _u in points]
    assert stamps == sorted(stamps)
    assert all(b - a == timedelta(hours=1) for a, b in pairwise(stamps))


def test_intervals_widen_with_the_horizon() -> None:
    """A 24-hour-ahead forecast is not as trustworthy as a 1-hour-ahead one, and must not look it."""
    rows = history()
    profile = fit(SERVICE, rows)
    assert profile is not None
    points = forecast(profile, rows, start=datetime(2026, 9, 21, 23, 0, tzinfo=UTC), hours=24)

    first_width = points[0][3] - points[0][2]
    last_width = points[-1][3] - points[-1][2]
    assert last_width > first_width


def test_bounds_never_go_negative() -> None:
    rows = history(noise=0.5)
    profile = fit(SERVICE, rows)
    assert profile is not None
    points = forecast(profile, rows, start=datetime(2026, 9, 21, 23, 0, tzinfo=UTC), hours=24)
    # ForecastPoint declares lower_bound ge=0, so a negative bound fails validation at the point
    # of publishing, far from the cause.
    assert all(lower >= 0.0 for _at, _p, lower, _u in points)
    assert all(upper >= lower for _at, _p, lower, upper in points)


def test_forecast_reproduces_the_daily_curve() -> None:
    """The module's first verification criterion: the forecast tracks the known daily curve."""
    rows = history()
    profile = fit(SERVICE, rows)
    assert profile is not None
    points = forecast(profile, rows, start=rows[-1].at, hours=24)

    predicted_peak = max(points, key=lambda p: p[1])
    predicted_trough = min(points, key=lambda p: p[1])
    # The generator peaks at 09:00 and troughs at 21:00; allow an hour either side for the noise
    # and the median smoothing.
    assert abs(predicted_peak[0].hour - 9) <= 1
    assert abs(predicted_trough[0].hour - 21) <= 1


def test_forecast_blends_profile_and_naive_by_lead_time() -> None:
    """The measured shape of the model: a blend whose weight depends on lead time.

    Backtesting showed the blend beating either component alone, and the optimal weight rising with
    the horizon - naive is unbiased but carries the variance of one observation, while the profile
    median is low-variance and only starts to pay off once the level has had time to drift.
    """
    rows = history()
    profile = fit(SERVICE, rows)
    assert profile is not None
    origin = rows[-1].at

    blended = forecast(profile, rows, start=origin, hours=24)
    naive = dict(seasonal_naive(rows, start=origin, hours=24))

    for step, (at, predicted, _lower, _upper) in enumerate(blended, start=1):
        weight = profile_weight_for(step)
        expected = weight * profile.predict_at(at) + (1 - weight) * naive[at]
        assert predicted == pytest.approx(expected)


def test_blend_weight_rises_with_lead_time() -> None:
    """Measured, not chosen: 0.15 at one hour, 0.90 at twenty-four."""
    assert profile_weight_for(1) == pytest.approx(0.15)
    assert profile_weight_for(6) == pytest.approx(0.75)
    assert profile_weight_for(24) == pytest.approx(0.90)

    weights = [profile_weight_for(h) for h in range(1, 25)]
    assert weights == sorted(weights), "weight must be monotone in lead time"
    # Interpolated between breakpoints rather than stepping.
    assert 0.15 < profile_weight_for(3) < 0.75


def test_blend_weight_is_clamped_outside_the_measured_range() -> None:
    # Lead times beyond the measured breakpoints must not extrapolate to a weight above 1.0, which
    # would invert the blend and subtract the baseline.
    assert profile_weight_for(0) == pytest.approx(0.15)
    assert profile_weight_for(200) == pytest.approx(0.90)
    assert all(0.0 <= profile_weight_for(h) <= 1.0 for h in range(0, 400))


def test_forecast_falls_back_to_the_profile_when_naive_has_no_value() -> None:
    """A service younger than a week, or a gap in history, must not drag the forecast to zero."""
    rows = history(days=14)
    profile = fit(SERVICE, rows)
    assert profile is not None
    # Empty history for the naive component, so no point can resolve a week back.
    points = forecast(profile, [], start=rows[-1].at, hours=3)

    for at, predicted, _lower, _upper in points:
        assert predicted == pytest.approx(profile.predict_at(at))
        assert predicted > 0


def test_seasonal_naive_looks_back_a_week() -> None:
    rows = history()
    points = seasonal_naive(rows, start=rows[-1].at, hours=6)
    lookup = {obs.at: obs.tps for obs in rows}

    for at, value in points:
        assert value == pytest.approx(lookup[at - timedelta(hours=HOURS_PER_WEEK)])


def test_seasonal_naive_walks_back_further_when_needed() -> None:
    # A horizon beyond one week must still resolve rather than silently returning zero.
    rows = history()
    points = seasonal_naive(rows, start=rows[-1].at, hours=HOURS_PER_WEEK + 12)
    assert all(value > 0 for _at, value in points)


# ---------------------------------------------------------------------------
# Backtesting - the module's headline claim
# ---------------------------------------------------------------------------
def test_model_beats_seasonal_naive_on_all_horizons() -> None:
    """The criterion module 5 is judged on.

    The profile model and the baseline capture the same daily and weekly shape; the difference is
    that the baseline carries the variance of a single observation while the model takes a median
    over four weeks. That variance reduction is the entire claimed improvement, which is what makes
    this a fair comparison rather than a straw man.
    """
    results = evaluate(SERVICE, history(), [1, 6, 24])

    assert set(results) == {1, 6, 24}
    for horizon, accuracy in results.items():
        assert accuracy.model_mape is not None, f"{horizon}h produced no MAPE"
        assert accuracy.baseline_mape is not None
        assert accuracy.beats_baseline, (
            f"{horizon}h: model {accuracy.model_mape:.4f} did not beat baseline "
            f"{accuracy.baseline_mape:.4f}"
        )
        assert accuracy.improvement is not None and accuracy.improvement > 0


def test_backtest_uses_several_origins() -> None:
    # One hold-out gives a number that depends on which slice happened to be last: cut on a quiet
    # Sunday and anything looks accurate.
    results = evaluate(SERVICE, history(), [6])
    assert results[6].origins > 1


def test_backtest_refits_per_origin_and_does_not_leak() -> None:
    """A profile fitted on the whole series would leak the hold-out into the factors.

    That is the most common way a backtest flatters itself, and it shows up as an accuracy the model
    cannot reproduce in production. With noise this high a leaking backtest reports a near-zero
    MAPE, so a plausible non-zero error is the evidence that refitting happens per origin.
    """
    results = evaluate(SERVICE, history(noise=0.25), [6])
    assert results[6].model_mape is not None
    assert results[6].model_mape > 0.01


def test_mape_skips_near_zero_actuals_and_reports_it() -> None:
    """Dividing by a near-zero trough turns a trivial absolute miss into a huge percentage."""
    rows = history()
    for index in range(-30, -10):
        rows[index] = Observation(at=rows[index].at, tps=MIN_ACTUAL_TPS / 10)
    results = evaluate(SERVICE, rows, [24])
    # The exclusion has to be visible, not a silent improvement to the score.
    assert results[24].points_skipped > 0


def test_evaluate_on_empty_history_is_harmless() -> None:
    assert evaluate(SERVICE, [], [1, 6, 24]) == {}


def test_insufficient_history_yields_no_mape_rather_than_a_wrong_one() -> None:
    results = evaluate(SERVICE, history(days=8), [24])
    # fit() refuses fewer than two weekly cycles, so there is nothing to score - and reporting a
    # confident MAPE from an unfittable model would be worse than reporting none.
    assert results[24].model_mape is None


# ---------------------------------------------------------------------------
# Headroom and scaling - where a forecast becomes an action
# ---------------------------------------------------------------------------
def test_headroom_is_driven_by_measured_accuracy() -> None:
    # Provisioning for exactly the predicted peak means being under-provisioned whenever the
    # forecast errs high, which is half the time by construction.
    assert headroom_for(0.40, floor=0.25) == pytest.approx(0.40)
    assert headroom_for(0.05, floor=0.25) == pytest.approx(0.25)
    assert headroom_for(None, floor=0.25) == pytest.approx(0.25)


def test_headroom_is_capped() -> None:
    # A wildly inaccurate model should not be able to demand unbounded over-provisioning.
    assert headroom_for(9.0, floor=0.25) == pytest.approx(1.0)


def test_recommendation_rounds_up() -> None:
    """The asymmetry is the whole argument.

    Module 2 measured a load factor of 1.26 producing 100% errors and a 56-second p99, against 0.2%
    errors at 8 replicas. One replica too many costs a little CPU; one too few costs the platform.
    """
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=4, measured=True)
    # 46 tps with 25% headroom is 57.5, over 15 per replica is 3.83 -> 4.
    result = recommend(46.0, capacity, headroom=0.25)
    assert result.recommended_replicas == 4


def test_predicted_peak_produces_scale_out() -> None:
    """The module's third criterion: a predicted peak recommends scaling ahead of the spike."""
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=2, measured=True)
    result = recommend(80.0, capacity, headroom=0.25)

    assert result.recommended_replicas > capacity.current_replicas
    assert result.action == "scale_out"


def test_falling_demand_produces_scale_in() -> None:
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=8, measured=True)
    result = recommend(20.0, capacity, headroom=0.25)
    assert result.action == "scale_in"
    assert result.recommended_replicas < 8


def test_matched_capacity_holds() -> None:
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=4, measured=True)
    assert recommend(46.0, capacity, headroom=0.25).action == "hold"


def test_recommendation_is_bounded() -> None:
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=4, measured=True)
    # A forecast corrupted by bad data must not be able to ask for a hundred containers.
    assert recommend(1_000_000.0, capacity, headroom=0.25).recommended_replicas == MAX_REPLICAS
    # And zero predicted traffic still needs something running.
    assert recommend(0.0, capacity, headroom=0.25).recommended_replicas == MIN_REPLICAS


def test_fallback_capacity_matches_the_schema_default() -> None:
    # PredictionEvent.tps_per_replica defaults to 50.0; two different fallbacks would mean a
    # recommendation computed here disagreed with the one the schema describes.
    assert fallback_capacity(SERVICE).tps_per_replica == DEFAULT_TPS_PER_REPLICA
    assert fallback_capacity(SERVICE).measured is False


def test_zero_capacity_does_not_divide_by_zero() -> None:
    capacity = ServiceCapacity(SERVICE, tps_per_replica=0.0, current_replicas=1, measured=False)
    result = recommend(10.0, capacity, headroom=0.25)
    assert result.recommended_replicas == MAX_REPLICAS


def test_higher_headroom_never_recommends_fewer_replicas() -> None:
    capacity = ServiceCapacity(SERVICE, tps_per_replica=15.0, current_replicas=2, measured=True)
    counts = [
        recommend(60.0, capacity, headroom=h).recommended_replicas
        for h in (0.1, 0.25, 0.5, 0.9)
    ]
    assert counts == sorted(counts)
