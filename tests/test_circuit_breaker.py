"""Cascading failures and circuit breakers in the simulator, and the playbook that trips them.

Pinned here:
* a degraded dependency cascades into its callers (latency and errors inherited)
* healthy dependencies, and dependencies never seen, contribute nothing
* a dependency that answered before and stops answering counts as down
* an open breaker stops the cascade at that hop (fail fast, small deferred backlog)
* open -> half-open -> closed when the probe finds the dependency healthy, re-open otherwise
* the healing playbook opens the breaker on every caller of the root cause, then restarts it
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from aiops_common.constants import MOJALOOP_DEPENDENCIES
from mojaloop_sim.dependencies import (
    DEFERRED_QUEUE_PER_OPEN_BREAKER,
    UNREACHABLE_LATENCY_SECONDS,
    BreakerState,
    DependencyMonitor,
)
from mojaloop_sim.profiles import get_profile

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CALLER = "quoting-service"
DEPS = tuple(MOJALOOP_DEPENDENCIES[CALLER])  # central-ledger, account-lookup-service
ROOT = "central-ledger"


def _payload(service: str, *, load: float = 0.5, extra_latency: float = 0.0, error: float | None = None):
    profile = get_profile(service)
    p50 = profile.base_latency_seconds * (1 + 0.35 * load) + extra_latency
    return {
        "state": {
            "load_factor": load,
            "latency_p50_seconds": p50,
            "error_ratio": profile.base_error_ratio if error is None else error,
        }
    }


def _monitor() -> DependencyMonitor:
    return DependencyMonitor(CALLER, DEPS, enabled=False)


def test_healthy_dependencies_contribute_nothing() -> None:
    monitor = _monitor()
    for dep in DEPS:
        monitor.observe(dep, _payload(dep), NOW)
    effects = monitor.effects()
    assert effects.extra_latency_seconds == 0
    assert effects.extra_error_ratio == 0
    assert effects.extra_queue_depth == 0


def test_degraded_dependency_cascades_into_the_caller() -> None:
    monitor = _monitor()
    monitor.observe(ROOT, _payload(ROOT, extra_latency=0.8, error=0.30), NOW)
    monitor.observe(DEPS[1], _payload(DEPS[1]), NOW)
    effects = monitor.effects()
    share = 1 / len(DEPS)
    assert effects.extra_latency_seconds == pytest.approx(share * 0.8, rel=0.01)
    assert effects.extra_error_ratio == pytest.approx(
        share * (0.30 - get_profile(ROOT).base_error_ratio), rel=0.01
    )


def test_never_seen_dependency_is_ignored_but_a_lost_one_is_down() -> None:
    monitor = _monitor()
    monitor.observe(ROOT, None, NOW)  # never answered: e.g. simulator run on its own
    assert monitor.effects().extra_latency_seconds == 0

    monitor.observe(ROOT, _payload(ROOT), NOW)  # answers once...
    monitor.observe(ROOT, None, NOW)  # ...then misses a poll: a blip, e.g. a restart
    assert monitor.effects().extra_latency_seconds == 0
    monitor.observe(ROOT, None, NOW)
    monitor.observe(ROOT, None, NOW)  # third miss in a row: down
    effects = monitor.effects()
    assert effects.extra_latency_seconds == pytest.approx(UNREACHABLE_LATENCY_SECONDS / len(DEPS))
    assert effects.extra_error_ratio == pytest.approx(1 / len(DEPS))


def test_open_breaker_stops_the_cascade_at_this_hop() -> None:
    monitor = _monitor()
    monitor.observe(ROOT, _payload(ROOT, extra_latency=0.8, error=0.30), NOW)
    monitor.breakers[ROOT].open(seconds=60, by="test", now=NOW)
    effects = monitor.effects()
    assert effects.extra_latency_seconds == 0
    assert effects.extra_error_ratio == 0
    assert effects.extra_queue_depth == DEFERRED_QUEUE_PER_OPEN_BREAKER


def test_breaker_half_opens_and_closes_when_the_probe_is_healthy() -> None:
    monitor = _monitor()
    breaker = monitor.breakers[ROOT]
    breaker.open(seconds=60, by="test", now=NOW)

    monitor.observe(ROOT, _payload(ROOT, extra_latency=0.8), NOW + timedelta(seconds=30))
    monitor.advance_breakers(NOW + timedelta(seconds=30))
    assert breaker.state is BreakerState.OPEN  # still inside the open period

    later = NOW + timedelta(seconds=61)
    monitor.observe(ROOT, _payload(ROOT, extra_latency=0.8), later)
    monitor.advance_breakers(later)
    assert breaker.state is BreakerState.OPEN  # probe failed: re-opened
    assert breaker.trips == 1

    healed = later + timedelta(seconds=61)
    monitor.observe(ROOT, _payload(ROOT), healed)
    monitor.advance_breakers(healed)
    assert breaker.state is BreakerState.CLOSED


async def test_playbook_opens_breakers_on_every_caller_then_restarts_root() -> None:
    from healing_engine.playbooks import PlaybookExecutor

    executor = AsyncMock()
    executor.restart_container = AsyncMock(return_value=(True, f"Restarted {ROOT}"))
    posts: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"state": "open"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            posts.append((url, json))
            return FakeResponse()

    playbooks = PlaybookExecutor(executor, http_client_factory=FakeClient)
    ok, message = await playbooks._circuit_break_cascade(ROOT)

    callers = sorted(s for s, deps in MOJALOOP_DEPENDENCIES.items() if ROOT in deps)
    assert ok is True
    assert sorted(url.split("//")[1].split(":")[0] for url, _ in posts) == callers
    assert all(url.endswith(f"/sim/circuits/{ROOT}") and body["action"] == "open" for url, body in posts)
    executor.restart_container.assert_awaited_once_with(ROOT)
    assert "breaker" in message.lower()


async def test_engine_runs_the_cascade_playbook_on_the_root_not_the_victim(monkeypatch) -> None:
    """Regression: the engine passed the anomalous service (a cascade *victim*) to
    circuit_break_cascade, which would trip breakers on the wrong edges and restart a
    healthy service; the safety claim was keyed on the victim too."""
    from unittest.mock import MagicMock

    import healing_engine.engine as engine_mod
    from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.engine import HealingEngine

    monkeypatch.setattr(engine_mod.asyncio, "sleep", AsyncMock())
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    safety = MagicMock()
    safety.check_all = AsyncMock(return_value=(True, "proposed", ""))
    safety._check_cooldown = AsyncMock(return_value=(False, ""))
    safety.record_action_executed = AsyncMock()
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(return_value={})
    snapshot.verify_recovery = MagicMock(return_value=(True, "recovered", {}))
    executor = MagicMock()
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.service_healthy = AsyncMock(return_value=True)
    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "ok"))
    engine = HealingEngine(db, MagicMock(publish=AsyncMock()), executor, safety, snapshot, playbooks)

    victim = AnomalyEvent(
        service="quoting-service",
        severity=Severity.CRITICAL,
        score=0.9,
        kind=AnomalyKind.LATENCY_SPIKE,
        detected_at=datetime.now(UTC),  # fresh: stale anomalies are ignored
    )
    incident = Incident(
        title="cascade",
        severity=Severity.CRITICAL,
        affected_services=["central-ledger", "quoting-service"],
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.SERVICE,
                target=ROOT,
                confidence=0.9,
                summary="ledger failure cascading",
            )
        ],
    )
    await engine._process_anomaly(victim, incident)

    call = playbooks.execute.await_args
    played, target, params = (list(call.args) + list(call.kwargs.values()))[:3]
    assert played == PlaybookName.CIRCUIT_BREAK_CASCADE
    assert target == ROOT
    assert params["victim_service"] == "quoting-service"
    assert safety.check_all.await_args.args[0] == f"{ROOT}:breakers"
    assert params["restart_root"] is True


async def test_engine_ignores_stale_anomalies() -> None:
    """Regression: after downtime the engine replayed the stream backlog and restarted every
    service in response to anomalies up to 9 hours old."""
    from unittest.mock import MagicMock

    from aiops_common.constants import AnomalyKind, Severity
    from aiops_common.schemas import AnomalyEvent
    from healing_engine.engine import HealingEngine

    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "ok"))
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(return_value={})
    engine = HealingEngine(db, MagicMock(), MagicMock(), MagicMock(), snapshot, playbooks)

    old = AnomalyEvent(
        service=ROOT,
        severity=Severity.CRITICAL,
        score=0.99,
        kind=AnomalyKind.MEMORY_PRESSURE,
        detected_at=datetime.now(UTC) - timedelta(hours=9),
    )
    await engine._process_anomaly(old, None)
    playbooks.execute.assert_not_awaited()
    snapshot.capture.assert_not_awaited()  # not even a decision was made, nor recorded


def test_transitive_dependencies_follow_the_call_graph() -> None:
    from rca_engine.incident_manager import transitive_dependencies

    deps = transitive_dependencies(["ml-api-adapter"])
    assert {"central-ledger", "quoting-service", "account-lookup-service"} <= deps
    assert "ml-api-adapter" not in deps
    assert transitive_dependencies(["central-ledger"]) == set()


def test_cascade_victim_gets_breaker_even_when_root_is_a_chaos_event() -> None:
    """Regression: callers of a service hit by a chaos run were restarted, because the
    incident's primary hypothesis was an EVENT, not a SERVICE."""
    from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.playbooks import PlaybookRegistry

    incident = Incident(
        title="memory leak",
        severity=Severity.CRITICAL,
        affected_services=["account-lookup-service", "quoting-service"],
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.EVENT,
                target="chaos_123",
                confidence=0.99,
                summary="chaos experiment",
            )
        ],
    )
    victim = AnomalyEvent(
        service="quoting-service",
        severity=Severity.CRITICAL,
        score=0.99,
        kind=AnomalyKind.ERROR_BURST,
        detected_at=datetime.now(UTC),
    )
    playbook, params = PlaybookRegistry.select_playbook(victim, incident, snapshot={})
    assert playbook == PlaybookName.CIRCUIT_BREAK_CASCADE
    assert params["downstream_target"] == "account-lookup-service"

    # The root itself still gets its own remediation, not a breaker.
    root = victim.model_copy(update={"service": "account-lookup-service", "kind": AnomalyKind.MEMORY_PRESSURE})
    playbook, _ = PlaybookRegistry.select_playbook(root, incident, snapshot={"memory_utilisation_ratio": 0.95})
    assert playbook == PlaybookName.RESTART_ON_OOM


