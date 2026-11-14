"""Self-healing engine behaviour (module 6).

Tests cover:
1. Safety rails:
   * Master switch: HEALING_ENABLED=false blocks all actions as skipped_dry_run
   * Confidence threshold: Actions below min_confidence are skipped_low_confidence
   * Per-target cooldown: Recent actions on same service blocked as skipped_cooldown
   * Hourly budget: Exceeding max_actions_per_hour blocked as skipped_budget

2. Playbook selection:
   * Memory pressure -> restart_on_oom
   * Traffic surge -> scale_out_preemptive
   * Deploy change hypothesis -> rollback_bad_deploy
   * Cascade failure -> circuit_break_cascade
   * Queue backlog -> clear_queue_backlog

3. Real Docker Compose scale-out and scale-in with resource-aware simulation:
   * Real scale-out from 1 to 2 replicas
   * Real scale-out from 2 to 3 replicas
   * Maximum replica limit (capped at max_replicas)
   * Insufficient RAM fallback (skipped_insufficient_capacity)
   * Insufficient CPU fallback (skipped_insufficient_capacity)
   * Real scale-in from 3 to 2 replicas
   * Real scale-in from 2 to 1 replica
   * Minimum replica limit (cannot scale below min_replicas)
   * Simulation does not create or remove containers
   * Safety rails block scaling
   * Cooldown prevents repeated scaling
   * Failed Docker scaling is correctly reported
   * Actual and simulated replica counts are not mixed
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiops_common.bus import EventBus
from aiops_common.config import HealingSettings
from aiops_common.constants import (
    HEALING_BUDGET_KEY,
    HEALING_COOLDOWN_KEY,
    ActionStatus,
    AnomalyKind,
    ExecutionMode,
    PlaybookName,
    RootCauseCategory,
)
from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis, Severity
from healing_engine.executor import DockerExecutor
from healing_engine.playbooks import PlaybookRegistry
from healing_engine.safety import SafetyRails


@pytest.fixture
def healing_settings():
    """Default healing settings for tests with healing enabled."""
    return HealingSettings(
        HEALING_ENABLED=True,
        max_actions_per_hour=10,
        cooldown_seconds=300,
        min_confidence=0.7,
        min_replicas=1,
        max_replicas=3,
        replica_memory_limit_mb=256.0,
        replica_cpu_limit=0.4,
        min_available_ram_mb=256.0,
        max_host_cpu_percent=85.0,
    )


@pytest.fixture
def mock_bus():
    """Mock event bus for Redis operations."""
    bus = MagicMock(spec=EventBus)
    bus.get_flag = AsyncMock(return_value=None)
    bus.set_flag = AsyncMock()
    bus.incr_with_ttl = AsyncMock(return_value=1)
    bus.client = MagicMock()
    bus.client.decr = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# Safety Rails Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_master_switch_blocks_when_disabled(mock_bus):
    """Master switch: HEALING_ENABLED=false blocks actions as skipped_dry_run."""
    settings = HealingSettings(HEALING_ENABLED=False)
    safety = SafetyRails(mock_bus, settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert not allowed, "Actions should be blocked when healing disabled"
    assert status == ActionStatus.SKIPPED_DRY_RUN
    assert "HEALING_ENABLED=false" in reason
    assert "dry-run" in reason.lower()


@pytest.mark.asyncio
async def test_safety_master_switch_allows_when_enabled(mock_bus, healing_settings):
    """Master switch: HEALING_ENABLED=true allows actions (if other checks pass)."""
    safety = SafetyRails(mock_bus, healing_settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert allowed, "Actions should be allowed when all checks pass"
    assert status == ActionStatus.PROPOSED
    assert reason == ""


@pytest.mark.asyncio
async def test_safety_confidence_threshold_blocks_low_confidence(mock_bus, healing_settings):
    """Confidence threshold: Below min_confidence blocks as skipped_low_confidence."""
    safety = SafetyRails(mock_bus, healing_settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.6)

    assert not allowed, "Low confidence actions should be blocked"
    assert status == ActionStatus.SKIPPED_LOW_CONFIDENCE
    assert "0.6" in reason or "0.60" in reason
    assert "0.7" in reason or "0.70" in reason


@pytest.mark.asyncio
async def test_safety_confidence_threshold_allows_high_confidence(mock_bus, healing_settings):
    """Confidence threshold: Above min_confidence allows action."""
    safety = SafetyRails(mock_bus, healing_settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.85)

    assert allowed, "High confidence actions should be allowed"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_cooldown_blocks_recent_service(mock_bus, healing_settings):
    """Cooldown: Service in cooldown period is blocked as skipped_cooldown."""
    safety = SafetyRails(mock_bus, healing_settings)
    mock_bus.get_flag = AsyncMock(return_value="1")

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert not allowed, "Actions should be blocked during cooldown"
    assert status == ActionStatus.SKIPPED_COOLDOWN
    assert "cooldown" in reason.lower()
    assert "300" in reason


@pytest.mark.asyncio
async def test_safety_cooldown_allows_after_expiry(mock_bus, healing_settings):
    """Cooldown: Service outside cooldown period is allowed."""
    safety = SafetyRails(mock_bus, healing_settings)
    mock_bus.get_flag = AsyncMock(return_value=None)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert allowed, "Actions should be allowed after cooldown expires"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_budget_blocks_when_exhausted(mock_bus, healing_settings):
    """Hourly budget: Exceeding max_actions_per_hour blocks as skipped_budget."""
    safety = SafetyRails(mock_bus, healing_settings)
    mock_bus.incr_with_ttl = AsyncMock(return_value=11)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert not allowed, "Actions should be blocked when budget exhausted"
    assert status == ActionStatus.SKIPPED_BUDGET
    assert "budget" in reason.lower()
    # The probe returned 11; the message reports actions actually used (10 of 10).
    assert "(10/10)" in reason


@pytest.mark.asyncio
async def test_safety_budget_allows_within_limit(mock_bus, healing_settings):
    """Hourly budget: Within max_actions_per_hour allows action."""
    safety = SafetyRails(mock_bus, healing_settings)
    mock_bus.incr_with_ttl = AsyncMock(return_value=5)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert allowed, "Actions should be allowed within budget"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_record_action_sets_cooldown_and_budget(mock_bus, healing_settings):
    """Recording an action sets cooldown flag and increments budget."""
    safety = SafetyRails(mock_bus, healing_settings)

    await safety.record_action_executed("central-ledger")

    mock_bus.set_flag.assert_called_once()
    call_args = mock_bus.set_flag.call_args
    assert HEALING_COOLDOWN_KEY in call_args[0][0]
    assert "central-ledger" in call_args[0][0]
    assert call_args[1]["ttl_seconds"] == 300

    mock_bus.incr_with_ttl.assert_called_once()
    budget_call = mock_bus.incr_with_ttl.call_args
    assert budget_call[0][0] == HEALING_BUDGET_KEY
    assert budget_call[1]["ttl_seconds"] == 3600


# ---------------------------------------------------------------------------
# Playbook Selection Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playbook_selection_memory_pressure_maps_to_restart():
    """Memory pressure anomaly selects restart_on_oom playbook."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        severity=Severity.CRITICAL,
        score=0.92,
        contributing_features=["memory_utilisation_ratio"],
        kind=AnomalyKind.MEMORY_PRESSURE,
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly)

    assert playbook == PlaybookName.RESTART_ON_OOM
    assert "anomaly_kind" in params
    assert params["anomaly_kind"] == AnomalyKind.MEMORY_PRESSURE


