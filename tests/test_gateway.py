"""Tests for the API gateway's own logic (no database or Redis required).

The gateway is mostly SQL plumbing, but two parts carry real decisions worth pinning
down: how a service's display status is derived, and how untyped database values are
normalised before they reach the UI.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

# The gateway is a service, not an installed package, so its directory goes on the path.
GATEWAY_ROOT = Path(__file__).resolve().parent.parent / "services" / "api_gateway"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from api_gateway import health_rules  # noqa: E402
from api_gateway.repository import DashboardRepository, _json, _num  # noqa: E402


# ---------------------------------------------------------------------------
# Display health rules
# ---------------------------------------------------------------------------
def test_all_signals_nominal_is_healthy() -> None:
    status, reasons = health_rules.evaluate(
        {
            "error_ratio": 0.002,
            "latency_p99_seconds": 0.4,
            "cpu_utilisation_ratio": 0.35,
            "memory_utilisation_ratio": 0.5,
            "queue_depth": 2,
            "settlement_lag_seconds": 1.5,
        }
    )
    assert status == "healthy"
    assert reasons == []


def test_stale_wins_over_every_other_signal() -> None:
    # A service that stopped reporting must not be shown as healthy just because its
    # last known numbers were fine.
    status, reasons = health_rules.evaluate({"error_ratio": 0.0}, stale=True)
    assert status == "stale"
    assert reasons == ["no samples received recently"]


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"error_ratio": 0.02}, "degraded"),
        ({"error_ratio": 0.08}, "critical"),
        ({"latency_p99_seconds": 1.5}, "degraded"),
        ({"latency_p99_seconds": 4.0}, "critical"),
        ({"memory_utilisation_ratio": 0.85}, "degraded"),
        ({"memory_utilisation_ratio": 0.95}, "critical"),
        ({"cpu_utilisation_ratio": 0.8}, "degraded"),
        ({"queue_depth": 250}, "critical"),
        ({"settlement_lag_seconds": 45}, "degraded"),
        ({"settlement_lag_seconds": 200}, "critical"),
    ],
)
def test_each_signal_can_trip_the_status(snapshot: dict[str, float], expected: str) -> None:
    status, reasons = health_rules.evaluate(snapshot)
    assert status == expected
    assert reasons, "a non-healthy status must explain itself"


def test_worst_signal_determines_the_status() -> None:
    # Memory is only at warning level, but the error ratio is critical; the tile must
    # show critical, not an average of the two.
    status, reasons = health_rules.evaluate(
        {"error_ratio": 0.09, "memory_utilisation_ratio": 0.85}
    )
    assert status == "critical"
    assert len(reasons) == 2


def test_reasons_name_the_metric_and_the_threshold() -> None:
    _, reasons = health_rules.evaluate({"memory_utilisation_ratio": 0.95})
    assert "memory" in reasons[0]
    assert "critical" in reasons[0]


def test_platform_status_is_the_worst_service() -> None:
    assert health_rules.worst_of(["healthy", "healthy", "degraded"]) == "degraded"
    assert health_rules.worst_of(["healthy", "critical", "degraded"]) == "critical"
    assert health_rules.worst_of(["healthy", "healthy"]) == "healthy"
    # A service with no data is less alarming than one actively degraded.
    assert health_rules.worst_of(["stale", "degraded"]) == "degraded"


def test_platform_status_with_no_services_is_unknown() -> None:
    assert health_rules.worst_of([]) == "unknown"


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------
def test_json_decodes_a_string_column() -> None:
    # asyncpg returns jsonb as a string for raw text() queries, because it has no type
    # information to work from.
    assert _json('{"memory": 0.93}', {}) == {"memory": 0.93}


def test_json_passes_through_already_decoded_values() -> None:
    assert _json({"a": 1}, {}) == {"a": 1}
    assert _json([1, 2], []) == [1, 2]


def test_json_falls_back_on_malformed_input() -> None:
    assert _json("{not json", {}) == {}
    assert _json(None, []) == []


def test_num_handles_decimal_from_sum_aggregates() -> None:
    # sum() over a bigint column returns numeric, which arrives as Decimal and is not
    # JSON serialisable.
    value = _num(Decimal("137.52"))
    assert isinstance(value, float)
    assert value == pytest.approx(137.52)


def test_num_handles_none_and_junk() -> None:
    assert _num(None) == 0.0
    assert _num(None, 5.0) == 5.0
    assert _num("not a number") == 0.0
    assert _num("42.5") == pytest.approx(42.5)


# ---------------------------------------------------------------------------
# Row cleaning
# ---------------------------------------------------------------------------
def test_clean_anomaly_normalises_every_field() -> None:
    cleaned = DashboardRepository._clean_anomaly(
        {
            "anomaly_id": "anom_1",
            "score": Decimal("0.87"),
            "raw_score": Decimal("-0.42"),
            "threshold": None,
            "contributing_features": None,
            "feature_snapshot": '{"memory_utilisation_ratio": 0.93}',
            "feature_deviations": '{"memory_utilisation_ratio": 4.2}',
        }
    )
    assert cleaned["score"] == pytest.approx(0.87)
    assert cleaned["raw_score"] == pytest.approx(-0.42)
    assert cleaned["threshold"] == pytest.approx(0.65)  # default when null
    assert cleaned["contributing_features"] == []
    assert cleaned["feature_snapshot"] == {"memory_utilisation_ratio": 0.93}
    assert cleaned["feature_deviations"] == {"memory_utilisation_ratio": 4.2}


def test_clean_incident_normalises_arrays_and_hypotheses() -> None:
    cleaned = DashboardRepository._clean_incident(
        {
            "incident_id": "inc_1",
            "affected_services": None,
            "anomaly_ids": ["anom_1"],
            "action_ids": None,
            "hypotheses": '[{"category": "service", "target": "central-ledger", "confidence": "0.91", "summary": "s"}]',
        }
    )
    assert cleaned["affected_services"] == []
    assert cleaned["action_ids"] == []
    hypothesis = cleaned["hypotheses"][0]
    assert hypothesis["confidence"] == pytest.approx(0.91)
    # Missing optional keys are filled so the UI never has to guard for them.
    assert hypothesis["evidence"] == []
    assert hypothesis["correlated_metrics"] == {}


def test_clean_chaos_derives_timings() -> None:
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    cleaned = DashboardRepository._clean_chaos(
        {
            "run_id": "chaos_1",
            "started_at": start,
            "detected_at": start + timedelta(seconds=38),
            "remediated_at": start + timedelta(seconds=161),
            "parameters": '{"cores": 2}',
        }
    )
    assert cleaned["time_to_detect_seconds"] == pytest.approx(38.0)
    assert cleaned["time_to_remediate_seconds"] == pytest.approx(161.0)
    assert cleaned["parameters"] == {"cores": 2}


def test_clean_chaos_timings_none_when_undetected() -> None:
    from datetime import UTC, datetime

    cleaned = DashboardRepository._clean_chaos(
        {
            "run_id": "chaos_2",
            "started_at": datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
            "detected_at": None,
            "remediated_at": None,
            "parameters": None,
        }
    )
    # A missed detection must read as missing, not as zero seconds.
    assert cleaned["time_to_detect_seconds"] is None
    assert cleaned["time_to_remediate_seconds"] is None
    assert cleaned["parameters"] == {}
