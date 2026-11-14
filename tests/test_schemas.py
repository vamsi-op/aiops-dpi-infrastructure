"""Data contract behaviour: computed fields, validation, serialisation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aiops_common.constants import (
    ANOMALY_FEATURES,
    MOJALOOP_DEPENDENCIES,
    MOJALOOP_SERVICES,
    ActionStatus,
    AnomalyKind,
    ChaosExperiment,
    PlaybookName,
    RootCauseCategory,
    Severity,
)
from aiops_common.schemas import (
    AnomalyEvent,
    ChaosRun,
    ForecastPoint,
    HealingAction,
    Incident,
    MetricSample,
    PredictionEvent,
    RootCauseHypothesis,
    new_id,
    utc_now,
)


# ---------------------------------------------------------------------------
# MetricSample
# ---------------------------------------------------------------------------
def test_error_ratio_is_derived_from_counts() -> None:
    sample = MetricSample(service="central-ledger", request_count=200, error_count=10)
    assert sample.error_ratio == pytest.approx(0.05)
    assert sample.success_ratio == pytest.approx(0.95)


def test_error_ratio_is_zero_when_no_traffic() -> None:
    # Guards against a divide-by-zero turning into a false "100% errors" alert.
    assert MetricSample(service="quoting-service").error_ratio == 0.0


def test_measured_error_ratio_takes_precedence_over_counts() -> None:
    # 300 requests, 0 errors by count - but the measured ratio is 0.15%.
    sample = MetricSample(
        service="ml-api-adapter", request_count=300, error_count=0, measured_error_ratio=0.0015
    )
    assert sample.error_ratio == pytest.approx(0.0015)
    assert sample.success_ratio == pytest.approx(0.9985)


def test_measured_error_ratio_must_be_a_fraction() -> None:
    with pytest.raises(ValidationError):
        MetricSample(service="central-ledger", measured_error_ratio=1.5)


def test_error_ratio_is_capped_at_one() -> None:
    sample = MetricSample(service="ml-api-adapter", request_count=5, error_count=50)
    assert sample.error_ratio == 1.0


def test_feature_vector_matches_the_model_contract() -> None:
    sample = MetricSample(
        service="central-ledger",
        tps=120.0,
        request_count=1000,
        error_count=20,
        latency_p50_seconds=0.08,
        latency_p99_seconds=0.9,
        cpu_utilisation_ratio=0.71,
        memory_utilisation_ratio=0.83,
        queue_depth=14,
        active_connections=95,
        settlement_lag_seconds=3.2,
    )
    vector = sample.feature_vector(ANOMALY_FEATURES)

    assert len(vector) == len(ANOMALY_FEATURES)
    assert vector[ANOMALY_FEATURES.index("tps")] == pytest.approx(120.0)
    assert vector[ANOMALY_FEATURES.index("error_ratio")] == pytest.approx(0.02)
    assert vector[ANOMALY_FEATURES.index("memory_utilisation_ratio")] == pytest.approx(0.83)
    assert all(isinstance(value, float) for value in vector)


def test_negative_metrics_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricSample(service="central-ledger", tps=-1.0)


def test_unknown_fields_are_rejected() -> None:
    # extra="forbid" catches typos in event payloads at the boundary instead of
    # silently dropping data.
    with pytest.raises(ValidationError):
        MetricSample(service="central-ledger", tpss=10)  # type: ignore[call-arg]


def test_timestamps_are_timezone_aware() -> None:
    assert MetricSample(service="central-ledger").timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# AnomalyEvent
# ---------------------------------------------------------------------------
def test_anomaly_is_actionable_above_threshold() -> None:
    anomaly = AnomalyEvent(service="central-ledger", score=0.82, threshold=0.65)
    assert anomaly.is_actionable is True


def test_anomaly_below_threshold_is_not_actionable() -> None:
    assert (
        AnomalyEvent(service="central-ledger", score=0.4, threshold=0.65).is_actionable is False
    )


def test_anomaly_score_must_be_normalised() -> None:
    with pytest.raises(ValidationError):
        AnomalyEvent(service="central-ledger", score=1.7)


def test_anomaly_defaults() -> None:
    anomaly = AnomalyEvent(service="quoting-service", score=0.7)
    assert anomaly.kind == AnomalyKind.MULTIVARIATE
    assert anomaly.severity == Severity.WARNING
    assert anomaly.anomaly_id.startswith("anom_")


# ---------------------------------------------------------------------------
# PredictionEvent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "recommended", "expected"),
    [(2, 5, "scale_out"), (5, 2, "scale_in"), (3, 3, "hold")],
)
def test_scaling_action_follows_replica_delta(
    current: int, recommended: int, expected: str
) -> None:
    prediction = PredictionEvent(
        service="central-ledger",
        horizon_hours=6,
        current_replicas=current,
        recommended_replicas=recommended,
    )
    assert prediction.scaling_action == expected


def test_forecast_points_round_trip() -> None:
    now = utc_now()
    prediction = PredictionEvent(
        service="central-ledger",
        horizon_hours=1,
        points=[
            ForecastPoint(timestamp=now + timedelta(minutes=i), predicted_tps=float(i))
            for i in range(3)
        ],
    )
    dumped = prediction.model_dump(mode="json")
    assert len(dumped["points"]) == 3
    assert PredictionEvent.model_validate(dumped).points[2].predicted_tps == 2.0


# ---------------------------------------------------------------------------
# Incident & RCA
# ---------------------------------------------------------------------------
def test_primary_cause_is_the_first_hypothesis() -> None:
    incident = Incident(
        title="Ledger latency spike",
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.CHANGE,
                target="dep_abc123",
                confidence=0.88,
                summary="Deploy of central-ledger v2.4.1 preceded the spike",
            ),
            RootCauseHypothesis(
                category=RootCauseCategory.EVENT,
                target="traffic_surge",
                confidence=0.31,
                summary="Traffic 2x above baseline",
            ),
        ],
    )
    assert incident.primary_cause is not None
    assert incident.primary_cause.category == RootCauseCategory.CHANGE
    assert incident.primary_cause.confidence == pytest.approx(0.88)


def test_incident_without_hypotheses_has_no_primary_cause() -> None:
    assert Incident(title="unknown").primary_cause is None


def test_incident_duration_is_none_while_open() -> None:
    assert Incident(title="open one").duration_seconds is None


def test_incident_duration_when_resolved() -> None:
    opened = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    incident = Incident(
        title="resolved one",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=4),
        status="resolved",
    )
    assert incident.duration_seconds == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# HealingAction
# ---------------------------------------------------------------------------
def test_proposed_action_is_not_executed() -> None:
    action = HealingAction(
        playbook=PlaybookName.RESTART_ON_OOM, target_service="central-ledger"
    )
    assert action.status == ActionStatus.PROPOSED
    assert action.dry_run is True
    assert action.was_executed is False


@pytest.mark.parametrize(
    ("status", "executed"),
    [
        (ActionStatus.SUCCEEDED, True),
        (ActionStatus.FAILED, True),
        (ActionStatus.SKIPPED_COOLDOWN, False),
        (ActionStatus.SKIPPED_DRY_RUN, False),
    ],
)
def test_was_executed_reflects_terminal_status(status: ActionStatus, executed: bool) -> None:
    action = HealingAction(
        playbook=PlaybookName.CIRCUIT_BREAK_CASCADE,
        target_service="quoting-service",
        status=status,
    )
    assert action.was_executed is executed


# ---------------------------------------------------------------------------
# ChaosRun — the demo scorecard
# ---------------------------------------------------------------------------
def test_chaos_run_timings() -> None:
    start = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    run = ChaosRun(
        experiment=ChaosExperiment.MEMORY_HOG,
        target_service="central-ledger",
        started_at=start,
        detected_at=start + timedelta(seconds=25),
        remediated_at=start + timedelta(seconds=70),
    )
    assert run.time_to_detect_seconds == pytest.approx(25.0)
    assert run.time_to_remediate_seconds == pytest.approx(70.0)


def test_chaos_run_timings_are_none_before_detection() -> None:
    run = ChaosRun(experiment=ChaosExperiment.CPU_HOG, target_service="quoting-service")
    assert run.time_to_detect_seconds is None
    assert run.time_to_remediate_seconds is None


# ---------------------------------------------------------------------------
# Constants integrity
# ---------------------------------------------------------------------------
def test_dependency_graph_only_references_known_services() -> None:
    for service, dependencies in MOJALOOP_DEPENDENCIES.items():
        assert service in MOJALOOP_SERVICES, service
        for dependency in dependencies:
            assert dependency in MOJALOOP_SERVICES, dependency


def test_dependency_graph_is_acyclic() -> None:
    # Root cause analysis walks this graph upstream; a cycle would loop forever.
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        assert node not in visiting, f"cycle through {node}"
        visiting.add(node)
        for child in MOJALOOP_DEPENDENCIES.get(node, ()):
            visit(child)
        visiting.discard(node)
        done.add(node)

    for service in MOJALOOP_DEPENDENCIES:
        visit(service)


def test_new_id_is_prefixed_and_unique() -> None:
    first, second = new_id("inc"), new_id("inc")
    assert first.startswith("inc_") and second.startswith("inc_")
    assert first != second