async def test_early_victim_anomaly_finds_the_dependency_incident() -> None:
    """Regression: a victim's anomaly reached healing before RCA had correlated it, so with
    no incident the cascade rule could not fire and the victim was restarted."""
    from unittest.mock import MagicMock

    from healing_engine.engine import HealingEngine

    db = MagicMock()
    db.fetch_one = AsyncMock(
        return_value={
            "incident_id": "inc_1",
            "opened_at": datetime.now(UTC),
            "status": "open",
            "severity": "critical",
            "platform": "mojaloop",
            "title": "leak",
            "affected_services": ["account-lookup-service"],
            "anomaly_ids": ["a1"],
            "hypotheses": '[{"category": "event", "target": "chaos_1", "confidence": 0.99, '
            '"summary": "chaos", "evidence": [], "correlated_metrics": {}}]',
            "ai_summary": "",
        }
    )
    engine = HealingEngine(db, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    incident = await engine._open_incident_for_dependencies("quoting-service")
    assert incident is not None and incident.incident_id == "inc_1"
    sent = db.fetch_one.await_args.args[1]["dependencies"]
    assert "account-lookup-service" in sent and "central-ledger" in sent
    # A service with no dependencies never borrows an incident.
    assert await engine._open_incident_for_dependencies("central-ledger") is None


async def test_engine_ignores_non_actionable_anomalies() -> None:
    """Regression: an info-level 0.52 anomaly got quoting-service restarted, because the
    action confidence came from RCA's fixed 0.90 'single service affected' hypothesis."""
    from unittest.mock import MagicMock

    from aiops_common.constants import AnomalyKind, Severity
    from aiops_common.schemas import AnomalyEvent
    from healing_engine.engine import HealingEngine

    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "ok"))
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(return_value={})
    engine = HealingEngine(MagicMock(), MagicMock(), MagicMock(), MagicMock(), snapshot, playbooks)
    blip = AnomalyEvent(
        service="quoting-service",
        severity=Severity.INFO,
        score=0.52,
        threshold=0.65,
        kind=AnomalyKind.MULTIVARIATE,
        detected_at=datetime.now(UTC),
    )
    await engine._process_anomaly(blip, None)
    playbooks.execute.assert_not_awaited()
    snapshot.capture.assert_not_awaited()


