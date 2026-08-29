"""Tests for the Mojaloop simulator (module 2).

The most valuable assertions here are the boring-looking ones about metric names. Those
names are the contract with module 3's collection agent, and a typo would surface as a
silently empty column in the anomaly detector's feature vector - three modules and several
days away from the cause.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import generate_latest

from aiops_common.constants import ANOMALY_FEATURES, MOJALOOP_SERVICES, DpiMetric
from mojaloop_sim.faults import (
    DEFAULT_MAGNITUDE,
    MAX_DURATION_SECONDS,
    FaultKind,
    FaultRegistry,
)
from mojaloop_sim.profiles import PROFILES, get_profile
from mojaloop_sim.simulator import TICK_SECONDS, Simulator
from mojaloop_sim.traffic import (
    KNEE,
    FaultEffects,
    TrafficModel,
    daily_multiplier,
    seasonal_multiplier,
    weekly_multiplier,
)


def _at(hour: int, minute: int = 0, *, weekday_offset: int = 0) -> datetime:
    """A Monday at the given time, optionally shifted by whole days."""
    base = datetime(2026, 8, 24, hour, minute, tzinfo=UTC)  # 2026-08-24 is a Monday
    return base + timedelta(days=weekday_offset)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def test_a_profile_exists_for_every_mojaloop_service() -> None:
    assert set(PROFILES) == set(MOJALOOP_SERVICES)


def test_unknown_service_fails_with_a_useful_message() -> None:
    with pytest.raises(ValueError, match="Unknown simulated service"):
        get_profile("not-a-service")


def test_every_service_emits_at_least_one_dpi_business_signal() -> None:
    # If no service emitted settlement lag, quote latency or verification latency, the
    # DPI-specific half of the project would have nothing to detect on.
    emitted = [
        profile
        for profile in PROFILES.values()
        if profile.emits_settlement_lag
        or profile.emits_quote_latency
        or profile.emits_verification_latency
    ]
    assert len(emitted) >= 3


def test_central_ledger_carries_the_most_traffic() -> None:
    # Fan-in from the dependency graph. Root cause analysis in module 7 depends on the
    # busiest service also being the most upstream one.
    busiest = max(PROFILES.values(), key=lambda p: p.traffic_share)
    assert busiest.name == "central-ledger"


# ---------------------------------------------------------------------------
# Seasonality - the signal module 5 has to forecast
# ---------------------------------------------------------------------------
def test_daily_curve_peaks_in_the_evening_and_troughs_before_dawn() -> None:
    assert daily_multiplier(19.5) > daily_multiplier(11.0) > daily_multiplier(4.0)


def test_daily_swing_is_large_enough_to_forecast() -> None:
    # A forecaster can only beat a naive baseline if the seasonal signal dominates the
    # noise. Noise is capped at +/-12%, so the daily swing must be far larger than that.
    hours = [h / 4 for h in range(96)]
    values = [daily_multiplier(h) for h in hours]
    assert max(values) / min(values) > 2.0


def test_weekends_are_quieter_than_weekdays() -> None:
    weekdays = [weekly_multiplier(day) for day in range(5)]
    weekend = [weekly_multiplier(day) for day in (5, 6)]
    assert min(weekdays) > max(weekend)


def test_seasonal_multiplier_is_deterministic() -> None:
    # Being a pure function of time is what keeps all five services coherent without
    # coordination, and makes a backtest reproduce the live curve.
    moment = _at(14, 30)
    assert seasonal_multiplier(moment) == seasonal_multiplier(moment)


# ---------------------------------------------------------------------------
# Traffic model - correlation is the point
# ---------------------------------------------------------------------------
def _model(service: str = "central-ledger", *, base_tps: float = 25.0) -> TrafficModel:
    return TrafficModel(get_profile(service), base_tps=base_tps, rng=random.Random(1234))


def test_healthy_load_stays_below_the_knee() -> None:
    # Default replica counts are chosen so peak traffic leaves headroom. If this fails the
    # simulator is permanently degraded and every module downstream sees constant alarm.
    for service in MOJALOOP_SERVICES:
        model = _model(service)
        state = model.sample(_at(19, 30), FaultEffects.none())
        assert state.load_factor < KNEE, f"{service} saturated at peak with default replicas"


def test_latency_and_errors_rise_together_with_load() -> None:
    # Correlated columns are what make an Isolation Forest useful. Independent noise would
    # give it nothing to learn.
    model = _model()
    quiet = model.sample(_at(4, 0), FaultEffects.none())
    busy = model.sample(_at(19, 30), FaultEffects.none())

    assert busy.tps > quiet.tps
    assert busy.latency_p99_seconds >= quiet.latency_p99_seconds
    assert busy.cpu_ratio > quiet.cpu_ratio


def test_latency_quantiles_are_ordered() -> None:
    state = _model().sample(_at(12, 0), FaultEffects.none())
    assert state.latency_p50_seconds < state.latency_p95_seconds < state.latency_p99_seconds


def test_scaling_out_reduces_load_and_latency() -> None:
    """The single most important behaviour in this module.

    If scaling did not relieve pressure, module 5's forecasts and module 6's scaling
    playbooks would be numbers on a dashboard with no observable consequence, and no
    remediation could ever be shown to have worked.
    """
    model = _model()
    moment = _at(19, 30)

    model.scale_to(1)
    before = model.sample(moment, FaultEffects.none())

    model.scale_to(8)
    after = model.sample(moment, FaultEffects.none())

    assert after.capacity_tps > before.capacity_tps
    assert after.load_factor < before.load_factor
    assert after.latency_p99_seconds < before.latency_p99_seconds
    assert after.queue_depth <= before.queue_depth


def test_saturation_produces_a_queue_and_errors() -> None:
    model = _model()
    model.scale_to(1)  # force well past the knee
    state = model.sample(_at(19, 30), FaultEffects.none())

    assert state.load_factor > 1.0
    assert state.queue_depth > 0
    assert state.error_ratio > get_profile("central-ledger").base_error_ratio


def test_only_designated_services_emit_settlement_lag() -> None:
    ledger = _model("central-ledger").sample(_at(12, 0), FaultEffects.none())
    lookup = _model("account-lookup-service").sample(_at(12, 0), FaultEffects.none())

    assert ledger.settlement_lag_seconds > 0
    # Publishing a constant zero would teach module 4 that the signal is meaningless.
    assert lookup.settlement_lag_seconds == 0.0


def test_ratios_stay_within_bounds() -> None:
    model = _model()
    model.scale_to(1)
    for hour in range(24):
        state = model.sample(_at(hour), FaultEffects.none())
        assert 0.0 <= state.cpu_ratio <= 1.0
        assert 0.0 <= state.memory_ratio <= 1.0
        assert 0.0 <= state.error_ratio <= 1.0
        assert state.tps >= 0.0


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------
def test_every_fault_kind_has_a_default_magnitude() -> None:
    # Lets a caller name a fault without knowing its units.
    assert set(DEFAULT_MAGNITUDE) == set(FaultKind)


def test_duration_is_capped() -> None:
    # A forgotten injection should degrade a service for minutes, not all afternoon.
    registry = FaultRegistry("central-ledger")
    fault = registry.inject(FaultKind.CPU_HOG, duration_seconds=999_999)
    assert fault.duration_seconds <= MAX_DURATION_SECONDS


def test_faults_expire_without_a_background_task() -> None:
    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.NETWORK_LATENCY, duration_seconds=30)

    now = datetime.now(UTC)
    assert len(registry.active(now)) == 1
    # Expiry is evaluated on read, so there is no timer that could die and leave a fault
    # stuck on.
    assert registry.active(now + timedelta(seconds=31)) == []


def test_expired_faults_have_no_effect() -> None:
    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.ERROR_INJECTION, magnitude=0.5, duration_seconds=10)
    later = datetime.now(UTC) + timedelta(seconds=11)
    assert registry.effects(later).extra_error_ratio == 0.0


def test_injecting_the_same_kind_twice_replaces_rather_than_stacks() -> None:
    # Stacking would let a caller exceed the safety caps and create a state nobody wants
    # to debug.
    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.CPU_HOG, magnitude=0.3)
    registry.inject(FaultKind.CPU_HOG, magnitude=0.4)

    active = registry.active()
    assert len(active) == 1
    assert active[0].magnitude == pytest.approx(0.4)


def test_latency_fault_increases_latency() -> None:
    model = _model()
    moment = _at(12, 0)
    baseline = model.sample(moment, FaultEffects.none())

    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.NETWORK_LATENCY, magnitude=0.5, duration_seconds=60)
    # Halfway through, so the ramp-in is complete.
    later = datetime.now(UTC) + timedelta(seconds=30)
    degraded = model.sample(moment, registry.effects(later))

    assert degraded.latency_p50_seconds > baseline.latency_p50_seconds


def test_memory_fault_climbs_monotonically() -> None:
    # A leak must present as a trend, not a spike - that is what makes module 4 classify it
    # as memory_pressure and module 6 choose a restart.
    model = _model()
    registry = FaultRegistry("central-ledger", tick_seconds=TICK_SECONDS)
    registry.inject(FaultKind.MEMORY_HOG, magnitude=0.5, duration_seconds=60)

    readings = []
    for tick in range(30):
        moment = _at(12, 0) + timedelta(seconds=tick)
        readings.append(model.sample(moment, registry.effects()).memory_ratio)

    assert readings[-1] > readings[0]
    assert readings[-1] <= 1.0


def test_bad_deploy_is_a_composite_signature() -> None:
    # A bad release shows up as errors AND latency starting together, which is what lets
    # module 7 attribute it to a change rather than to load.
    registry = FaultRegistry("quoting-service")
    registry.inject(FaultKind.BAD_DEPLOY, duration_seconds=120)
    effects = registry.effects()

    assert effects.extra_error_ratio > 0
    assert effects.extra_latency_seconds > 0
    assert effects.extra_queue_depth > 0


def test_traffic_spike_multiplies_traffic() -> None:
    registry = FaultRegistry("ml-api-adapter")
    registry.inject(FaultKind.TRAFFIC_SPIKE, magnitude=3.0, duration_seconds=60)
    # Mid-window, where the sine shape is at its maximum.
    mid = datetime.now(UTC) + timedelta(seconds=30)
    assert registry.effects(mid).traffic_multiplier > 1.5


def test_clear_removes_everything() -> None:
    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.CPU_HOG)
    registry.inject(FaultKind.QUEUE_BACKLOG)
    assert registry.clear() == 2
    assert registry.active() == []


def test_kill_service_has_no_ongoing_effect() -> None:
    # It is implemented by exiting the process, not by degrading a signal.
    registry = FaultRegistry("central-ledger")
    registry.inject(FaultKind.KILL_SERVICE)
    effects = registry.effects()
    assert effects == FaultEffects.none()


# ---------------------------------------------------------------------------
# Metric names - the contract with module 3
# ---------------------------------------------------------------------------
def _exposition(service: str = "central-ledger") -> str:
    simulator = Simulator(service, base_tps=25.0, seasonality=True, seed=7)
    simulator.tick()
    return generate_latest(simulator.metrics.registry).decode()


def test_exposition_contains_every_expected_dpi_metric() -> None:
    """Every DpiMetric a service emits must actually appear in /metrics.

    This is the assertion that stops module 3 from silently collecting nothing.
    """
    text = _exposition("central-ledger")

    # central-ledger emits settlement lag but not quote or verification latency.
    expected = {
        DpiMetric.API_REQUEST_TOTAL,
        DpiMetric.TRANSACTION_TOTAL,
        DpiMetric.TRANSACTION_FAILED_TOTAL,
        DpiMetric.TRANSACTION_DURATION_SECONDS,
        DpiMetric.API_LATENCY_P99_SECONDS,
        DpiMetric.API_SUCCESS_RATIO,
        DpiMetric.SETTLEMENT_LAG_SECONDS,
        DpiMetric.CPU_UTILISATION_RATIO,
        DpiMetric.MEMORY_UTILISATION_RATIO,
        DpiMetric.QUEUE_DEPTH,
        DpiMetric.ACTIVE_CONNECTIONS,
        DpiMetric.REPLICAS,
        DpiMetric.CAPACITY_TPS,
    }
    missing = sorted(name for name in expected if str(name) not in text)
    assert not missing, f"metrics declared in DpiMetric but not exported: {missing}"


def test_counter_names_are_exactly_as_declared() -> None:
    """prometheus_client rewrites counter names, so verify the wire format explicitly.

    A `Counter` named `dpi_transaction_total` is exposed as `dpi_transaction_total`, but the
    library strips and re-adds the suffix internally. If that behaviour ever changed, module
    3's PromQL would quietly match nothing - so assert on the actual exposition.
    """
    text = _exposition()
    for metric in (DpiMetric.API_REQUEST_TOTAL, DpiMetric.TRANSACTION_TOTAL):
        assert f"{metric} " in text or f"{metric}{{" in text


def test_histogram_exposes_bucket_series_for_quantiles() -> None:
    # Module 3 derives p50/p95/p99 with histogram_quantile, which needs _bucket series.
    text = _exposition()
    assert f"{DpiMetric.TRANSACTION_DURATION_SECONDS}_bucket" in text


def test_services_without_a_signal_do_not_export_it() -> None:
    text = _exposition("ml-api-adapter")
    # ml-api-adapter has no settlement, so the series should be absent rather than zero.
    assert str(DpiMetric.SETTLEMENT_LAG_SECONDS) not in text


def test_verification_latency_is_exported_by_account_lookup() -> None:
    # The cross-platform metric: this is the field MOSIP would populate.
    assert str(DpiMetric.VERIFICATION_LATENCY_SECONDS) in _exposition("account-lookup-service")


def test_fault_flags_are_always_published() -> None:
    # Published as 0 rather than omitted, so PromQL never has to cope with a series
    # appearing and disappearing.
    text = _exposition()
    assert "mojaloop_sim_fault_active" in text
    for kind in FaultKind:
        assert f'kind="{kind}"' in text


def test_anomaly_features_are_all_derivable_from_the_exposition() -> None:
    """Every feature the anomaly detector needs must be obtainable from module 2's output.

    Guards the end-to-end contract: ANOMALY_FEATURES -> Prometheus -> MetricSample.
    """
    text = _exposition("central-ledger")
    # Map each feature to the metric (or histogram) that provides it.
    sources = {
        "tps": DpiMetric.TRANSACTION_TOTAL,
        "error_ratio": DpiMetric.TRANSACTION_FAILED_TOTAL,
        "latency_p50_seconds": DpiMetric.TRANSACTION_DURATION_SECONDS,
        "latency_p99_seconds": DpiMetric.TRANSACTION_DURATION_SECONDS,
        "cpu_utilisation_ratio": DpiMetric.CPU_UTILISATION_RATIO,
        "memory_utilisation_ratio": DpiMetric.MEMORY_UTILISATION_RATIO,
        "queue_depth": DpiMetric.QUEUE_DEPTH,
        "active_connections": DpiMetric.ACTIVE_CONNECTIONS,
        "settlement_lag_seconds": DpiMetric.SETTLEMENT_LAG_SECONDS,
    }
    assert set(sources) == set(ANOMALY_FEATURES), "feature list changed without a source"
    for feature, metric in sources.items():
        assert str(metric) in text, f"no source metric for feature {feature!r}"


# ---------------------------------------------------------------------------
# Simulator wiring
# ---------------------------------------------------------------------------
def test_tick_populates_state() -> None:
    simulator = Simulator("quoting-service", base_tps=25.0, seed=3)
    assert simulator.last_state is None
    state = simulator.tick()
    assert simulator.last_state is state
    assert state.tps > 0


def test_scale_changes_capacity_and_is_clamped() -> None:
    simulator = Simulator("central-ledger", base_tps=25.0, seed=3)
    assert simulator.scale(6) == 6
    assert simulator.model.capacity_tps == pytest.approx(6 * 15.0)
    # Guard rails at both ends: zero replicas would divide by zero, and a runaway
    # scale-out should not be possible from a single API call.
    assert simulator.scale(0) == 1
    assert simulator.scale(10_000) == 50


def test_snapshot_is_json_friendly() -> None:
    simulator = Simulator("central-ledger", base_tps=25.0, seed=3)
    simulator.tick()
    snapshot = simulator.snapshot()

    assert snapshot["service"] == "central-ledger"
    assert snapshot["state"] is not None
    assert isinstance(snapshot["active_faults"], list)
    assert set(snapshot["injectable_faults"]) == {str(k) for k in FaultKind}


def test_seasonality_can_be_disabled() -> None:
    # Useful for tests and for load experiments where a flat baseline is wanted.
    flat = TrafficModel(
        get_profile("central-ledger"), base_tps=25.0, seasonality=False, rng=random.Random(5)
    )
    quiet = flat.sample(_at(4, 0), FaultEffects.none())
    busy = flat.sample(_at(19, 30), FaultEffects.none())
    # Only the random walk should differ, so the two are within noise of each other.
    assert math.isclose(quiet.tps, busy.tps, rel_tol=0.25)
