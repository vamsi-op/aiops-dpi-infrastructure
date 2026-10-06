"""Root Cause Analysis engine behaviour (module 7).

Tests cover the four correlation methods and their interaction:

* **Temporal correlation**: clusters anomalies that occur within a time window
* **Topological correlation**: detects cascades using the dependency graph
* **Change correlation**: links anomalies to recent deployments
* **Event correlation**: correlates with traffic surges and chaos experiments

Key acceptance criteria:
- Cascade detection: 1 incident for 3 dependent services
- Change correlation: deployment followed by errors
- Event correlation: traffic surge or chaos experiment correlation
- Confidence scoring: appropriate confidence levels for each category
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops_common.constants import AnomalyKind, RootCauseCategory, Severity
from aiops_common.schemas import AnomalyEvent, DeploymentEvent
from rca_engine.correlator import RCACorrelator


@pytest.mark.asyncio
async def test_temporal_correlation_groups_nearby_anomalies() -> None:
    """Anomalies within temporal window should be grouped together."""
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomalies = [
        AnomalyEvent(
            anomaly_id=f"anom-{i}",
            detected_at=base_time + timedelta(seconds=i * 30),
            service="central-ledger",
            metric_name="tps",
            severity=Severity.WARNING,
            anomaly_score=0.8,
            contributing_features=["tps"],
            predicted_kind=AnomalyKind.TRAFFIC_SURGE,
        )
        for i in range(3)
    ]

    correlator = RCACorrelator(db=None)
    hypotheses = await correlator.correlate(anomalies)

    assert len(hypotheses) > 0
    # Should have at least temporal correlation
    temporal = [h for h in hypotheses if "temporal" in h.summary.lower() or "same time" in h.summary.lower()]
    assert len(temporal) > 0


@pytest.mark.asyncio
async def test_topological_cascade_detection() -> None:
    """Cascade through dependency graph should be detected.
    
    Acceptance: 3 services (e.g., central-ledger → account-lookup-service → als-oracle-pathfinder)
    should create 1 incident with topological correlation.
    """
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    # Simulate cascade: central-ledger fails, then downstream services
    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-1",
            detected_at=base_time,
            service="central-ledger",
            metric_name="error_ratio",
            severity=Severity.CRITICAL,
            anomaly_score=0.95,
            contributing_features=["error_ratio", "latency_p99_seconds"],
            predicted_kind=AnomalyKind.ERROR_SPIKE,
        ),
        AnomalyEvent(
            anomaly_id="anom-2",
            detected_at=base_time + timedelta(seconds=45),
            service="account-lookup-service",
            metric_name="error_ratio",
            severity=Severity.CRITICAL,
            anomaly_score=0.92,
            contributing_features=["error_ratio"],
            predicted_kind=AnomalyKind.ERROR_SPIKE,
        ),
        AnomalyEvent(
            anomaly_id="anom-3",
            detected_at=base_time + timedelta(seconds=90),
            service="als-oracle-pathfinder",
            metric_name="error_ratio",
            severity=Severity.CRITICAL,
            anomaly_score=0.88,
            contributing_features=["error_ratio"],
            predicted_kind=AnomalyKind.ERROR_SPIKE,
        ),
    ]

    correlator = RCACorrelator(db=None)
    hypotheses = await correlator.correlate(anomalies)

    # Should detect topological correlation (cascade)
    topological = [
        h for h in hypotheses
        if h.category == RootCauseCategory.SERVICE
        and ("cascade" in h.summary.lower() or "dependency" in h.summary.lower())
    ]
    assert len(topological) > 0, "Should detect service cascade"
    
    # Cascade should have high confidence
    if topological:
        assert topological[0].confidence >= 0.70, "Cascade should have high confidence"
        # Should identify central-ledger as root
        assert "central-ledger" in topological[0].summary


@pytest.mark.asyncio
async def test_change_correlation_with_deployment(mock_db_with_deployment) -> None:
    """Anomaly following deployment should be correlated with high confidence.
    
    Acceptance: deployment at T, error spike at T+2min should correlate.
    """
    deploy_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=deploy_time + timedelta(minutes=2),
        service="central-ledger",
        metric_name="error_ratio",
        severity=Severity.CRITICAL,
        anomaly_score=0.92,
        contributing_features=["error_ratio"],
        predicted_kind=AnomalyKind.ERROR_SPIKE,
    )

    correlator = RCACorrelator(db=mock_db_with_deployment)
    hypotheses = await correlator.correlate([anomaly])

    # Should detect change correlation
    change = [h for h in hypotheses if h.category == RootCauseCategory.CHANGE]
    assert len(change) > 0, "Should detect change correlation"
    
    # Change correlation should have high confidence when close in time
    if change:
        assert change[0].confidence >= 0.50, "Recent deployment should have high confidence"
        assert "deployment" in change[0].summary.lower() or "deploy" in change[0].summary.lower()


@pytest.mark.asyncio
async def test_chaos_experiment_correlation(mock_db_with_chaos) -> None:
    """Anomaly during chaos experiment should be correlated with chaos event.
    
    Acceptance: chaos experiment marked as "expected disruption" with 99% confidence.
    """
    chaos_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=chaos_time + timedelta(seconds=30),
        service="central-ledger",
        metric_name="latency_p99_seconds",
        severity=Severity.WARNING,
        anomaly_score=0.85,
        contributing_features=["latency_p99_seconds"],
        predicted_kind=AnomalyKind.LATENCY_SPIKE,
    )

    correlator = RCACorrelator(db=mock_db_with_chaos)
    hypotheses = await correlator.correlate([anomaly])

    # Should detect chaos experiment
    event = [h for h in hypotheses if h.category == RootCauseCategory.EVENT]
    assert len(event) > 0, "Should detect chaos experiment"
    
    # Chaos should have very high confidence (99%)
    if event:
        assert event[0].confidence >= 0.95, "Chaos experiment should have 99% confidence"
        assert "chaos" in event[0].summary.lower()


@pytest.mark.asyncio
async def test_traffic_surge_correlation(mock_db_with_traffic) -> None:
    """Traffic surge should be detected and correlated with anomalies."""
    surge_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    
    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-1",
            detected_at=surge_time + timedelta(seconds=15),
            service="central-ledger",
            metric_name="tps",
            severity=Severity.WARNING,
            anomaly_score=0.78,
            contributing_features=["tps", "cpu_utilisation_ratio"],
            predicted_kind=AnomalyKind.TRAFFIC_SURGE,
        ),
        AnomalyEvent(
            anomaly_id="anom-2",
            detected_at=surge_time + timedelta(seconds=30),
            service="central-ledger",
            metric_name="cpu_utilisation_ratio",
            severity=Severity.WARNING,
            anomaly_score=0.75,
            contributing_features=["cpu_utilisation_ratio"],
            predicted_kind=AnomalyKind.RESOURCE_EXHAUSTION,
        ),
    ]

    correlator = RCACorrelator(db=mock_db_with_traffic)
    hypotheses = await correlator.correlate(anomalies)

    # Should detect traffic event
    event = [h for h in hypotheses if h.category == RootCauseCategory.EVENT]
    assert len(event) > 0, "Should detect traffic surge event"


@pytest.mark.asyncio
async def test_hypothesis_ranking_by_confidence() -> None:
    """Hypotheses should be ranked by confidence, highest first."""
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    
    # Create anomaly that could have multiple explanations
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=base_time,
        service="central-ledger",
        metric_name="error_ratio",
        severity=Severity.CRITICAL,
        anomaly_score=0.92,
        contributing_features=["error_ratio"],
        predicted_kind=AnomalyKind.ERROR_SPIKE,
    )

    correlator = RCACorrelator(db=None)
    hypotheses = await correlator.correlate([anomaly])

    # Hypotheses should be sorted by confidence descending
    if len(hypotheses) > 1:
        for i in range(len(hypotheses) - 1):
            assert hypotheses[i].confidence >= hypotheses[i + 1].confidence, \
                "Hypotheses should be sorted by confidence descending"


# Fixtures for database mocking

@pytest.fixture
async def mock_db_with_deployment():
    """Mock database that returns a recent deployment."""
    class MockDB:
        async def fetch(self, query: str, *args):
            # Return a deployment that happened recently
            deploy_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
            return [
                {
                    "service": "central-ledger",
                    "deployed_at": deploy_time,
                    "version": "v1.2.3",
                }
            ]
    
    return MockDB()


@pytest.fixture
async def mock_db_with_chaos():
    """Mock database that returns a chaos experiment event."""
    class MockDB:
        async def fetch(self, query: str, *args):
            chaos_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
            return [
                {
                    "event_type": "chaos_experiment",
                    "service": "central-ledger",
                    "created_at": chaos_time,
                    "description": "Network latency injection",
                }
            ]
    
    return MockDB()


@pytest.fixture
async def mock_db_with_traffic():
    """Mock database that returns traffic surge event."""
    class MockDB:
        async def fetch(self, query: str, *args):
            surge_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
            return [
                {
                    "event_type": "traffic_surge",
                    "service": "central-ledger",
                    "created_at": surge_time,
                    "description": "Traffic increased by 250%",
                }
            ]
    
    return MockDB()