@pytest.mark.asyncio
async def test_playbook_selection_traffic_surge_maps_to_scale_out():
    """Traffic surge anomaly selects scale_out_preemptive playbook."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        severity=Severity.WARNING,
        score=0.78,
        contributing_features=["tps"],
        kind=AnomalyKind.TRAFFIC_SURGE,
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly)

    assert playbook == PlaybookName.SCALE_OUT_PREEMPTIVE
    assert params["target_replicas"] == "+1"


@pytest.mark.asyncio
async def test_playbook_selection_change_hypothesis_maps_to_rollback():
    """Incident with CHANGE hypothesis overrides anomaly and selects rollback."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        severity=Severity.CRITICAL,
        score=0.92,
        contributing_features=["error_ratio"],
        kind=AnomalyKind.ERROR_BURST,
    )

    incident = Incident(
        opened_at=datetime.now(UTC),
        status="open",
        severity=Severity.CRITICAL,
        platform="mojaloop",
        title="Error burst after deployment",
        affected_services=["central-ledger"],
        anomaly_ids=["anom-1"],
        hypotheses=[
            RootCauseHypothesis(
                target="central-ledger",
                category=RootCauseCategory.CHANGE,
                confidence=0.85,
                summary="Deployment to v1.2.3 2 minutes before error burst",
                evidence=["Deployment at T-2min", "Error rate 0% → 25%"],
            )
        ],
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly, incident)

    assert playbook == PlaybookName.ROLLBACK_BAD_DEPLOY


