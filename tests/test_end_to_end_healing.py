"""Comprehensive End-to-End Self-Healing Test Suite.

Covers all 9 required verification scenarios:
1. Multiple related anomalies within time window correlate into a single incident.
2. Duplicate incident prevention: subsequent anomalies update ongoing incident instead of duplicating.
3. Distinction between live incidents and seeded demo incidents.
4. Genuine healing execution when HEALING_ENABLED=true.
5. Dry-run behaviour when HEALING_ENABLED=false (actions marked skipped_dry_run).
6. Safety-rail enforcement (cooldown, budget, low-confidence blocking).
7. Successful recovery verification: post-remediation metrics meet SLOs -> SUCCEEDED.
8. Failed recovery verification: post-remediation metrics violate SLOs -> FAILED.
9. Incident resolution integrity: incident remains open/mitigating on recovery failure; resolves only on verified recovery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiops_common.bus import EventBus
from aiops_common.config import HealingSettings, Settings
from aiops_common.constants import (
    ActionStatus,
    AnomalyKind,
    PlaybookName,
    RootCauseCategory,
    Severity,
)
from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
from healing_engine.engine import HealingEngine
from healing_engine.executor import ExecutorInterface, ScaleResult
from healing_engine.playbooks import PlaybookExecutor, PlaybookRegistry
from healing_engine.safety import SafetyRails
from healing_engine.snapshot import MetricSnapshot
from rca_engine.correlator import RCACorrelator
from rca_engine.incident_manager import IncidentManager


# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------

class MockAsyncDB:
    """Mock database implementing fetch_all, fetch_one, execute for isolated tests."""

    def __init__(self):
        self.rows: list[dict[str, Any]] = []
        self.executed_queries: list[tuple[str, dict[str, Any] | None]] = []
        self.mock_fetch_all_result: list[dict[str, Any]] | None = None
        self.mock_fetch_one_result: dict[str, Any] | None = None

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.executed_queries.append((sql, params))
        if self.mock_fetch_all_result is not None:
            return self.mock_fetch_all_result
        return self.rows

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.executed_queries.append((sql, params))
        if self.mock_fetch_one_result is not None:
            return self.mock_fetch_one_result
        return self.rows[0] if self.rows else None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        self.executed_queries.append((sql, params))
        return 1


@pytest.fixture
def mock_bus():
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    bus.get_flag = AsyncMock(return_value=None)
    bus.set_flag = AsyncMock()
    bus.incr_with_ttl = AsyncMock(return_value=1)
    bus.client = MagicMock()
    bus.client.decr = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# Scenario 1: Multiple Related Anomalies Correlate into One Incident
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_1_multiple_anomalies_correlate_one_incident(mock_bus):
    """Multiple related anomalies within the temporal window generate one single incident."""
    db = MockAsyncDB()
    correlator = RCACorrelator(db=db)
    incident_mgr = IncidentManager(db=db, bus=mock_bus)

    base_time = datetime.now(UTC)
    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-ledger-1",
            detected_at=base_time,
            service="central-ledger",
            severity=Severity.CRITICAL,
            score=0.95,
            kind=AnomalyKind.MEMORY_PRESSURE,
            contributing_features=["memory_utilisation_ratio"],
        ),
        AnomalyEvent(
            anomaly_id="anom-ledger-2",
            detected_at=base_time + timedelta(seconds=20),
            service="central-ledger",
            severity=Severity.CRITICAL,
            score=0.98,
            kind=AnomalyKind.MEMORY_PRESSURE,
            contributing_features=["memory_utilisation_ratio", "latency_p99_seconds"],
        ),
    ]

    hypotheses = await correlator.correlate_all(anomalies)
    incident = await incident_mgr.create_incident(anomalies, hypotheses)

    assert incident is not None
    assert incident.incident_id.startswith("inc_")
    assert incident.affected_services == ["central-ledger"]
    assert len(incident.anomaly_ids) == 2
    assert "anom-ledger-1" in incident.anomaly_ids
    assert "anom-ledger-2" in incident.anomaly_ids
    # Event published
    mock_bus.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 2: Duplicate Incident Prevention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_2_duplicate_incident_prevention(mock_bus):
    """Subsequent anomalies for an ongoing problem update the existing incident instead of creating a duplicate."""
    db = MockAsyncDB()
    # Mock that an active incident already exists in the database
    existing_incident_id = "inc_active_999"
    db.mock_fetch_one_result = {
        "incident_id": existing_incident_id,
        "opened_at": datetime.now(UTC) - timedelta(minutes=2),
        "status": "open",
        "severity": "warning",
        "platform": "mojaloop",
        "title": "[Live] Service central-ledger failure",
        "affected_services": ["central-ledger"],
        "anomaly_ids": ["anom-old-1"],
        "hypotheses": json.dumps([{"category": "service", "target": "central-ledger", "confidence": 0.85, "summary": "issue"}]),
        "ai_summary": "Existing summary",
    }

    incident_mgr = IncidentManager(db=db, bus=mock_bus)

    new_anomalies = [
        AnomalyEvent(
            anomaly_id="anom-new-2",
            detected_at=datetime.now(UTC),
            service="central-ledger",
            severity=Severity.CRITICAL,
            score=0.99,
            kind=AnomalyKind.MEMORY_PRESSURE,
            contributing_features=["memory_utilisation_ratio"],
        )
    ]

    hypotheses = [
        RootCauseHypothesis(
            category=RootCauseCategory.SERVICE,
            target="central-ledger",
            confidence=0.90,
            summary="Ongoing memory leak on central-ledger",
        )
    ]

    incident = await incident_mgr.create_incident(new_anomalies, hypotheses)

    # Returned incident should have the EXISTING id, not a new duplicate
    assert incident.incident_id == existing_incident_id
    assert "anom-old-1" in incident.anomaly_ids
    assert "anom-new-2" in incident.anomaly_ids
    # An update query should have executed
    update_executed = any("UPDATE incidents" in q[0] for q in db.executed_queries)
    assert update_executed, "Should have executed an UPDATE on existing incident"


# ---------------------------------------------------------------------------
# Scenario 3: Live vs Seeded Incident Distinction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_3_live_vs_seeded_distinction(mock_bus):
    """Live incidents generated from live telemetry carry the [Live] distinction."""
    db = MockAsyncDB()
    incident_mgr = IncidentManager(db=db, bus=mock_bus)

    anomalies = [
        AnomalyEvent(
            anomaly_id="anom-live-1",
            detected_at=datetime.now(UTC),
            service="account-lookup-service",
            severity=Severity.WARNING,
            score=0.88,
            kind=AnomalyKind.LATENCY_SPIKE,
        )
    ]

    incident = await incident_mgr.create_incident(anomalies, [])
    assert incident.title.startswith("[Live]"), f"Title '{incident.title}' must start with [Live]"


# ---------------------------------------------------------------------------
# Scenario 4: Genuine Remediation Execution when HEALING_ENABLED=true
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_4_genuine_healing_execution(mock_bus):
    """When HEALING_ENABLED=true, real operations execute and are logged as real."""
    db = MockAsyncDB()
    snapshot = MetricSnapshot(db=db)

    # Mock real executor
    mock_executor = MagicMock(spec=ExecutorInterface)
    mock_executor.restart_container = AsyncMock(return_value=(True, "Restarted container central-ledger-1"))
    mock_executor.get_replica_count = AsyncMock(return_value=1)
    mock_executor.client = MagicMock()
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_executor.client.containers.list.return_value = [mock_container]

    playbook_executor = PlaybookExecutor(mock_executor)

    res = await playbook_executor.execute(
        PlaybookName.RESTART_ON_OOM,
        "central-ledger",
        {"anomaly_kind": AnomalyKind.MEMORY_PRESSURE},
    )

    success, message = res[0], res[1]
    metadata = getattr(res, "metadata", {})

    assert success is True
    assert "Restarted container" in message
    assert metadata.get("execution_mode") == "real"
    mock_executor.restart_container.assert_called_once_with("central-ledger")


# ---------------------------------------------------------------------------
# Scenario 5: Dry-Run Behaviour when HEALING_ENABLED=false
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_5_dry_run_behaviour(mock_bus):
    """When HEALING_ENABLED=false, safety rails block execution with SKIPPED_DRY_RUN."""
    settings = HealingSettings(HEALING_ENABLED=False)
    safety = SafetyRails(mock_bus, settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.95)

    assert allowed is False
    assert status == ActionStatus.SKIPPED_DRY_RUN
    assert "disabled" in reason.lower() or "dry-run" in reason.lower()


# ---------------------------------------------------------------------------
# Scenario 6: Safety-Rail Blocking (Cooldown, Budget, Low Confidence)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_6_safety_rails_enforcement(mock_bus):
    """Safety rails block actions when confidence is below floor or cooldown is active."""
    settings = HealingSettings(
        HEALING_ENABLED=True,
        min_confidence=0.80,
        cooldown_seconds=300,
        max_actions_per_hour=5,
    )
    safety = SafetyRails(mock_bus, settings)

    # 1. Low confidence check
    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.60)
    assert allowed is False
    assert status == ActionStatus.SKIPPED_LOW_CONFIDENCE

    # 2. Cooldown check: mock active cooldown in Redis
    mock_bus.get_flag = AsyncMock(return_value=b"1")
    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.95)
    assert allowed is False
    assert status == ActionStatus.SKIPPED_COOLDOWN


# ---------------------------------------------------------------------------
# Scenario 7: Successful Recovery Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_7_successful_recovery_verification():
    """Post-remediation metrics meeting SLOs and showing clear improvement result in SUCCEEDED."""
    db = MockAsyncDB()
    snapshot = MetricSnapshot(db=db)

    # Before snapshot: degraded memory and high latency
    before = {
        "memory_utilisation_ratio": 0.96,
        "latency_p99_seconds": 0.450,
        "error_ratio": 0.04,
        "queue_depth": 35.0,
        "cpu_utilisation_ratio": 0.70,
    }

    # After snapshot: recovered memory and healthy metrics within SLO
    after = {
        "memory_utilisation_ratio": 0.42,
        "latency_p99_seconds": 0.080,
        "error_ratio": 0.001,
        "queue_depth": 0.0,
        "cpu_utilisation_ratio": 0.35,
    }

    recovered, reason, details = snapshot.verify_recovery(
        service="central-ledger",
        before=before,
        after=after,
        playbook="restart_on_oom",
        anomaly_kind="memory_pressure",
        container_healthy=True,
    )

    assert recovered is True, f"Expected recovery to succeed, failed with: {reason}"
    assert "verified healthy" in reason.lower()
    assert details["target_metric_improved"] is True


# ---------------------------------------------------------------------------
# Scenario 8: Failed Recovery Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_8_failed_recovery_verification():
    """Post-remediation metrics that still violate SLOs result in FAILED verification."""
    db = MockAsyncDB()
    snapshot = MetricSnapshot(db=db)

    # Before snapshot: memory leak
    before = {
        "memory_utilisation_ratio": 0.98,
        "latency_p99_seconds": 0.500,
        "error_ratio": 0.05,
        "queue_depth": 40.0,
        "cpu_utilisation_ratio": 0.88,
    }

    # After snapshot: restart failed to clear memory (persisted high)
    after = {
        "memory_utilisation_ratio": 0.97,
        "latency_p99_seconds": 0.480,
        "error_ratio": 0.05,
        "queue_depth": 38.0,
        "cpu_utilisation_ratio": 0.85,
    }

    recovered, reason, details = snapshot.verify_recovery(
        service="central-ledger",
        before=before,
        after=after,
        playbook="restart_on_oom",
        anomaly_kind="memory_pressure",
        container_healthy=True,
    )

    assert recovered is False, "Expected recovery to fail due to persisted saturation"
    assert "persisted" in reason.lower() or "failed" in reason.lower()


# ---------------------------------------------------------------------------
# Scenario 9: Incident Resolution Integrity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_9_incident_resolution_integrity(mock_bus):
    """Incident remains open/mitigating if recovery fails; resolves only after confirmed recovery."""
    db = MockAsyncDB()
    incident_mgr = IncidentManager(db=db, bus=mock_bus)

    # Test 9A: When metrics are still unhealthy, auto-resolve MUST NOT close the incident
    db.mock_fetch_all_result = [
        {
            "incident_id": "inc_ongoing_1",
            "anomaly_ids": ["anom-1"],
            "affected_services": ["central-ledger"],
            "opened_at": datetime.now(UTC) - timedelta(minutes=10),
        }
    ]

    # Mock anomaly age > 300s, but service metrics still degraded (error_ratio 0.08)
    def mock_fetch_one(sql, params=None):
        if "MAX(detected_at)" in sql:
            return {"latest_detection": datetime.now(UTC) - timedelta(minutes=6)}
        if "healing_actions" in sql:
            return {"status": "failed", "result_message": "Recovery verification failed"}
        if "service_metrics" in sql:
            return {"avg_error": 0.08, "avg_p99": 0.65, "avg_queue": 25.0}
        return None

    db.fetch_one = AsyncMock(side_effect=mock_fetch_one)

    await incident_mgr.check_auto_resolve()

    # Verify that resolve_incident was NOT called
    resolve_queries = [q for q in db.executed_queries if "SET status = 'resolved'" in q[0]]
    assert len(resolve_queries) == 0, "Incident should NOT resolve when metrics are unhealthy or action failed"

    # Test 9B: When metrics are verified healthy and quiet, incident resolves cleanly
    def mock_fetch_one_healthy(sql, params=None):
        if "MAX(detected_at)" in sql:
            return {"latest_detection": datetime.now(UTC) - timedelta(minutes=6)}
        if "healing_actions" in sql:
            return {"status": "succeeded", "result_message": "Remediation verified"}
        if "service_metrics" in sql:
            return {"avg_error": 0.001, "avg_p99": 0.05, "avg_queue": 0.0}
        return None

    db.fetch_one = AsyncMock(side_effect=mock_fetch_one_healthy)
    await incident_mgr.check_auto_resolve()

    resolve_queries = [q for q in db.executed_queries if "SET status = 'resolved'" in q[0]]
    assert len(resolve_queries) == 1, "Incident should resolve once recovery is verified"


# ---------------------------------------------------------------------------
# Scenario 10: Traffic Overload Selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_10_traffic_overload_selects_scale_out():
    """When telemetry shows incoming TPS near or above capacity, engine selects scale-out (+1)."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-traffic-surge-1",
        detected_at=datetime.now(UTC),
        service="quoting-service",
        severity=Severity.CRITICAL,
        score=0.92,
        kind=AnomalyKind.QUEUE_BACKLOG,
    )
    # Incoming TPS is 35, while capacity is 30 -> 116% load
    snapshot = {
        "tps": 35.0,
        "capacity_tps": 30.0,
        "queue_depth": 18.0,
        "cpu_utilisation_ratio": 0.85,
        "memory_utilisation_ratio": 0.45,
        "error_ratio": 0.0,
    }

    playbook, params = PlaybookRegistry.select_playbook(
        anomaly=anomaly,
        incident=None,
        snapshot=snapshot,
        previous_actions=None,
    )

    assert playbook == PlaybookName.SCALE_OUT_PREEMPTIVE
    assert params.get("target_replicas") == "+1"
    assert params.get("root_cause") == "traffic_overload"


