"""The archived training set (docs/DATA.md) and the CSV path of replay_history.py.

Pinned here:
* the committed CSV is complete and fault-free, as the anomaly detector's baseline must be
* the generator is deterministic for a pinned end time
* export -> read round-trips every column with the right types
* loading shifts by whole weeks only, so weekday and hour-of-day survive
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "ml/datasets/mojaloop_replay_28d.csv"
SERVICES = {
    "ml-api-adapter",
    "central-ledger",
    "quoting-service",
    "account-lookup-service",
    "transaction-request-service",
}


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location(
        "replay_history", ROOT / "scripts/replay_history.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_history"] = module
    spec.loader.exec_module(module)
    return module


def test_archived_csv_is_complete_and_fault_free(replay):
    rows = list(replay.read_csv(DATASET))
    assert {r["service"] for r in rows} == SERVICES
    # 28 days: 26 at 15-minute steps plus 2 at 1-minute steps, for each of 5 services.
    assert len(rows) == (26 * 24 * 4 + 2 * 24 * 60) * len(SERVICES)
    assert rows[-1]["timestamp"] - rows[0]["timestamp"] < timedelta(days=28)
    # A clean baseline: no error burst anywhere (the live alert threshold is 1%).
    assert max(r["error_ratio"] for r in rows) < 0.01
    assert all(json.loads(r["labels"])["source"] == "replay" for r in rows)


def test_export_is_reproducible_for_a_pinned_end(replay, tmp_path):
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    rows_a = replay.export_csv(tmp_path / "a.csv", days=1, end=end)
    rows_b = replay.export_csv(tmp_path / "b.csv", days=1, end=end)
    assert rows_a == rows_b == 1 * 24 * 60 * 5  # one day, 1-minute fine tier, five services
    assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()


def test_export_then_read_round_trips(replay, tmp_path):
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    path = tmp_path / "rt.csv"
    replay.export_csv(path, days=1, end=end)
    original = list(
        replay.generate_rows(
            days=1, end=end, base_tps=25.0, seasonality=True, platform="mojaloop"
        )
    )
    loaded = list(replay.read_csv(path))
    assert len(loaded) == len(original)
    for want, got in zip(original[:50], loaded[:50], strict=True):
        for key in replay.METRIC_COLUMNS:
            if key == "labels":
                continue
            assert got[key] == want[key], key
        assert isinstance(got["request_count"], int)
        labels = json.loads(got["labels"])
        assert labels["source"] == "replay"
        assert labels["dataset"] == "rt.csv"


@pytest.mark.parametrize("gap_days", [0, 1, 6, 7, 8, 30])
def test_week_shift_preserves_weekday_and_hour(replay, gap_days):
    last = datetime(2026, 11, 13, 11, 59, tzinfo=UTC)
    target = last + timedelta(days=gap_days, hours=3)
    shift = replay.week_shift(last, target)
    moved = last + shift
    assert shift % timedelta(weeks=1) == timedelta(0)
    assert moved <= target
    assert target - moved < timedelta(weeks=1)
    assert (moved.weekday(), moved.hour, moved.minute) == (
        last.weekday(),
        last.hour,
        last.minute,
    )


# ---------------------------------------------------------------------------
# Training rows must look like what the live pipeline produces
# ---------------------------------------------------------------------------
def test_histogram_quantile_matches_prometheus_interpolation(replay):
    # One lognormal with median 0.1 and sigma 0.3: p99 is truly 0.1 * e^(2.326 * 0.3) ~ 0.201,
    # which falls in the (0.15, 0.25] bucket. Prometheus interpolates linearly inside it.
    import math

    true_p99 = 0.1 * math.exp(2.326 * 0.3)
    est = replay.histogram_quantile(0.99, [0.1], 0.3)
    assert 0.15 < est <= 0.25
    assert est != pytest.approx(true_p99, rel=0.01)  # the bucket estimate is not the true value

    # Interpolation rule, by hand: fraction of the way through the bucket.
    def phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    below = phi(math.log(0.15 / 0.1) / 0.3)
    at = phi(math.log(0.25 / 0.1) / 0.3)
    assert est == pytest.approx(0.15 + 0.10 * (0.99 - below) / (at - below))


def test_histogram_quantile_edges(replay):
    assert replay.histogram_quantile(0.99, [], 0.3) == 0.0
    # Far beyond the last finite bucket: Prometheus reports that bucket's bound.
    assert replay.histogram_quantile(0.99, [500.0], 0.3) == replay.LATENCY_BUCKETS[-1]
    # Inside the first bucket the lower bound is 0.
    assert 0 < replay.histogram_quantile(0.5, [0.002], 0.1) < replay.LATENCY_BUCKETS[0]


def test_replay_rows_use_measured_error_ratio_not_rounded_counts(replay):
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    rows = list(
        replay.generate_rows(
            days=1, end=end, base_tps=25.0, seasonality=True, platform="mojaloop"
        )
    )
    # The live agent now stores the measured ratio, so replay must too: no row may have its
    # ratio collapsed to the error_count / request_count quantisation.
    assert all(r["error_ratio"] > 0 for r in rows)
    assert any(
        r["request_count"]
        and r["error_ratio"] != round(r["error_count"] / r["request_count"], 6)
        for r in rows
    )


def _state(**overrides):
    from types import SimpleNamespace

    base = {
        "tps": 20.0,
        "error_ratio": 0.002,
        "latency_p50_seconds": 0.043,
        "latency_p99_seconds": 0.1225,  # the exact quantile the old replay stored
        "quote_latency_seconds": 0.0,
        "verification_latency_seconds": 0.069,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_replay_latencies_are_bucket_estimates_not_exact_quantiles(replay):
    state = _state()
    out = replay.observed_latencies([state], sigma=0.45)

    assert out["latency_p99_seconds"] == replay.histogram_quantile(0.99, [0.043], 0.45)
    assert out["latency_p99_seconds"] > state.latency_p99_seconds  # interpolation runs high
    assert out["verification_latency_seconds"] == replay.histogram_quantile(
        0.95, [0.069], 0.45 * 0.8
    )
    assert out["quote_latency_seconds"] == 0.0  # service does not emit it


def test_window_mixes_the_medians_it_spans(replay):
    # A drifting median widens the window's distribution: p99 over two medians sits above
    # p99 at their geometric mean.
    import math

    wide = replay.observed_latencies(
        [_state(latency_p50_seconds=0.040), _state(latency_p50_seconds=0.050)], sigma=0.45
    )
    centre = replay.histogram_quantile(0.99, [math.sqrt(0.040 * 0.050)], 0.45)
    assert wide["latency_p99_seconds"] > centre


def test_window_rates_match_prometheus_rate_semantics(replay):
    states = [_state(tps=10.0, error_ratio=0.01), _state(tps=30.0, error_ratio=0.0)]
    tps, ratio = replay.window_rates(states)
    assert tps == pytest.approx(20.0)
    assert ratio == pytest.approx(0.0025)  # failures / transactions, not the mean ratio


def test_gauges_are_instantaneous_while_tps_is_averaged(replay):
    """Regression: on the 15-minute tier tps and connections came from one instant, so
    connections was an exact function of tps in training, while live it scatters (tps is a
    5-minute rate, connections a scrape-time gauge). The detector flagged that scatter."""
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    rows = [
        r
        for r in replay.generate_rows(
            days=3, end=end, base_tps=25.0, seasonality=True, platform="mojaloop"
        )
        if r["service"] == "central-ledger" and json.loads(r["labels"])["step"] == "900"
    ]
    assert rows, "expected coarse-tier rows"
    # connections = 6 * instantaneous_tps / replicas + 4 * replicas; with tps averaged over
    # the window the identity no longer holds exactly.
    off = [
        abs(r["active_connections"] - (6 * r["tps"] / r["replicas"] + 4 * r["replicas"]))
        for r in rows
    ]
    assert max(off) > 0.5


def test_sampled_latencies_scatter_like_a_finite_histogram(replay):
    """Regression: noise-free quantiles made p50 an exact function of traffic in training,
    while live quantiles (a few thousand observations per window) scatter around it."""
    import random
    import statistics

    state = _state(tps=9.0, latency_p50_seconds=0.048, verification_latency_seconds=0.0)
    rng = random.Random(0)
    p50s = [
        replay.observed_latencies([state], sigma=0.45, rng=rng)["latency_p50_seconds"]
        for _ in range(200)
    ]
    expected = replay.histogram_quantile(0.5, [0.048], 0.45)
    assert statistics.pstdev(p50s) > 0  # it scatters
    assert statistics.mean(p50s) == pytest.approx(expected, rel=0.01)  # around the expectation
    assert statistics.pstdev(p50s) < 0.02 * expected  # by a few thousand samples' worth


def test_observations_follow_the_simulator_cap(replay):
    assert replay.observations_in_window(9.4) == 300 * 9
    assert replay.observations_in_window(250.0) == 300 * replay.MAX_OBSERVATIONS_PER_TICK
    assert replay.observations_in_window(0.0) == 300  # at least one per tick