@pytest.mark.asyncio
async def test_playbook_selection_cascade_hypothesis_maps_to_circuit_breaker():
    """Incident with cascade SERVICE hypothesis selects circuit_break_cascade."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="account-lookup-service",
        severity=Severity.CRITICAL,
        score=0.90,
        contributing_features=["error_ratio"],
        kind=AnomalyKind.ERROR_BURST,
    )

    incident = Incident(
        opened_at=datetime.now(UTC),
        status="open",
        severity=Severity.CRITICAL,
        platform="mojaloop",
        title="Service cascade from central-ledger",
        affected_services=["central-ledger", "account-lookup-service"],
        anomaly_ids=["anom-1"],
        hypotheses=[
            RootCauseHypothesis(
                target="central-ledger",
                category=RootCauseCategory.SERVICE,
                confidence=0.88,
                summary="Cascade from central-ledger to downstream services",
                evidence=[
                    "central-ledger failed at T",
                    "account-lookup-service errors at T+45s",
                ],
            )
        ],
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly, incident)

    assert playbook == PlaybookName.CIRCUIT_BREAK_CASCADE


@pytest.mark.asyncio
async def test_playbook_selection_queue_backlog_maps_to_clear_queue():
    """Queue backlog anomaly selects clear_queue_backlog playbook."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        severity=Severity.WARNING,
        score=0.82,
        contributing_features=["queue_depth"],
        kind=AnomalyKind.QUEUE_BACKLOG,
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly)

    assert playbook == PlaybookName.CLEAR_QUEUE_BACKLOG


# ---------------------------------------------------------------------------
# Real Docker Compose Scale-Out & Scale-In Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Mock DockerClient that avoids real socket connections."""
    client = MagicMock()
    client.ping.return_value = True
    return client


@pytest.mark.asyncio
async def test_scale_out_real_1_to_2(mock_docker_client, healing_settings):
    """Test 1: Real scale-out from 1 to 2 replicas when capacity is available."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=1)

    executor.scaler.scale_out = AsyncMock(
        return_value=(
            True,
            "Successfully scaled 'central-ledger' from 1 to 2 replicas (new: aiops-central-ledger-2)",
            {
                "actual_replicas_before": 1,
                "actual_replicas_after": 2,
                "container_id": "c-123",
                "container_name": "aiops-central-ledger-2",
            },
        )
    )

    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(
            True,
            "Sufficient headroom",
            {"available_ram_mb": 4096.0, "cpu_percent": 15.0},
        ),
    ):
        result = await executor.scale_service("central-ledger", replicas=2)

    assert result.success is True
    assert result.metadata["execution_mode"] == ExecutionMode.REAL
    assert result.metadata["status"] == ActionStatus.SUCCEEDED
    assert result.metadata["actual_replicas_before"] == 1
    assert result.metadata["actual_replicas_after"] == 2
    assert "aiops-central-ledger-2" in result.message


@pytest.mark.asyncio
async def test_scale_out_real_2_to_3(mock_docker_client, healing_settings):
    """Test 2: Real scale-out from 2 to 3 replicas."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=2)

    executor.scaler.scale_out = AsyncMock(
        return_value=(
            True,
            "Successfully scaled 'central-ledger' from 2 to 3 replicas",
            {
                "actual_replicas_before": 2,
                "actual_replicas_after": 3,
                "container_id": "c-456",
                "container_name": "aiops-central-ledger-3",
            },
        )
    )

    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(
            True,
            "Sufficient headroom",
            {"available_ram_mb": 3000.0, "cpu_percent": 25.0},
        ),
    ):
        result = await executor.scale_service("central-ledger", replicas=3)

    assert result.success is True
    assert result.metadata["execution_mode"] == ExecutionMode.REAL
    assert result.metadata["status"] == ActionStatus.SUCCEEDED
    assert result.metadata["actual_replicas_before"] == 2
    assert result.metadata["actual_replicas_after"] == 3