# ---------------------------------------------------------------------------
# Scenario 11: Persistent Queue Backlog Prevents Repeated Restarts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_11_persistent_queue_backlog_escalates_from_failed_restart():
    """When a prior restart failed to clear queue, engine escalates to scale-out instead of repeating restart."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-queue-persist-1",
        detected_at=datetime.now(UTC),
        service="account-lookup-service",
        severity=Severity.WARNING,
        score=0.80,
        kind=AnomalyKind.QUEUE_BACKLOG,
    )
    # Traffic is moderate (15 TPS on 40 TPS capacity), but queue is stuck at 25
    snapshot = {
        "tps": 15.0,
        "capacity_tps": 40.0,
        "queue_depth": 25.0,
        "cpu_utilisation_ratio": 0.35,
        "memory_utilisation_ratio": 0.40,
        "error_ratio": 0.0,
    }

    # Attempt 1: First time, select clear_queue_backlog
    playbook_1, params_1 = PlaybookRegistry.select_playbook(
        anomaly=anomaly,
        incident=None,
        snapshot=snapshot,
        previous_actions=[],
    )
    assert playbook_1 == PlaybookName.CLEAR_QUEUE_BACKLOG

    # Attempt 2: After restart failed, do NOT repeat restart -> escalate to scale_out_preemptive
    prior_actions = [
        {
            "action_id": "act-prior-1",
            "playbook": PlaybookName.CLEAR_QUEUE_BACKLOG,
            "status": "failed",
            "result_message": "Recovery failed: Queue backlog persisted",
        }
    ]
    playbook_2, params_2 = PlaybookRegistry.select_playbook(
        anomaly=anomaly,
        incident=None,
        snapshot=snapshot,
        previous_actions=prior_actions,
    )
    assert playbook_2 == PlaybookName.SCALE_OUT_PREEMPTIVE
    assert params_2.get("target_replicas") == "+1"
    assert params_2.get("root_cause") == "persistent_queue_backlog_escalated"


# ---------------------------------------------------------------------------
# Scenario 12: Downstream Dependency Failure Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_12_downstream_dependency_failure_identified():
    """Identifies when downstream service failure causes upstream anomalies, avoiding blind upstream restart."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-upstream-1",
        detected_at=datetime.now(UTC),
        service="ml-api-adapter",
        severity=Severity.CRITICAL,
        score=0.88,
        kind=AnomalyKind.LATENCY_SPIKE,
    )
    incident = Incident(
        incident_id="inc-downstream-cascade",
        opened_at=datetime.now(UTC),
        status="open",
        severity=Severity.CRITICAL,
        affected_services=["ml-api-adapter", "central-ledger"],
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.SERVICE,
                target="central-ledger",
                confidence=0.90,
                summary="Service central-ledger failure cascading downstream",
            )
        ],
    )

    playbook, params = PlaybookRegistry.select_playbook(
        anomaly=anomaly,
        incident=incident,
        snapshot={"tps": 20.0, "capacity_tps": 25.0, "queue_depth": 10.0},
        previous_actions=None,
    )

    assert params.get("root_cause") == "downstream_dependency_failure"
    assert params.get("downstream_target") == "central-ledger"