def test_indirect_victim_gets_breaker_on_the_deepest_root() -> None:
    """Regression: transaction-request-service reaches central-ledger only through
    quoting-service; checking direct dependencies alone got it restarted as collateral."""
    from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.playbooks import PlaybookRegistry

    incident = Incident(
        title="ledger latency",
        severity=Severity.CRITICAL,
        affected_services=["central-ledger", "ml-api-adapter"],  # quoting not (yet) in it
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.EVENT, target="chaos_9", confidence=0.99, summary="x"
            )
        ],
    )
    victim = AnomalyEvent(
        service="transaction-request-service",
        severity=Severity.CRITICAL,
        score=0.9,
        kind=AnomalyKind.MULTIVARIATE,
        detected_at=datetime.now(UTC),
    )
    playbook, params = PlaybookRegistry.select_playbook(victim, incident, snapshot={})
    assert playbook == PlaybookName.CIRCUIT_BREAK_CASCADE
    assert params["downstream_target"] == "central-ledger"


async def test_breakers_are_not_blocked_by_the_roots_restart_cooldown(monkeypatch) -> None:
    """Regression: breaker attempts shared the root's cooldown key, so right after the root's
    restart 28 attempts were blocked and the breakers opened 6 minutes late."""
    from unittest.mock import MagicMock

    import healing_engine.engine as engine_mod
    from aiops_common.constants import AnomalyKind, PlaybookName, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.engine import HealingEngine

    monkeypatch.setattr(engine_mod.asyncio, "sleep", AsyncMock())
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    safety = MagicMock()
    safety._check_cooldown = AsyncMock(return_value=(True, "root in cooldown"))
    safety.check_all = AsyncMock(return_value=(True, "proposed", ""))
    safety.record_action_executed = AsyncMock()
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(return_value={})
    snapshot.verify_recovery = MagicMock(return_value=(True, "ok", {}))
    executor = MagicMock()
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.service_healthy = AsyncMock(return_value=True)
    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "ok"))
    engine = HealingEngine(db, MagicMock(publish=AsyncMock()), executor, safety, snapshot, playbooks)

    incident = Incident(
        title="cascade",
        severity=Severity.CRITICAL,
        affected_services=["central-ledger", "quoting-service"],
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.SERVICE, target=ROOT, confidence=0.9, summary="x"
            )
        ],
    )
    victim = AnomalyEvent(
        service="quoting-service",
        severity=Severity.CRITICAL,
        score=0.9,
        kind=AnomalyKind.LATENCY_SPIKE,
        detected_at=datetime.now(UTC),
    )
    await engine._process_anomaly(victim, incident)

    assert safety.check_all.await_args.args[0] == f"{ROOT}:breakers"
    call = playbooks.execute.await_args
    played, target, params = (list(call.args) + list(call.kwargs.values()))[:3]
    assert played == PlaybookName.CIRCUIT_BREAK_CASCADE and target == ROOT
    assert params["restart_root"] is False  # remediated moments ago: breakers only