@pytest.mark.asyncio
async def test_scale_out_max_replica_limit(mock_docker_client, healing_settings):
    """Test 3: Maximum replica limit prevents scaling beyond MAX_REPLICAS=3."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=3)
    executor.scaler.scale_out = AsyncMock()

    result = await executor.scale_service("central-ledger", replicas=4)

    assert result.success is False
    assert result.metadata["status"] == ActionStatus.SKIPPED_MAX_REPLICAS
    assert result.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result.metadata["actual_replicas_before"] == 3
    assert result.metadata["actual_replicas_after"] == 3
    assert result.metadata["simulated_replicas_after"] == 4
    assert "maximum replicas (3)" in result.message
    executor.scaler.scale_out.assert_not_called()


@pytest.mark.asyncio
async def test_scale_out_insufficient_ram_fallback(mock_docker_client, healing_settings):
    """Test 4: Insufficient host RAM triggers simulation fallback with skipped_insufficient_capacity."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.scaler.scale_out = AsyncMock()

    ram_metrics = {"available_ram_mb": 120.0, "required_ram_mb": 256.0, "cpu_percent": 20.0}
    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(
            False,
            "Available RAM 120.0 MB is below requirement 256.0 MB",
            ram_metrics,
        ),
    ):
        result = await executor.scale_service("central-ledger", replicas=2)

    assert result.success is False
    assert result.metadata["status"] == ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY
    assert result.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result.metadata["actual_replicas_before"] == 1
    assert result.metadata["actual_replicas_after"] == 1
    assert result.metadata["simulated_replicas_after"] == 2
    assert "Insufficient host capacity" in result.message
    executor.scaler.scale_out.assert_not_called()


@pytest.mark.asyncio
async def test_scale_out_insufficient_cpu_fallback(mock_docker_client, healing_settings):
    """Test 5: Insufficient host CPU triggers simulation fallback."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=2)
    executor.scaler.scale_out = AsyncMock()

    cpu_metrics = {"available_ram_mb": 2048.0, "cpu_percent": 92.5, "max_cpu_percent": 85.0}
    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(False, "Host CPU usage 92.5% exceeds threshold 85.0%", cpu_metrics),
    ):
        result = await executor.scale_service("central-ledger", replicas=3)

    assert result.success is False
    assert result.metadata["status"] == ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY
    assert result.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result.metadata["actual_replicas_before"] == 2
    assert result.metadata["actual_replicas_after"] == 2
    assert result.metadata["simulated_replicas_after"] == 3
    executor.scaler.scale_out.assert_not_called()


@pytest.mark.asyncio
async def test_scale_in_real_3_to_2(mock_docker_client, healing_settings):
    """Test 6: Real scale-in from 3 to 2 replicas."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=3)

    executor.scaler.scale_in = AsyncMock(
        return_value=(
            True,
            "Successfully scaled in 'central-ledger' from 3 to 2 replicas (removed aiops-central-ledger-3)",
            {
                "actual_replicas_before": 3,
                "actual_replicas_after": 2,
                "removed_container_name": "aiops-central-ledger-3",
            },
        )
    )

    result = await executor.scale_service("central-ledger", replicas=2)

    assert result.success is True
    assert result.metadata["execution_mode"] == ExecutionMode.REAL
    assert result.metadata["status"] == ActionStatus.SUCCEEDED
    assert result.metadata["actual_replicas_before"] == 3
    assert result.metadata["actual_replicas_after"] == 2
    assert "aiops-central-ledger-3" in result.message


