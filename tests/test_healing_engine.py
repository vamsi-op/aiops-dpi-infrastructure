"""Self-healing engine behaviour (module 6).

Tests focus on the safety rails - the deliverable is proving the brakes work:

* **Master switch**: HEALING_ENABLED=false blocks all actions as skipped_dry_run
* **Confidence threshold**: Actions below min_confidence are skipped_low_confidence
* **Per-target cooldown**: Recent actions on same service blocked as skipped_cooldown
* **Hourly budget**: Exceeding max_actions_per_hour blocked as skipped_budget

Playbook selection and execution are tested with mocked Docker operations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from aiops_common.bus import EventBus
from aiops_common.config import HealingSettings
from aiops_common.constants import (
    HEALING_BUDGET_KEY,
    HEALING_COOLDOWN_KEY,
    ActionStatus,
    AnomalyKind,
    PlaybookName,
    RootCauseCategory,
)
from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis, Severity
from healing_engine.playbooks import PlaybookRegistry
from healing_engine.safety import SafetyRails


@pytest.fixture
def healing_settings():
    """Default healing settings for tests."""
    return HealingSettings(
        enabled=True,
        max_actions_per_hour=10,
        cooldown_seconds=300,
        min_confidence=0.7,
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


@pytest.mark.asyncio
async def test_safety_master_switch_blocks_when_disabled(mock_bus):
    """Master switch: HEALING_ENABLED=false blocks actions as skipped_dry_run."""
    settings = HealingSettings(enabled=False)  # Disabled
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
async def test_safety_confidence_threshold_blocks_low_confidence(
    mock_bus, healing_settings
):
    """Confidence threshold: Below min_confidence blocks as skipped_low_confidence."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Confidence 0.6 below threshold of 0.7
    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.6)

    assert not allowed, "Low confidence actions should be blocked"
    assert status == ActionStatus.SKIPPED_LOW_CONFIDENCE
    assert "0.6" in reason or "0.60" in reason
    assert "0.7" in reason or "0.70" in reason


@pytest.mark.asyncio
async def test_safety_confidence_threshold_allows_high_confidence(
    mock_bus, healing_settings
):
    """Confidence threshold: Above min_confidence allows action."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Confidence 0.85 above threshold of 0.7
    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.85)

    assert allowed, "High confidence actions should be allowed"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_cooldown_blocks_recent_service(mock_bus, healing_settings):
    """Cooldown: Service in cooldown period is blocked as skipped_cooldown."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Mock cooldown flag exists
    mock_bus.get_flag = AsyncMock(return_value="1")

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert not allowed, "Actions should be blocked during cooldown"
    assert status == ActionStatus.SKIPPED_COOLDOWN
    assert "cooldown" in reason.lower()
    assert "300" in reason  # cooldown_seconds


@pytest.mark.asyncio
async def test_safety_cooldown_allows_after_expiry(mock_bus, healing_settings):
    """Cooldown: Service outside cooldown period is allowed."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Mock cooldown flag does not exist (expired)
    mock_bus.get_flag = AsyncMock(return_value=None)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert allowed, "Actions should be allowed after cooldown expires"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_budget_blocks_when_exhausted(mock_bus, healing_settings):
    """Hourly budget: Exceeding max_actions_per_hour blocks as skipped_budget."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Mock budget counter at 11 (over limit of 10)
    mock_bus.incr_with_ttl = AsyncMock(return_value=11)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert not allowed, "Actions should be blocked when budget exhausted"
    assert status == ActionStatus.SKIPPED_BUDGET
    assert "budget" in reason.lower()
    assert "11" in reason
    assert "10" in reason


@pytest.mark.asyncio
async def test_safety_budget_allows_within_limit(mock_bus, healing_settings):
    """Hourly budget: Within max_actions_per_hour allows action."""
    safety = SafetyRails(mock_bus, healing_settings)

    # Mock budget counter at 5 (under limit of 10)
    mock_bus.incr_with_ttl = AsyncMock(return_value=5)

    allowed, status, reason = await safety.check_all("central-ledger", confidence=0.9)

    assert allowed, "Actions should be allowed within budget"
    assert status == ActionStatus.PROPOSED