async def test_playbook_skips_root_restart_when_told() -> None:
    from healing_engine.playbooks import PlaybookExecutor

    executor = AsyncMock()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            class R:
                status_code = 200

            return R()

    ok, message = await PlaybookExecutor(executor, http_client_factory=FakeClient)._circuit_break_cascade(
        ROOT, restart_root=False
    )
    assert ok is True
    executor.restart_container.assert_not_awaited()
    assert "not restarted again" in message


async def test_breaker_only_action_is_not_judged_by_the_roots_stale_p99(monkeypatch) -> None:
    """Regression: a breakers-only action was marked failed because the root's p99, measured
    seconds later, was still a rate window full of the fault - and the incident got stuck."""
    from unittest.mock import MagicMock

    import healing_engine.engine as engine_mod
    from aiops_common.constants import AnomalyKind, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.engine import HealingEngine

    monkeypatch.setattr(engine_mod.asyncio, "sleep", AsyncMock())
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    safety = MagicMock()
    safety._check_cooldown = AsyncMock(return_value=(True, "root in cooldown"))
    safety.check_all = AsyncMock(return_value=(True, "proposed", ""))
    safety.record_action_executed = AsyncMock()
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(return_value={})
    snapshot.verify_recovery = MagicMock(return_value=(False, "p99=0.274s", {}))
    executor = MagicMock()
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.service_healthy = AsyncMock(return_value=True)
    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "Opened circuit breaker on central-ledger"))
    engine = HealingEngine(db, MagicMock(publish=AsyncMock()), executor, safety, snapshot, playbooks)
    incident = Incident(
        title="cascade",
        severity=Severity.CRITICAL,
        affected_services=["central-ledger", "quoting-service"],
        hypotheses=[
            RootCauseHypothesis(category=RootCauseCategory.SERVICE, target=ROOT, confidence=0.9, summary="x")
        ],
    )
    victim = AnomalyEvent(
        service="quoting-service",
        severity=Severity.CRITICAL,
        score=0.9,
        kind=AnomalyKind.LATENCY_SPIKE,
        detected_at=datetime.now(UTC),
    )
    await engine._process_anomaly(victim, incident)
    assert safety.record_action_executed.await_args.kwargs["success"] is True