@pytest.mark.asyncio
async def test_scale_in_real_2_to_1(mock_docker_client, healing_settings):
    """Test 7: Real scale-in from 2 to 1 replica."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=2)

    executor.scaler.scale_in = AsyncMock(
        return_value=(
            True,
            "Successfully scaled in 'central-ledger' from 2 to 1 replicas (removed aiops-central-ledger-2)",
            {
                "actual_replicas_before": 2,
                "actual_replicas_after": 1,
                "removed_container_name": "aiops-central-ledger-2",
            },
        )
    )

    result = await executor.scale_service("central-ledger", replicas=1)

    assert result.success is True
    assert result.metadata["execution_mode"] == ExecutionMode.REAL
    assert result.metadata["status"] == ActionStatus.SUCCEEDED
    assert result.metadata["actual_replicas_before"] == 2
    assert result.metadata["actual_replicas_after"] == 1


@pytest.mark.asyncio
async def test_scale_in_min_replica_limit(mock_docker_client, healing_settings):
    """Test 8: Minimum replica limit prevents scaling below MIN_REPLICAS=1."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.scaler.scale_in = AsyncMock()

    result = await executor.scale_service("central-ledger", replicas=0)

    assert result.success is False
    assert result.metadata["status"] == ActionStatus.SKIPPED_MIN_REPLICAS
    assert result.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result.metadata["actual_replicas_before"] == 1
    assert result.metadata["actual_replicas_after"] == 1
    assert result.metadata["simulated_replicas_after"] == 1
    assert "minimum replicas (1)" in result.message
    executor.scaler.scale_in.assert_not_called()


@pytest.mark.asyncio
async def test_simulation_does_not_modify_containers(mock_docker_client):
    """Test 9: In simulation/dry-run mode, no Docker containers are created or removed."""
    dry_run_settings = HealingSettings(HEALING_ENABLED=False)
    executor = DockerExecutor(client=mock_docker_client, settings=dry_run_settings)
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.scaler.scale_out = AsyncMock()
    executor.scaler.scale_in = AsyncMock()

    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(True, "Capacity OK", {}),
    ):
        result_out = await executor.scale_service("central-ledger", replicas=2)

    assert result_out.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result_out.metadata["status"] == ActionStatus.SKIPPED_DRY_RUN
    assert result_out.metadata["actual_replicas_before"] == 1
    assert result_out.metadata["actual_replicas_after"] == 1
    assert result_out.metadata["simulated_replicas_after"] == 2
    executor.scaler.scale_out.assert_not_called()

    # Now test scale-in simulation
    executor.get_replica_count = AsyncMock(return_value=2)
    result_in = await executor.scale_service("central-ledger", replicas=1)

    assert result_in.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result_in.metadata["status"] == ActionStatus.SKIPPED_DRY_RUN
    assert result_in.metadata["actual_replicas_before"] == 2
    assert result_in.metadata["actual_replicas_after"] == 2
    assert result_in.metadata["simulated_replicas_after"] == 1
    executor.scaler.scale_in.assert_not_called()


@pytest.mark.asyncio
async def test_safety_rails_block_scaling(mock_bus):
    """Test 10: SafetyRails block scaling before execution if dry-run or low confidence."""
    dry_run_settings = HealingSettings(HEALING_ENABLED=False)
    safety = SafetyRails(mock_bus, dry_run_settings)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.95)
    assert not allowed
    assert status == ActionStatus.SKIPPED_DRY_RUN


@pytest.mark.asyncio
async def test_cooldown_prevents_repeated_scaling(mock_bus, healing_settings):
    """Test 11: Cooldown period blocks repeated scaling on the same service."""
    safety = SafetyRails(mock_bus, healing_settings)
    mock_bus.get_flag = AsyncMock(return_value="1")  # Flag set indicates active cooldown

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.95)
    assert not allowed
    assert status == ActionStatus.SKIPPED_COOLDOWN