# ---------------------------------------------------------------------------
# Scenario 13: Insufficient Host Capacity Skips Scale-Out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_13_insufficient_capacity_skips_scale_out(monkeypatch):
    """When host RAM or CPU is insufficient, scale-out is safely skipped."""
    from healing_engine.executor import DockerExecutor

    monkeypatch.setenv("HEALING_ENABLED", "true")
    client = MagicMock()
    settings = HealingSettings(HEALING_ENABLED=True)
    executor = DockerExecutor(client=client, settings=settings)

    with patch.object(executor, "get_replica_count", AsyncMock(return_value=1)), \
         patch("healing_engine.executor.check_resource_capacity", return_value=(False, "Host RAM buffer exhausted", {})):
        result = await executor.scale_service("account-lookup-service", replicas=2)
        assert result.success is False
        assert result.metadata.get("status") == ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY
        assert "insufficient" in result.message.lower()


# ---------------------------------------------------------------------------
# Scenario 14: Global Container Limit (20 Containers) Enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_14_global_container_limit_enforced(monkeypatch):
    """Scale-out is strictly blocked if the host reaches the 20-container global limit."""
    from healing_engine.executor import DockerExecutor

    monkeypatch.setenv("HEALING_ENABLED", "true")
    client = MagicMock()
    # Mock 20 running containers on the Docker host
    mock_containers = [MagicMock() for _ in range(20)]
    client.containers.list.return_value = mock_containers

    settings = HealingSettings(
        HEALING_ENABLED=True,
        global_max_containers=20,
    )
    executor = DockerExecutor(client=client, settings=settings)

    with patch.object(executor, "get_replica_count", AsyncMock(return_value=1)):
        result = await executor.scale_service("quoting-service", replicas=2)
        assert result.success is False
        assert result.metadata.get("status") == ActionStatus.SKIPPED_GLOBAL_LIMIT
        assert "global container limit" in result.message.lower()


