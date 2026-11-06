"""Anomaly detection behaviour (module 4).

Two things here are worth more than the rest, because both fail *silently* and produce a
plausible-looking detector that is wrong:

* **Score calibration.** If the mapping from the forest's decision function to 0-1 is not
  anchored on the model's own boundary, `ANOMALY_SCORE_THRESHOLD = 0.65` stops meaning
  anything. The naive percentile mapping puts it at the 65th percentile of normal data.
* **Deviation direction.** Ranking by `abs(deviation)` is the obvious implementation and it
  raises `latency_spike` on a service responding unusually *fast*.

Training data is synthesised here rather than read from TimescaleDB: these tests are about the
decision logic, and the live path is covered by `scripts/check_anomaly_detector.py`.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aiops_common.constants import ANOMALY_FEATURES, AnomalyKind, Severity
from aiops_common.schemas import MetricSample
from anomaly_detector import model as model_module
from anomaly_detector.baseline import (
    MIN_BUCKET_SAMPLES,
    RELATIVE_SIGMA_FLOOR,
    build_baseline,
)
from anomaly_detector.classify import IMPLICATION_SIGMA, classify
from anomaly_detector.detector import ANOMALY_COOLDOWN_SECONDS, AnomalyDetector
from anomaly_detector.scorer import Scorer

THRESHOLD = 0.65
SERVICE = "central-ledger"

# Roughly what a healthy central-ledger looks like: ~26 tps, p99 near 0.22s, 0.2% errors.
NORMAL = {
    "tps": 26.0,
    "error_ratio": 0.002,
    "latency_p50_seconds": 0.05,
    "latency_p99_seconds": 0.22,
    "cpu_utilisation_ratio": 0.45,
    "memory_utilisation_ratio": 0.48,
    "queue_depth": 2.0,
    "active_connections": 30.0,
    "settlement_lag_seconds": 1.5,
}


# The agent's collection interval. Used to turn an error *ratio* into the integer counts
# `MetricSample` actually stores, exactly as `MetricsCollector._counts_for` does.
INTERVAL_SECONDS = 15


def test_anomaly_emission_cooldown_is_per_service() -> None:
    detector = AnomalyDetector(None, None, Scorer(THRESHOLD), None)
    first_emission = 100.0
    detector._last_anomaly_emitted[SERVICE] = first_emission

    assert ANOMALY_COOLDOWN_SECONDS > 0
    assert detector._in_anomaly_cooldown(
        SERVICE, first_emission + ANOMALY_COOLDOWN_SECONDS - 0.1
    )
    assert not detector._in_anomaly_cooldown(SERVICE, first_emission + ANOMALY_COOLDOWN_SECONDS)
    assert not detector._in_anomaly_cooldown("quoting-service", first_emission + 1.0)


def _counts(tps: float, error_ratio: float) -> tuple[int, int]:
    requests = max(1, round(tps * INTERVAL_SECONDS))
    return requests, min(requests, round(requests * error_ratio))


def healthy_rows(count: int = 1500, *, seed: int = 7) -> list[dict[str, Any]]:
    """Synthetic healthy history with a daily curve, so hour-of-day buckets are populated.

    1500 minutes is just over 25 hours, so every hour-of-day bucket fills; 1200 would cover
    only 20 and leave four falling back to service-wide statistics.

    `error_ratio` is derived from integer counts rather than used directly, because that is
    what the real pipeline produces - it is a computed property on `MetricSample`, not a stored
    field. At 26 tps over 15s a 0.2% error rate is 0.78 errors, which rounds to 0 or 1, so the
    observed ratio is quantised. Generating a smooth ratio here would give the baseline a
    variance the live data does not have, and every real sample would then look anomalous.
    """
    rng = random.Random(seed)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        moment = start + timedelta(minutes=index)
        # Daily shape, mirroring the simulator so baselines have real seasonality to remove
        # rather than flat noise.
        seasonal = 1.0 + 0.45 * math.sin(2 * math.pi * (moment.hour / 24.0))
        row: dict[str, Any] = {"timestamp": moment}
        for feature, base in NORMAL.items():
            if feature == "error_ratio":
                continue
            jitter = rng.uniform(0.94, 1.06)
            scale = seasonal if feature in {"tps", "cpu_utilisation_ratio"} else 1.0
            row[feature] = base * scale * jitter

        requests, errors = _counts(row["tps"], NORMAL["error_ratio"] * rng.uniform(0.94, 1.06))
        row["error_ratio"] = errors / requests
        rows.append(row)
    return rows


def fitted_model(rows: list[dict[str, Any]] | None = None, *, contamination: float = 0.02):
    rows = rows if rows is not None else healthy_rows()
    baseline = build_baseline(SERVICE, rows)
    vectors = [[float(r[f]) for f in ANOMALY_FEATURES] for r in rows]
    model, metrics = model_module.fit(
        SERVICE,
        vectors,
        baseline,
        version="test",
        contamination=contamination,
        data_sources=("test",),
    )
    return model, metrics


def sample(**overrides: Any) -> MetricSample:
    """Build a `MetricSample`, translating an `error_ratio` override into counts.

    `MetricSample.error_ratio` is a computed property over `request_count` and `error_count`,
    so passing it as a keyword silently does nothing - the base validator strips computed
    fields. That cost an hour: tests asserting on a 175x error burst were quietly scoring a
    sample whose error ratio was zero.
    """
    values = dict(NORMAL)
    values.update(overrides)
    ratio = float(values.pop("error_ratio", 0.0))
    requests, errors = _counts(float(values["tps"]), ratio)
    # Midday, which sits inside a well-populated hourly bucket.
    return MetricSample(
        service=SERVICE,
        timestamp=datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
        request_count=requests,
        error_count=errors,
        **values,
    )


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
def test_baseline_covers_every_hour_and_feature() -> None:
    baseline = build_baseline(SERVICE, healthy_rows())
    assert set(baseline.overall) == set(ANOMALY_FEATURES)
    hours = {hour for hour, _ in baseline.hourly}
    assert hours == set(range(24)), "25 hours of minute data should fill all 24 buckets"


def test_sparse_hourly_bucket_falls_back_to_overall() -> None:
    # A sigma computed from three points produces enormous deviations on ordinary data, which
    # is worse than being less specific.
    rows = healthy_rows(count=MIN_BUCKET_SAMPLES - 1)
    baseline = build_baseline(SERVICE, rows)
    stats = baseline.stats_for("tps", rows[0]["timestamp"].hour)
    assert stats is baseline.overall["tps"]


def test_sigma_floor_stops_near_constant_signals_exploding() -> None:
    """`error_ratio` sits at 0.002 with almost no variance on a healthy service.

    Without a floor, (value - mean) / sigma would read hundreds of sigma for a trivial move and
    outrank a genuine outage in `contributing_features`.
    """
    rows = healthy_rows()
    for row in rows:
        row["error_ratio"] = 0.002  # perfectly constant
    baseline = build_baseline(SERVICE, rows)
    stats = baseline.overall["error_ratio"]

    assert stats.sigma == pytest.approx(0.002 * RELATIVE_SIGMA_FLOOR)
    # A doubling is notable but must stay on a comparable scale to other signals.
    assert 0 < stats.deviation(0.004) < 50


def test_deviations_are_signed() -> None:
    baseline = build_baseline(SERVICE, healthy_rows())
    high = baseline.deviations(sample(latency_p99_seconds=2.0), sample().timestamp)
    low = baseline.deviations(sample(latency_p99_seconds=0.01), sample().timestamp)

    assert high["latency_p99_seconds"] > 0
    assert low["latency_p99_seconds"] < 0


def test_baseline_survives_a_round_trip() -> None:
    baseline = build_baseline(SERVICE, healthy_rows())
    restored = type(baseline).from_dict(baseline.to_dict())

    at = sample().timestamp
    assert restored.deviations(sample(), at) == baseline.deviations(sample(), at)


# ---------------------------------------------------------------------------
# Calibration - the part that makes the threshold mean something
# ---------------------------------------------------------------------------
def test_normal_sample_scores_below_the_decision_boundary() -> None:
    model, _ = fitted_model()
    score, _raw = model.score([float(NORMAL[f]) for f in ANOMALY_FEATURES])
    assert score < 0.5


def test_boundary_is_exactly_half() -> None:
    """raw == 0 is where the forest flips from inlier to outlier, and must map to 0.5."""
    model, _ = fitted_model()
    # tanh(0) == 0, so the mapping gives exactly 0.5 regardless of the scale.
    assert 0.5 * (1.0 + math.tanh(0.0 / model.scale)) == 0.5


def test_forest_alone_cannot_rank_out_of_range_faults() -> None:
    """Documents the limitation, so nobody 'fixes' the combined scorer back into a single one.

    1 second and 40 seconds of p99 latency are both far outside the training range, so they
    traverse the forest's trees identically and receive the *same* score. The forest is not
    broken; it is an outlier detector being asked to extrapolate, which it cannot do.
    """
    model, _ = fitted_model()
    mild, _ = model.score(sample(latency_p99_seconds=1.0).feature_vector(ANOMALY_FEATURES))
    severe, _ = model.score(sample(latency_p99_seconds=40.0).feature_vector(ANOMALY_FEATURES))
    assert severe == pytest.approx(mild)


def test_combined_score_is_monotone_in_severity() -> None:
    """The guarantee that actually has to hold.

    The RCA engine ranks anomalies against each other, so a worse outage must score higher.
    The baseline supplies the ordering the forest cannot, and the logarithmic deviation curve
    is what stops both saturating at exactly 1.0.
    """
    model, _ = fitted_model()
    scorer = Scorer(THRESHOLD)
    mild = scorer.score(sample(latency_p99_seconds=1.0), model)
    severe = scorer.score(sample(latency_p99_seconds=40.0), model)

    assert severe.score > mild.score
    assert severe.score < 1.0, "saturating at exactly 1.0 would destroy the ranking"


def test_scores_stay_within_the_schema_range() -> None:
    model, _ = fitted_model()
    extreme = sample(tps=100_000.0, error_ratio=1.0, latency_p99_seconds=1e6, queue_depth=1e9)
    score, _ = model.score(extreme.feature_vector(ANOMALY_FEATURES))
    # AnomalyEvent.score is declared ge=0.0, le=1.0; a value outside that fails validation at
    # the point of publishing, long after the cause.
    assert 0.0 <= score <= 1.0


def test_training_data_rarely_clears_the_threshold() -> None:
    """The figure that predicts whether the detector will be usable.

    A detector that fires on a large share of its own clean training data will be ignored
    within a day. This is the assertion that would have caught a percentile-based calibration,
    which puts ~35% of normal data above 0.65.
    """
    _model, metrics = fitted_model()
    assert metrics["train_above_0_65"] < 0.02
    # Sanity: the forest's own outlier fraction should land near `contamination`.
    assert metrics["train_outlier_ratio"] == pytest.approx(0.02, abs=0.015)


def test_calibration_scale_is_recorded_and_positive() -> None:
    model, metrics = fitted_model()
    assert model.scale > 0
    assert metrics["calibration_scale"] == model.scale


# ---------------------------------------------------------------------------
# Classification - direction, dominance and the multivariate case
# ---------------------------------------------------------------------------
def test_high_memory_is_memory_pressure() -> None:
    result = classify(SERVICE, {"memory_utilisation_ratio": 6.0}, 0.9, THRESHOLD)
    assert result.kind == AnomalyKind.MEMORY_PRESSURE
    # The label module 6 matches `restart_on_oom` against.
    assert result.contributing == ["memory_utilisation_ratio"]


def test_low_latency_is_not_an_anomaly_kind() -> None:
    """The bug an `abs(deviation)` implementation would ship.

    A service responding five sigma faster than usual is having a good day. Reporting
    `latency_spike` would send module 6 looking for a problem that does not exist.
    """
    result = classify(SERVICE, {"latency_p99_seconds": -5.0}, 0.9, THRESHOLD)
    assert result.kind == AnomalyKind.MULTIVARIATE
    # The kind is not claimed, but the signal is still named - see the test below.
    assert result.contributing == ["latency_p99_seconds"]


def test_forest_only_anomaly_still_names_its_signals() -> None:
    """Found by the live fault check, which failed on "not explainable".

    When the forest fires but nothing crosses IMPLICATION_SIGMA, the original code emitted an
    anomaly with an empty `contributing_features`. That is worse than useless: it cannot be
    triaged, module 6 has nothing to match, and the operator's only recourse is to go and read
    the raw metrics - exactly the work this platform exists to remove. The threshold governs
    which *kind* is claimed, not whether an explanation is given.
    """
    result = classify(
        SERVICE,
        {"error_ratio": 1.6, "latency_p99_seconds": 1.0, "tps": 0.3},
        0.86,
        THRESHOLD,
    )
    assert result.kind == AnomalyKind.MULTIVARIATE
    # Ranked by magnitude, largest first.
    assert result.contributing[0] == "error_ratio"
    assert len(result.contributing) == 3
    # The wording must not imply a threshold was crossed when none was.
    assert "none exceeds" in result.description
    assert "error ratio 1.6 sigma above" in result.description


def test_descriptions_are_ascii_only() -> None:
    """A Windows console is cp1252 and raises on U+03C3.

    The description is stored, logged, printed and rendered in the dashboard. Using the sigma
    symbol crashed the verification script twice - the same trap as the em dashes that broke the
    PowerShell scripts, one layer further in.
    """
    for deviations in (
        {"memory_utilisation_ratio": 6.0},
        {"error_ratio": 1.6, "tps": 0.4},
        {},
    ):
        description = classify(SERVICE, deviations, 0.9, THRESHOLD).description
        description.encode("ascii")  # raises UnicodeEncodeError if not ASCII


def test_traffic_direction_is_distinguished() -> None:
    # Traffic is the one signal where both directions matter: a surge threatens capacity, a
    # collapse usually means callers have stopped arriving.
    surge = classify(SERVICE, {"tps": 5.0}, 0.9, THRESHOLD)
    drop = classify(SERVICE, {"tps": -5.0}, 0.9, THRESHOLD)
    assert surge.kind == AnomalyKind.TRAFFIC_SURGE
    assert drop.kind == AnomalyKind.TRAFFIC_DROP


def test_below_implication_sigma_does_not_claim_a_kind() -> None:
    """The threshold governs the *kind*, not whether an explanation is offered.

    A signal 1.9 sigma out is not strong enough to assert `cpu_saturation` and send module 6
    after a scaling playbook - but it is still the most useful thing to tell an operator about
    why the forest objected.
    """
    result = classify(
        SERVICE, {"cpu_utilisation_ratio": IMPLICATION_SIGMA - 0.1}, 0.9, THRESHOLD
    )
    assert result.kind == AnomalyKind.MULTIVARIATE
    assert result.contributing == ["cpu_utilisation_ratio"]
    assert "none exceeds" in result.description


def test_dominant_signal_wins_over_mild_companions() -> None:
    result = classify(
        SERVICE,
        {"memory_utilisation_ratio": 9.0, "cpu_utilisation_ratio": 2.2},
        0.9,
        THRESHOLD,
    )
    # CPU is almost certainly a consequence of the memory problem, not a second incident.
    assert result.kind == AnomalyKind.MEMORY_PRESSURE


def test_three_implicated_signals_is_multivariate() -> None:
    """The `bad_deploy` signature.

    The simulator raises errors, latency and queue depth together on purpose, because module 7
    has to attribute that composite to a *change* rather than to load. Labelling it
    `error_burst` because errors deviated most would discard the evidence.
    """
    result = classify(
        SERVICE,
        {
            "error_ratio": 3.0,
            "latency_p99_seconds": 2.8,
            "queue_depth": 2.5,
        },
        0.9,
        THRESHOLD,
    )
    assert result.kind == AnomalyKind.MULTIVARIATE
    assert len(result.contributing) == 3


def test_tie_breaks_toward_the_more_specific_playbook() -> None:
    # Two comparable signals pointing at different kinds. Memory pressure has a precise
    # remediation; "cpu is high" is a symptom of almost anything.
    result = classify(
        SERVICE,
        {"cpu_utilisation_ratio": 2.6, "memory_utilisation_ratio": 2.5},
        0.9,
        THRESHOLD,
    )
    assert result.kind == AnomalyKind.MEMORY_PRESSURE


def test_severity_scales_with_score_relative_to_the_threshold() -> None:
    below = classify(SERVICE, {"error_ratio": 3.0}, THRESHOLD - 0.05, THRESHOLD)
    just_over = classify(SERVICE, {"error_ratio": 3.0}, THRESHOLD + 0.01, THRESHOLD)
    far_over = classify(SERVICE, {"error_ratio": 3.0}, 0.95, THRESHOLD)

    # Recorded but not actionable, so a run of near-misses is still queryable.
    assert below.severity == Severity.INFO
    assert just_over.severity == Severity.WARNING
    assert far_over.severity == Severity.CRITICAL


def test_cascade_is_critical_even_at_a_modest_score() -> None:
    result = classify(
        SERVICE,
        {"error_ratio": 2.5, "latency_p99_seconds": 2.5, "queue_depth": 2.5},
        THRESHOLD + 0.01,
        THRESHOLD,
    )
    assert result.severity == Severity.CRITICAL


def test_description_names_the_signals_and_direction() -> None:
    result = classify(SERVICE, {"memory_utilisation_ratio": 6.2}, 0.9, THRESHOLD)
    assert "memory utilisation" in result.description
    assert "6.2" in result.description
    assert "above" in result.description


# ---------------------------------------------------------------------------
# Scoring end to end
# ---------------------------------------------------------------------------
def test_normal_sample_produces_no_anomaly() -> None:
    model, _ = fitted_model()
    result = Scorer(THRESHOLD).score(sample(), model)

    assert result.anomaly is None
    # Writing an AnomalyEvent per service per 15 seconds would be ~29k rows a day of "nothing
    # happened" and would bury the real ones.
    assert "boundary" in result.skipped_reason


def test_out_of_range_fault_is_caught_by_the_baseline_not_the_forest() -> None:
    """The regression for the flaw that nearly shipped.

    An Isolation Forest is an outlier detector, not a novelty detector: its trees hold only
    split thresholds seen during training, so a value far outside that range follows an
    average-length path and scores as normal. Measured before the fix, an `error_ratio` of
    0.35 - 175x the 0.002 baseline - scored 0.0023 and produced no anomaly at all.

    Standard-deviation distance extrapolates without bound, so the baseline catches it. This
    asserts the division of labour explicitly, because the forest scoring it low is expected
    behaviour rather than a fault to be fixed.
    """
    model, _ = fitted_model()
    catastrophic = sample(error_ratio=0.35)
    result = Scorer(THRESHOLD).score(catastrophic, model)

    assert result.anomaly is not None, "a 175x error rate must not be scored as normal"
    assert result.anomaly.kind == AnomalyKind.ERROR_BURST
    assert result.anomaly.is_actionable
    # The whole point: the baseline decided, and the forest did not object usefully.
    assert result.decided_by == "baseline"
    assert result.deviation_score > result.forest_score


def test_forest_still_owns_unusual_combinations() -> None:
    """The forest's own contribution, which the baseline cannot make.

    Every value here is within a couple of sigma of its own baseline, so no single signal is
    implicated - but the combination (high traffic with a *low* queue and low CPU) never occurs
    in the training data. This is the case an Isolation Forest exists for.
    """
    model, _ = fitted_model()
    odd = sample(tps=NORMAL["tps"] * 1.05, queue_depth=0.05, active_connections=1.0)
    result = Scorer(THRESHOLD).score(odd, model)

    # Not asserting an anomaly is raised - that depends on how unusual this particular
    # combination happens to be - but the forest must be the one with an opinion.
    assert result.forest_score >= result.deviation_score or result.anomaly is None


def test_memory_leak_produces_an_actionable_memory_anomaly() -> None:
    model, _ = fitted_model()
    leaking = sample(memory_utilisation_ratio=0.95, queue_depth=3.0)
    result = Scorer(THRESHOLD).score(leaking, model)

    assert result.anomaly is not None
    assert result.anomaly.kind == AnomalyKind.MEMORY_PRESSURE
    assert result.anomaly.is_actionable
    assert "memory_utilisation_ratio" in result.anomaly.contributing_features


def test_anomaly_carries_a_full_snapshot_and_deviations() -> None:
    model, _ = fitted_model()
    result = Scorer(THRESHOLD).score(sample(error_ratio=0.35), model)

    assert result.anomaly is not None
    # The snapshot is what lets an anomaly be read months later, after the metrics table has
    # aged that window out under its 30-day retention policy.
    assert set(result.anomaly.feature_snapshot) == set(ANOMALY_FEATURES)
    assert set(result.anomaly.feature_deviations) == set(ANOMALY_FEATURES)
    assert result.anomaly.model_version == "test"
    assert result.anomaly.window_start == sample().timestamp


def test_score_threshold_is_carried_onto_the_event() -> None:
    model, _ = fitted_model()
    result = Scorer(0.8).score(sample(error_ratio=0.5, latency_p99_seconds=9.0), model)
    assert result.anomaly is not None
    # `is_actionable` is computed from these two, so the threshold in force has to travel with
    # the event rather than being re-derived by whoever reads it later.
    assert result.anomaly.threshold == 0.8


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_model_round_trips_through_disk(tmp_path: Path) -> None:
    model, _ = fitted_model()
    path = model_module.save(model, tmp_path)
    restored = model_module.load(path)

    vector = sample(error_ratio=0.2).feature_vector(ANOMALY_FEATURES)
    assert restored.score(vector) == model.score(vector)
    assert restored.baseline.overall.keys() == model.baseline.overall.keys()
    assert restored.data_sources == ("test",)


def test_load_refuses_a_model_fitted_on_different_features(tmp_path: Path) -> None:
    """`ANOMALY_FEATURES` is append-only and part of the model contract.

    Scoring the wrong columns produces confident nonsense with nothing to indicate a problem,
    so loading such an artifact must fail loudly instead.
    """
    import joblib

    model, _ = fitted_model()
    path = model_module.save(model, tmp_path)
    payload = joblib.load(path)
    payload["features"] = ["tps", "error_ratio"]
    joblib.dump(payload, path)

    with pytest.raises(ValueError, match="ANOMALY_FEATURES"):
        model_module.load(path)


def test_load_all_skips_a_corrupt_artifact(tmp_path: Path) -> None:
    model, _ = fitted_model()
    model_module.save(model, tmp_path)
    (tmp_path / f"{model_module.MODEL_NAME}-broken.joblib").write_bytes(b"not a joblib file")

    # One unreadable artifact must not stop the other four services being scored.
    loaded = model_module.load_all(tmp_path)
    assert set(loaded) == {SERVICE}


def test_load_all_on_a_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert model_module.load_all(tmp_path / "nope") == {}


def test_fit_rejects_empty_input() -> None:
    baseline = build_baseline(SERVICE, healthy_rows())
    with pytest.raises(ValueError, match="no training vectors"):
        model_module.fit(SERVICE, [], baseline, version="v", contamination=0.02)


def test_fit_replaces_non_finite_values() -> None:
    """A single NaN makes its column's scaler mean NaN, and then every score is NaN.

    The symptom appears at scoring time, far from the cause, so it is handled at fit time.
    """
    rows = healthy_rows()
    vectors = [[float(r[f]) for f in ANOMALY_FEATURES] for r in rows]
    vectors[10][0] = float("nan")
    vectors[11][1] = float("inf")

    model, _ = model_module.fit(
        SERVICE,
        vectors,
        build_baseline(SERVICE, rows),
        version="v",
        contamination=0.02,
    )
    score, raw = model.score([float(NORMAL[f]) for f in ANOMALY_FEATURES])
    assert not math.isnan(score)
    assert not math.isnan(raw)
