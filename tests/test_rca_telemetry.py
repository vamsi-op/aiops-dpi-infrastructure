"""Logs and traces as RCA evidence (module 7).

Pinned here:
* Loki warning/error counts and Tempo service-graph failed edges are parsed correctly
* failed calls *into* the suspected root raise confidence and add evidence; logs add text only
* a dead Loki or Prometheus never blocks correlation - evidence is just empty
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops_common.constants import AnomalyKind, RootCauseCategory, Severity
from aiops_common.schemas import AnomalyEvent
from rca_engine.correlator import TRACE_CONFIRMATION_BOOST, Correlator
from rca_engine.telemetry import TelemetryClient

BASE = datetime(2026, 11, 13, 19, 0, tzinfo=UTC)


def _vector(rows: list[tuple[dict, float]]) -> dict:
    return {"data": {"result": [{"metric": m, "value": [0, str(v)]} for m, v in rows]}}


def _fetch_with(*, logs=(), failed=(), total=()):
    async def fetch(url: str, params: dict) -> dict:
        query = params["query"]
        if "/loki/" in url:
            return _vector(list(logs))
        if "request_failed_total" in query:
            return _vector(list(failed))
        return _vector(list(total))

    return fetch


async def test_collect_parses_logs_and_failed_edges() -> None:
    client = TelemetryClient(
        fetch=_fetch_with(
            logs=[({"service": "central-ledger"}, 14)],
            failed=[({"client": "quoting-service", "server": "central-ledger"}, 9)],
            total=[({"client": "quoting-service", "server": "central-ledger"}, 60)],
        )
    )
    evidence = await client.collect(300)
    assert evidence.log_problems == {"central-ledger": 14}
    assert evidence.failing_callers_of("central-ledger") == [("quoting-service", 9.0, 60.0)]
    lines = evidence.statements("central-ledger")
    assert any(line.startswith("Logs: 14") for line in lines)
    assert any("quoting-service -> central-ledger failed" in line for line in lines)


async def test_unreachable_backends_give_empty_evidence() -> None:
    async def broken(url, params):
        raise ConnectionError("loki down")

    evidence = await TelemetryClient(fetch=broken).collect(300)
    assert evidence.log_problems == {} and evidence.failed_calls == {}


def _cascade() -> list[AnomalyEvent]:
    # central-ledger first, then its callers: the topological root is central-ledger.
    return [
        AnomalyEvent(
            anomaly_id=f"anom-{i}",
            detected_at=BASE + timedelta(seconds=15 * i),
            service=service,
            severity=Severity.WARNING,
            score=0.9,
            contributing_features=["latency_p99_seconds"],
            kind=AnomalyKind.LATENCY_SPIKE,
        )
        for i, service in enumerate(["central-ledger", "quoting-service", "ml-api-adapter"])
    ]


class _NoDeployments:
    async def fetch_all(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
async def test_trace_evidence_confirms_the_root_and_raises_confidence() -> None:
    plain = Correlator(_NoDeployments())
    with_traces = Correlator(
        _NoDeployments(),
        telemetry=TelemetryClient(
            fetch=_fetch_with(
                logs=[({"service": "central-ledger"}, 5)],
                failed=[({"client": "quoting-service", "server": "central-ledger"}, 12)],
            )
        ),
    )
    base = next(
        h for h in await plain.correlate_all(_cascade()) if h.category == RootCauseCategory.SERVICE
    )
    confirmed = next(
        h
        for h in await with_traces.correlate_all(_cascade())
        if h.category == RootCauseCategory.SERVICE
    )
    assert confirmed.target == base.target == "central-ledger"
    assert confirmed.confidence == pytest.approx(
        min(0.99, base.confidence + TRACE_CONFIRMATION_BOOST)
    )
    assert confirmed.correlated_metrics["trace_failed_calls_in"] == 12
    assert confirmed.correlated_metrics["log_problems"] == 5
    assert any(e.startswith("Traces:") for e in confirmed.evidence)


class _DeployAndChaosDB:
    """A bad_deploy chaos run on ml-api-adapter, plus the deployment it recorded."""

    async def fetch_all(self, query, params=None):
        if "FROM deployments" in query:
            return [
                {
                    "deployment_id": "dep_1",
                    "occurred_at": BASE - timedelta(seconds=30),
                    "service": "ml-api-adapter",
                    "to_version": "v1.0.5-canary",
                    "change_type": "deploy",
                }
            ]
        if "FROM chaos_runs" in query:
            return [
                {
                    "run_id": "chaos_1",
                    "experiment": "bad_deploy",
                    "target_service": "ml-api-adapter",
                    "started_at": BASE - timedelta(seconds=30),
                }
            ]
        return []


@pytest.mark.asyncio
async def test_bad_deploy_chaos_is_attributed_to_the_change_not_an_event() -> None:
    """Regression: the chaos EVENT hypothesis (0.99) outranked the deployment, so healing
    restarted instead of rolling back - on Kubernetes, straight back into the bad version."""
    anomaly = AnomalyEvent(
        anomaly_id="a1",
        detected_at=BASE,
        service="ml-api-adapter",
        severity=Severity.CRITICAL,
        score=0.99,
        contributing_features=["error_ratio"],
        kind=AnomalyKind.ERROR_BURST,
    )
    hypotheses = await Correlator(_DeployAndChaosDB()).correlate_all([anomaly])
    assert hypotheses[0].category == RootCauseCategory.CHANGE
    assert all(h.category != RootCauseCategory.EVENT for h in hypotheses)


@pytest.mark.asyncio
async def test_merge_keeps_the_stronger_diagnosis() -> None:
    """Regression: a later batch analysed alone (CHANGE evidence decayed to 0.5) replaced the
    incident's 0.94 CHANGE root with 'only this service is affected' (0.90)."""
    import json as _json
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock as _MagicMock

    from aiops_common.schemas import RootCauseHypothesis
    from rca_engine.incident_manager import IncidentManager

    change = RootCauseHypothesis(
        category=RootCauseCategory.CHANGE, target="dep_1", confidence=0.94, summary="deploy"
    )
    weaker = RootCauseHypothesis(
        category=RootCauseCategory.SERVICE, target="ml-api-adapter", confidence=0.90, summary="x"
    )
    db = _MagicMock()
    db.fetch_one = _AsyncMock(
        return_value={
            "incident_id": "inc_1",
            "opened_at": BASE,
            "status": "open",
            "severity": "critical",
            "platform": "mojaloop",
            "title": "bad deploy",
            "affected_services": ["ml-api-adapter"],
            "anomaly_ids": ["a1"],
            "hypotheses": _json.dumps([change.model_dump()]),
            "ai_summary": "",
        }
    )
    db.execute = _AsyncMock()
    manager = IncidentManager(db, _MagicMock(publish=_AsyncMock()))
    later = AnomalyEvent(
        anomaly_id="a2",
        detected_at=BASE + timedelta(minutes=5),
        service="ml-api-adapter",
        severity=Severity.CRITICAL,
        score=0.99,
        kind=AnomalyKind.ERROR_BURST,
    )
    incident = await manager.create_incident([later], [weaker])
    assert incident.hypotheses[0].category == RootCauseCategory.CHANGE
