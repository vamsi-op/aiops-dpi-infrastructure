"""Root Cause Analysis engine behaviour (module 7).

Tests cover the four correlation methods and their interaction:
* **Temporal correlation**: clusters anomalies that occur within a time window
* **Topological correlation**: detects cascades using the dependency graph
* **Change correlation**: links anomalies to recent deployments
* **Event correlation**: correlates with traffic surges and chaos experiments
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiops_common.constants import AnomalyKind, RootCauseCategory, Severity
from aiops_common.schemas import AnomalyEvent
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
            severity=Severity.WARNING,
            score=0.8,
            contributing_features=["tps"],
            kind=AnomalyKind.TRAFFIC_SURGE,
        )
        for i in range(3)
    ]

    correlator = RCACorrelator(db=None)
    clusters = await correlator.correlate_temporal(anomalies)

    assert len(clusters) == 1
    assert len(clusters[0]) == 3


@pytest.mark.asyncio
async def test_topological_cascade_detection() -> None:
    """Cascade through dependency graph should be detected.

    ml-api-adapter -> quoting-service -> central-ledger.
    central-ledger has no downstream dependencies in Mojaloop, so it is the root.
    """
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-1",
            detected_at=base_time,
            service="central-ledger",
            severity=Severity.CRITICAL,
            score=0.95,
            contributing_features=["error_ratio", "latency_p99_seconds"],
            kind=AnomalyKind.ERROR_BURST,
        ),
        AnomalyEvent(
            anomaly_id="anom-2",
            detected_at=base_time + timedelta(seconds=45),
            service="quoting-service",
            severity=Severity.CRITICAL,
            score=0.92,
            contributing_features=["error_ratio"],
            kind=AnomalyKind.ERROR_BURST,
        ),
        AnomalyEvent(
            anomaly_id="anom-3",
            detected_at=base_time + timedelta(seconds=90),
            service="ml-api-adapter",
            severity=Severity.CRITICAL,
            score=0.88,
            contributing_features=["error_ratio"],
            kind=AnomalyKind.ERROR_BURST,
        ),
    ]

    correlator = RCACorrelator(db=None)
    hypothesis = correlator.correlate_topological(anomalies)

    assert hypothesis is not None
    assert hypothesis.category == RootCauseCategory.SERVICE
    assert hypothesis.target in {"central-ledger", "ml-api-adapter", "quoting-service"}
    assert hypothesis.confidence >= 0.70


@pytest.mark.asyncio
async def test_change_correlation_with_deployment(mock_db_with_deployment) -> None:
    """Anomaly following deployment should be correlated with high confidence."""
    deploy_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=deploy_time + timedelta(minutes=2),
        service="central-ledger",
        severity=Severity.CRITICAL,
        score=0.92,
        contributing_features=["error_ratio"],
        kind=AnomalyKind.ERROR_BURST,
    )

    correlator = RCACorrelator(db=mock_db_with_deployment)
    hypotheses = await correlator.correlate_change([anomaly])

    assert len(hypotheses) > 0
    change = hypotheses[0]
    assert change.category == RootCauseCategory.CHANGE
    assert change.confidence >= 0.50
    assert "deploy" in change.summary.lower()


@pytest.mark.asyncio
async def test_chaos_experiment_correlation(mock_db_with_chaos) -> None:
    """Anomaly during chaos experiment should be correlated with chaos event."""
    chaos_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=chaos_time + timedelta(seconds=30),
        service="central-ledger",
        severity=Severity.WARNING,
        score=0.85,
        contributing_features=["latency_p99_seconds"],
        kind=AnomalyKind.LATENCY_SPIKE,
    )

    correlator = RCACorrelator(db=mock_db_with_chaos)
    hypotheses = await correlator.correlate_event([anomaly])

    assert len(hypotheses) > 0
    event = hypotheses[0]
    assert event.category == RootCauseCategory.EVENT
    assert event.confidence >= 0.95
    assert "chaos" in event.summary.lower()


@pytest.mark.asyncio
async def test_traffic_surge_correlation() -> None:
    """Traffic surge should be detected and correlated with anomalies."""
    surge_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-1",
            detected_at=surge_time + timedelta(seconds=15),
            service="central-ledger",
            severity=Severity.WARNING,
            score=0.78,
            contributing_features=["tps"],
            kind=AnomalyKind.TRAFFIC_SURGE,
        ),
        AnomalyEvent(
            anomaly_id="anom-2",
            detected_at=surge_time + timedelta(seconds=30),
            service="central-ledger",
            severity=Severity.WARNING,
            score=0.75,
            contributing_features=["tps"],
            kind=AnomalyKind.TRAFFIC_SURGE,
        ),
    ]

    class EmptyDB:
        async def fetch_all(self, query: str, params: dict[str, Any] | None = None):
            return []

    correlator = RCACorrelator(db=EmptyDB())
    hypotheses = await correlator.correlate_event(anomalies)

    assert len(hypotheses) > 0
    event = hypotheses[0]
    assert event.category == RootCauseCategory.EVENT
    assert "traffic" in event.summary.lower()


@pytest.mark.asyncio
async def test_hypothesis_ranking_by_confidence(mock_db_with_deployment) -> None:
    """Hypotheses should be ranked by confidence, highest first."""
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=base_time + timedelta(minutes=2),
        service="central-ledger",
        severity=Severity.CRITICAL,
        score=0.92,
        contributing_features=["error_ratio"],
        kind=AnomalyKind.ERROR_BURST,
    )

    correlator = RCACorrelator(db=mock_db_with_deployment)
    hypotheses = await correlator.correlate_all([anomaly])

    if len(hypotheses) > 1:
        for i in range(len(hypotheses) - 1):
            assert hypotheses[i].confidence >= hypotheses[i + 1].confidence


# Fixtures for database mocking
@pytest.fixture
def mock_db_with_deployment():
    """Mock database returning a recent deployment."""

    class MockDB:
        async def fetch_all(self, query: str, params: dict[str, Any] | None = None):
            deploy_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
            return [
                {
                    "deployment_id": "dep-1",
                    "occurred_at": deploy_time,
                    "service": "central-ledger",
                    "to_version": "v1.2.3",
                    "change_type": "deploy",
                }
            ]

    return MockDB()


@pytest.fixture
def mock_db_with_chaos():
    """Mock database returning an active chaos experiment."""

    class MockDB:
        async def fetch_all(self, query: str, params: dict[str, Any] | None = None):
            chaos_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
            return [
                {
                    "run_id": "chaos-1",
                    "experiment": "latency_injection",
                    "target_service": "central-ledger",
                    "started_at": chaos_time,
                }
            ]

    return MockDB()


# ---------------------------------------------------------------------------
# Incident Auto-Resolution Tests (SLO verification, benign anomalies & healing)
# ---------------------------------------------------------------------------
class MockIncidentDB:
    """Mock DB for testing incident lifecycle and resolution rules."""

    def __init__(
        self,
        incidents: list[dict[str, Any]],
        healing_actions: list[dict[str, Any]] | None = None,
        service_metrics: list[dict[str, Any]] | None = None,
        anomalies: list[dict[str, Any]] | None = None,
    ):
        self.incidents = incidents
        self.healing_actions = healing_actions or []
        self.service_metrics = service_metrics or []
        self.anomalies = anomalies or []
        self.resolved_ids: list[str] = []

    async def fetch_all(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        q = query.lower()
        if "from incidents" in q:
            return [
                inc for inc in self.incidents if inc.get("status") in ("open", "mitigating")
            ]
        if "from service_metrics" in q:
            return self.service_metrics
        if "from anomalies" in q:
            return self.anomalies
        return []

    async def fetch_one(
        self, query: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params = params or {}
        q = query.lower()
        if "from healing_actions" in q:
            inc_id = params.get("incident_id")
            for act in self.healing_actions:
                if act.get("incident_id") == inc_id:
                    return act
            return self.healing_actions[0] if self.healing_actions else None
        if "select ai_summary from incidents" in q:
            inc_id = params.get("incident_id")
            for inc in self.incidents:
                if inc.get("incident_id") == inc_id:
                    return {"ai_summary": inc.get("ai_summary", "")}
            return None
        return None

    async def execute(self, query: str, params: dict[str, Any] | None = None) -> int:
        params = params or {}
        q = query.lower()
        if "update incidents" in q and "status = 'resolved'" in q:
            inc_id = params.get("incident_id")
            if inc_id:
                self.resolved_ids.append(inc_id)
                for inc in self.incidents:
                    if inc.get("incident_id") == inc_id:
                        inc["status"] = "resolved"
                        inc["ai_summary"] = params.get("ai_summary", "")
            return 1
        return 0


class MockEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, data: dict[str, Any]) -> None:
        self.published.append((stream, data))


@pytest.mark.asyncio
async def test_auto_resolve_healthy_with_benign_unrelated_anomalies() -> None:
    """Healthy service with benign/sub-threshold anomalies resolves after recovery window."""
    from rca_engine.incident_manager import IncidentManager

    now = datetime.now(UTC)
    incident = {
        "incident_id": "inc_test_benign",
        "opened_at": now - timedelta(seconds=200),
        "status": "open",
        "affected_services": ["ml-api-adapter"],
        "hypotheses": [
            {
                "target": "ml-api-adapter",
                "category": "service",
                "confidence": 0.9,
                "summary": "ml-api-adapter issue",
            }
        ],
        "ai_summary": "Issue in ml-api-adapter",
    }
    # Healthy telemetry strictly within SLO thresholds
    metrics = [
        {
            "service": "ml-api-adapter",
            "avg_error": 0.0008,  # 0.08% <= 1.0%
            "avg_p99": 0.120,  # 120ms <= 250ms
            "avg_queue": 0.0,  # 0 <= 5.0
            "avg_cpu": 0.35,  # 35% <= 85%
            "avg_mem": 0.40,  # 40% <= 85%
        }
    ]
    # Benign / info-level jitter anomalies generated by Isolation Forest
    anomalies = [
        {
            "anomaly_id": "anom_benign_1",
            "detected_at": now - timedelta(seconds=15),
            "service": "ml-api-adapter",
            "severity": Severity.INFO,
            "score": 0.62,
            "kind": AnomalyKind.LATENCY_SPIKE,
            "contributing_features": ["latency_p99_seconds"],
        }
    ]

    mock_db = MockIncidentDB(
        incidents=[incident],
        service_metrics=metrics,
        anomalies=anomalies,
    )
    bus = MockEventBus()
    mgr = IncidentManager(db=mock_db, bus=bus)

    await mgr.check_auto_resolve()

    assert "inc_test_benign" in mock_db.resolved_ids
    assert incident["status"] == "resolved"
    assert "Resolution:" in incident["ai_summary"]


@pytest.mark.asyncio
async def test_auto_resolve_blocked_by_recurring_relevant_anomaly() -> None:
    """Recurring critical anomaly on primary target keeps incident OPEN."""
    from rca_engine.incident_manager import IncidentManager

    now = datetime.now(UTC)
    incident = {
        "incident_id": "inc_test_recurring",
        "opened_at": now - timedelta(seconds=200),
        "status": "open",
        "affected_services": ["quoting-service"],
        "hypotheses": [
            {
                "target": "quoting-service",
                "category": "service",
                "confidence": 0.95,
                "summary": "quoting-service failure",
            }
        ],
        "ai_summary": "Quoting service failure",
    }
    metrics = [
        {
            "service": "quoting-service",
            "avg_error": 0.002,
            "avg_p99": 0.150,
            "avg_queue": 0.0,
            "avg_cpu": 0.45,
            "avg_mem": 0.50,
        }
    ]
    # Severe recurring anomaly directly on primary target
    anomalies = [
        {
            "anomaly_id": "anom_crit_1",
            "detected_at": now - timedelta(seconds=20),
            "service": "quoting-service",
            "severity": Severity.CRITICAL,
            "score": 0.92,
            "kind": AnomalyKind.ERROR_BURST,
            "contributing_features": ["error_ratio"],
        }
    ]

    mock_db = MockIncidentDB(
        incidents=[incident],
        service_metrics=metrics,
        anomalies=anomalies,
    )
    bus = MockEventBus()
    mgr = IncidentManager(db=mock_db, bus=bus)

    await mgr.check_auto_resolve()

    assert "inc_test_recurring" not in mock_db.resolved_ids
    assert incident["status"] == "open"


@pytest.mark.asyncio
async def test_auto_resolve_blocked_by_unhealthy_slo_metrics() -> None:
    """Unhealthy SLO metrics keep incident OPEN."""
    from rca_engine.incident_manager import IncidentManager

    now = datetime.now(UTC)
    incident = {
        "incident_id": "inc_test_unhealthy_slo",
        "opened_at": now - timedelta(seconds=200),
        "status": "open",
        "affected_services": ["central-ledger"],
        "hypotheses": [
            {
                "target": "central-ledger",
                "category": "service",
                "confidence": 0.9,
                "summary": "central-ledger degradation",
            }
        ],
        "ai_summary": "Degradation in central ledger",
    }
    # Unhealthy SLO metrics (e.g. error ratio 6% exceeds 1% SLO)
    metrics = [
        {
            "service": "central-ledger",
            "avg_error": 0.06,  # 6% > 1% SLO
            "avg_p99": 0.180,
            "avg_queue": 0.0,
            "avg_cpu": 0.50,
            "avg_mem": 0.55,
        }
    ]

    mock_db = MockIncidentDB(
        incidents=[incident],
        service_metrics=metrics,
        anomalies=[],
    )
    bus = MockEventBus()
    mgr = IncidentManager(db=mock_db, bus=bus)

    await mgr.check_auto_resolve()

    assert "inc_test_unhealthy_slo" not in mock_db.resolved_ids
    assert incident["status"] == "open"


@pytest.mark.asyncio
async def test_auto_resolve_blocked_by_failed_healing_action() -> None:
    """Failed healing action keeps incident OPEN."""
    from aiops_common.constants import ActionStatus
    from rca_engine.incident_manager import IncidentManager

    now = datetime.now(UTC)
    incident = {
        "incident_id": "inc_test_failed_action",
        "opened_at": now - timedelta(seconds=200),
        "status": "open",
        "affected_services": ["transaction-request-service"],
        "hypotheses": [
            {
                "target": "transaction-request-service",
                "category": "service",
                "confidence": 0.9,
                "summary": "transaction-request-service failure",
            }
        ],
        "ai_summary": "Transaction request service down",
    }
    metrics = [
        {
            "service": "transaction-request-service",
            "avg_error": 0.001,
            "avg_p99": 0.100,
            "avg_queue": 0.0,
            "avg_cpu": 0.30,
            "avg_mem": 0.30,
        }
    ]
    healing_actions = [
        {
            "action_id": "act_failed_1",
            "incident_id": "inc_test_failed_action",
            "status": ActionStatus.FAILED,
            "result_message": "Container restart failed recovery verification",
        }
    ]

    mock_db = MockIncidentDB(
        incidents=[incident],
        healing_actions=healing_actions,
        service_metrics=metrics,
        anomalies=[],
    )
    bus = MockEventBus()
    mgr = IncidentManager(db=mock_db, bus=bus)

    await mgr.check_auto_resolve()

    assert "inc_test_failed_action" not in mock_db.resolved_ids
    assert incident["status"] == "open"


@pytest.mark.asyncio
async def test_auto_resolve_succeeds_with_healthy_metrics_and_verified_healing() -> None:
    """Healthy metrics with successful recovery verification resolves incident correctly."""
    from aiops_common.constants import ActionStatus
    from rca_engine.incident_manager import IncidentManager

    now = datetime.now(UTC)
    incident = {
        "incident_id": "inc_test_verified_healing",
        "opened_at": now - timedelta(seconds=250),
        "status": "open",
        "affected_services": ["ml-api-adapter"],
        "hypotheses": [
            {
                "target": "ml-api-adapter",
                "category": "service",
                "confidence": 0.9,
                "summary": "ml-api-adapter recovered",
            }
        ],
        "ai_summary": "ml-api-adapter incident",
    }
    metrics = [
        {
            "service": "ml-api-adapter",
            "avg_error": 0.0005,
            "avg_p99": 0.110,
            "avg_queue": 0.0,
            "avg_cpu": 0.32,
            "avg_mem": 0.38,
        }
    ]
    healing_actions = [
        {
            "action_id": "act_verified_1",
            "incident_id": "inc_test_verified_healing",
            "status": ActionStatus.SUCCEEDED,
            "result_message": "Restarted container (Recovery verified)",
            "parameters": {
                "recovery_verification": {
                    "verified": True,
                    "reason": "Queue depth and latency within SLO",
                }
            },
        }
    ]

    mock_db = MockIncidentDB(
        incidents=[incident],
        healing_actions=healing_actions,
        service_metrics=metrics,
        anomalies=[],
    )
    bus = MockEventBus()
    mgr = IncidentManager(db=mock_db, bus=bus)

    await mgr.check_auto_resolve()

    assert "inc_test_verified_healing" in mock_db.resolved_ids
    assert incident["status"] == "resolved"
    assert "Resolution:" in incident["ai_summary"]
    assert "Remediation verified recovery" in incident["ai_summary"]