@pytest.mark.asyncio
async def test_safety_record_action_sets_cooldown_and_budget(mock_bus, healing_settings):
    """Recording an action sets cooldown flag and increments budget."""
    safety = SafetyRails(mock_bus, healing_settings)

    await safety.record_action_executed("central-ledger")

    # Verify cooldown flag was set
    mock_bus.set_flag.assert_called_once()
    call_args = mock_bus.set_flag.call_args
    assert HEALING_COOLDOWN_KEY in call_args[0][0]
    assert "central-ledger" in call_args[0][0]
    assert call_args[1]["ttl_seconds"] == 300

    # Verify budget was incremented
    mock_bus.incr_with_ttl.assert_called_once()
    budget_call = mock_bus.incr_with_ttl.call_args
    assert budget_call[0][0] == HEALING_BUDGET_KEY
    assert budget_call[1]["ttl_seconds"] == 3600


@pytest.mark.asyncio
async def test_playbook_selection_memory_pressure_maps_to_restart():
    """Memory pressure anomaly selects restart_on_oom playbook."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        metric_name="memory_utilisation_ratio",
        severity=Severity.CRITICAL,
        anomaly_score=0.92,
        contributing_features=["memory_utilisation_ratio"],
        predicted_kind=AnomalyKind.MEMORY_PRESSURE,
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
        metric_name="tps",
        severity=Severity.WARNING,
        anomaly_score=0.78,
        contributing_features=["tps"],
        predicted_kind=AnomalyKind.TRAFFIC_SURGE,
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
        metric_name="error_ratio",
        severity=Severity.CRITICAL,
        anomaly_score=0.92,
        contributing_features=["error_ratio"],
        predicted_kind=AnomalyKind.ERROR_BURST,
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
                category=RootCauseCategory.CHANGE,
                confidence=0.85,
                summary="Deployment to v1.2.3 2 minutes before error burst",
                evidence=["Deployment at T-2min", "Error rate 0% → 25%"],
            )
        ],
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly, incident)

    assert (
        playbook == PlaybookName.ROLLBACK_BAD_DEPLOY
    ), "Change hypothesis should trigger rollback"


@pytest.mark.asyncio
async def test_playbook_selection_cascade_hypothesis_maps_to_circuit_breaker():
    """Incident with cascade SERVICE hypothesis selects circuit_break_cascade."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="account-lookup-service",
        metric_name="error_ratio",
        severity=Severity.CRITICAL,
        anomaly_score=0.90,
        contributing_features=["error_ratio"],
        predicted_kind=AnomalyKind.ERROR_BURST,
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
                category=RootCauseCategory.SERVICE,
                confidence=0.88,
                summary="Cascade from central-ledger to downstream services",
                evidence=["central-ledger failed at T", "account-lookup-service errors at T+45s"],
            )
        ],
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly, incident)

    assert (
        playbook == PlaybookName.CIRCUIT_BREAK_CASCADE
    ), "Cascade hypothesis should trigger circuit breaker"


@pytest.mark.asyncio
async def test_playbook_selection_queue_backlog_maps_to_clear_queue():
    """Queue backlog anomaly selects clear_queue_backlog playbook."""
    anomaly = AnomalyEvent(
        anomaly_id="anom-1",
        detected_at=datetime.now(UTC),
        service="central-ledger",
        metric_name="queue_depth",
        severity=Severity.WARNING,
        anomaly_score=0.82,
        contributing_features=["queue_depth"],
        predicted_kind=AnomalyKind.QUEUE_BACKLOG,
    )

    playbook, params = PlaybookRegistry.select_playbook(anomaly)

    assert playbook == PlaybookName.CLEAR_QUEUE_BACKLOG