@pytest.mark.asyncio
async def test_docker_scaling_failure_reported(mock_docker_client, healing_settings):
    """Test 12: Failed Docker scaling operation is correctly caught and reported as failed."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=1)

    executor.scaler.scale_out = AsyncMock(
        return_value=(
            False,
            "Failed to start container: port already in use",
            {"actual_replicas_before": 1, "actual_replicas_after": 1},
        )
    )

    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(True, "Capacity OK", {}),
    ):
        result = await executor.scale_service("central-ledger", replicas=2)

    assert result.success is False
    assert result.metadata["status"] == ActionStatus.FAILED
    assert result.metadata["execution_mode"] == ExecutionMode.REAL
    assert result.metadata["actual_replicas_after"] == 1
    assert "port already in use" in result.message


@pytest.mark.asyncio
async def test_actual_and_simulated_replicas_not_mixed(mock_docker_client, healing_settings):
    """Test 13: Actual and simulated replica counts are distinct and preserved in metadata."""
    executor = DockerExecutor(client=mock_docker_client, settings=healing_settings)
    executor.get_replica_count = AsyncMock(return_value=2)
    executor.scaler.scale_out = AsyncMock()

    with patch(
        "healing_engine.executor.check_resource_capacity",
        return_value=(False, "RAM constrained", {}),
    ):
        result = await executor.scale_service("central-ledger", replicas=3)

    # Verify fields are distinct and not mixed
    assert result.metadata["actual_replicas_before"] == 2
    assert result.metadata["actual_replicas_after"] == 2  # Unchanged!
    assert result.metadata["simulated_replicas_after"] == 3  # Intended target!
    assert result.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert result.metadata["status"] == ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY
    executor.scaler.scale_out.assert_not_called()


async def test_concurrent_checks_on_one_service_let_exactly_one_act(healing_settings):
    """Regression: an anomaly and the incident it opened were processed concurrently, both
    read 'no cooldown', and both restarted the same service within a second."""
    import asyncio

    store: dict[str, str] = {}

    class NxBus:
        class client:
            @staticmethod
            async def get(key):
                return None

            @staticmethod
            async def decr(key):
                return 0

        async def get_flag(self, key):
            return store.get(key)

        async def claim_flag(self, key, value="1", *, ttl_seconds):
            await asyncio.sleep(0)  # let the other check interleave
            if key in store:
                return False
            store[key] = value
            return True

        async def set_flag(self, key, value="1", *, ttl_seconds=None):
            store[key] = value

        async def incr_with_ttl(self, key, *, ttl_seconds):
            return 1

    safety = SafetyRails(NxBus(), healing_settings)
    results = await asyncio.gather(
        safety.check_all("central-ledger", 0.9, incident_id=None),
        safety.check_all("central-ledger", 0.9, incident_id="inc_1"),
    )
    allowed = [ok for ok, _status, _reason in results]
    assert allowed.count(True) == 1
    blocked = next(r for r in results if not r[0])
    assert blocked[1] == ActionStatus.SKIPPED_COOLDOWN
    # A different service is not affected by the claim.
    ok, _, _ = await safety.check_all("quoting-service", 0.9, incident_id=None)
    assert ok is True


async def test_blocked_attempts_do_not_consume_budget(healing_settings):
    """Regression: each attempt blocked by the budget still incremented the counter
    (observed 45/10 with 10 executed), so the budget could never recover."""
    store = {"n": healing_settings.max_actions_per_hour}  # budget exactly used up

    class CountingBus:
        class client:
            @staticmethod
            async def decr(key):
                store["n"] -= 1

        async def incr_with_ttl(self, key, *, ttl_seconds):
            store["n"] += 1
            return store["n"]

    safety = SafetyRails(CountingBus(), healing_settings)
    for _ in range(5):
        over, reason = await safety._check_budget()
        assert over is True
    assert store["n"] == healing_settings.max_actions_per_hour  # unchanged by blocked checks
    assert f"({healing_settings.max_actions_per_hour}/" in reason


@pytest.mark.asyncio
async def test_attempt_cap_escalates_once_per_incident() -> None:
    """Regression: every anomaly after the cap wrote another skipped_max_attempts row (37
    in five minutes for one incident). The escalation is recorded once."""
    from healing_engine.engine import HealingEngine

    flags: set[str] = set()

    class Bus:
        async def claim_flag(self, key: str, value: str, *, ttl_seconds: int) -> bool:
            if key in flags:
                return False
            flags.add(key)
            return True

    engine = object.__new__(HealingEngine)
    engine.bus = Bus()

    assert await engine._first_escalation("inc_1") is True
    assert await engine._first_escalation("inc_1") is False
    assert await engine._first_escalation("inc_2") is True