# ---------------------------------------------------------------------------
# Scenario 15: Cooldown Recorded on Remediation Failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_15_cooldown_recorded_on_failure(mock_bus):
    """Failed actions record failure cooldown in Redis, preventing rapid restart loops."""
    settings = HealingSettings(
        HEALING_ENABLED=True,
        failure_cooldown_seconds=120,
    )
    safety = SafetyRails(bus=mock_bus, settings=settings)

    await safety.record_action_executed(
        target_service="central-ledger",
        success=False,
        incident_id="inc-test-1",
        playbook=PlaybookName.CLEAR_QUEUE_BACKLOG,
    )

    # Verify set_flag was called with failure_cooldown_seconds TTL (120s)
    mock_bus.set_flag.assert_any_call(
        "aiops:healing:cooldown:central-ledger",
        value="1",
        ttl_seconds=120,
    )


# ---------------------------------------------------------------------------
# Scenario 16: Remediation Attempt Limit Enforced (Max 2 Attempts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_16_attempt_limit_enforced(mock_bus, monkeypatch):
    """When an incident reaches 2 remediation attempts, automatic remediation stops with manual intervention."""
    monkeypatch.setenv("HEALING_ENABLED", "true")
    settings = HealingSettings(
        HEALING_ENABLED=True,
        max_attempts_per_incident=2,
    )
    safety = SafetyRails(bus=mock_bus, settings=settings)

    # Mock Redis returning 2 attempts already recorded for this incident
    mock_bus.get_flag.return_value = "2"

    allowed, status, reason = await safety.check_all(
        target_service="central-ledger",
        confidence=0.85,
        incident_id="inc-exhausted-attempts",
        playbook=PlaybookName.CLEAR_QUEUE_BACKLOG,
    )

    assert allowed is False
    assert status == ActionStatus.SKIPPED_MAX_ATTEMPTS
    assert "maximum remediation attempts (2) reached" in reason.lower()
    assert "manual intervention required" in reason.lower()