async def test_healing_waits_for_rcas_diagnosis(monkeypatch) -> None:
    """Regression: acting on the bare symptom restarted a bad deploy; the restart's cooldown
    then blocked the rollback RCA diagnosed moments later."""
    from unittest.mock import MagicMock

    import healing_engine.engine as engine_mod
    from healing_engine.engine import HealingEngine

    monkeypatch.setattr(engine_mod.asyncio, "sleep", AsyncMock())
    row = {
        "incident_id": "inc_2",
        "opened_at": datetime.now(UTC),
        "status": "open",
        "severity": "critical",
        "platform": "mojaloop",
        "title": "bad deploy",
        "affected_services": ["ml-api-adapter"],
        "anomaly_ids": ["a1"],
        "hypotheses": '[{"category": "change", "target": "dep_1", "confidence": 0.94, '
        '"summary": "deploy", "evidence": [], "correlated_metrics": {}}]',
        "ai_summary": "",
    }
    db = MagicMock()
    db.fetch_one = AsyncMock(side_effect=[None, None, row])  # RCA links it on the 3rd look
    engine = HealingEngine(db, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    incident = await engine._await_rca_incident("a1")
    assert incident is not None and incident.hypotheses[0].category == "change"


async def test_stale_first_reading_is_rechecked_before_failing(monkeypatch) -> None:
    """Regression: a successful rollback was marked failed because the first post-restart
    sample still carried the old process's last gauge (queue 60); 30 s later it read 0."""
    from unittest.mock import MagicMock

    import healing_engine.engine as engine_mod
    from aiops_common.constants import AnomalyKind, RootCauseCategory, Severity
    from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis
    from healing_engine.engine import HealingEngine

    monkeypatch.setattr(engine_mod.asyncio, "sleep", AsyncMock())
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    safety = MagicMock()
    safety._check_cooldown = AsyncMock(return_value=(False, ""))
    safety.check_all = AsyncMock(return_value=(True, "proposed", ""))
    safety.record_action_executed = AsyncMock()
    snapshot = MagicMock()
    snapshot.capture = AsyncMock(side_effect=[{"queue_depth": 3.0}, {"queue_depth": 60.0}, {"queue_depth": 0.0}])
    snapshot.verify_recovery = MagicMock(
        side_effect=lambda **kw: (
            (kw["after"]["queue_depth"] <= 5.0),
            f"queue={kw['after']['queue_depth']}",
            {},
        )
    )
    executor = MagicMock()
    executor.get_replica_count = AsyncMock(return_value=1)
    executor.service_healthy = AsyncMock(return_value=True)
    playbooks = MagicMock()
    playbooks.execute = AsyncMock(return_value=(True, "Rolled back ml-api-adapter"))
    engine = HealingEngine(db, MagicMock(publish=AsyncMock()), executor, safety, snapshot, playbooks)
    incident = Incident(
        title="bad deploy",
        severity=Severity.CRITICAL,
        affected_services=["ml-api-adapter"],
        hypotheses=[
            RootCauseHypothesis(
                category=RootCauseCategory.CHANGE,
                target="dep_1",
                confidence=0.94,
                summary="Recent deploy to ml-api-adapter may have introduced issues",
            )
        ],
    )
    anomaly = AnomalyEvent(
        service="ml-api-adapter",
        severity=Severity.CRITICAL,
        score=0.99,
        kind=AnomalyKind.QUEUE_BACKLOG,
        detected_at=datetime.now(UTC),
    )
    await engine._process_anomaly(anomaly, incident)

    assert safety.record_action_executed.await_args.kwargs["success"] is True
    assert snapshot.verify_recovery.call_count == 2
