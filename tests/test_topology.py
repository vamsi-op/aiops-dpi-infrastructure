"""The service graph is configuration (aiops_common.topology), not Mojaloop code."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiops_common.constants import MOJALOOP_DEPENDENCIES, AnomalyKind, Severity
from aiops_common.schemas import AnomalyEvent
from aiops_common.topology import (
    TopologyError,
    default_topology,
    get_topology,
    load_topology,
    parse_topology,
)

REPO = Path(__file__).resolve().parents[1]

# A made-up, non-Mojaloop DPI system: a gateway in front of an identity registry and a
# ledger, with the ledger depending on a notary.
CUSTOM = {
    "name": "custom-dpi",
    "services": {
        "gateway": {"calls": ["registry", "ledger"]},
        "registry": {"calls": []},
        "ledger": {"calls": ["notary"]},
        "notary": {"calls": []},
    },
}


@pytest.fixture
def custom_topology(tmp_path, monkeypatch):
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(CUSTOM), encoding="utf-8")
    monkeypatch.setenv("AIOPS_TOPOLOGY_FILE", str(path))
    get_topology.cache_clear()
    yield get_topology()
    get_topology.cache_clear()


def test_bundled_file_matches_the_simulator_graph() -> None:
    from_file = load_topology(REPO / "config" / "topology.mojaloop.json")
    assert from_file.dependencies == default_topology().dependencies
    assert set(from_file.services) == {str(s) for s in MOJALOOP_DEPENDENCIES}


def test_graph_queries() -> None:
    graph = parse_topology(CUSTOM)
    assert graph.callers_of("ledger") == ["gateway"]
    assert graph.transitive_dependencies("gateway") == {"registry", "ledger", "notary"}
    assert graph.calls_transitively("gateway", "notary")
    assert not graph.calls_transitively("notary", "gateway")


@pytest.mark.parametrize(
    ("services", "message"),
    [
        ({}, "no services"),
        ({"a": {"calls": ["missing"]}}, "unknown"),
        ({"a": {"calls": ["a"]}}, "itself"),
        ({"a": {"calls": ["b"]}, "b": {"calls": ["a"]}}, "cycle"),
        ({"a": {"calls": "b"}}, "list"),
    ],
)
def test_invalid_topologies_are_rejected(services, message) -> None:
    with pytest.raises(TopologyError, match=message):
        parse_topology({"services": services})


def test_unset_means_builtin_mojaloop(monkeypatch) -> None:
    monkeypatch.delenv("AIOPS_TOPOLOGY_FILE", raising=False)
    assert load_topology().name == "mojaloop"


def _anomaly(service: str, seconds: int) -> AnomalyEvent:
    return AnomalyEvent(
        anomaly_id=f"a-{service}",
        detected_at=datetime(2026, 11, 14, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds),
        service=service,
        severity=Severity.CRITICAL,
        score=0.95,
        contributing_features=["latency_p99_seconds"],
        kind=AnomalyKind.LATENCY_SPIKE,
    )


def test_rca_finds_root_in_a_custom_topology(custom_topology) -> None:
    """The deepest failing dependency is the root, whatever the platform."""
    from rca_engine.correlator import RCACorrelator

    # The gateway alarms first, but it is a victim of notary through ledger.
    anomalies = [_anomaly("gateway", 0), _anomaly("ledger", 20), _anomaly("notary", 40)]
    hypothesis = RCACorrelator(db=None).correlate_topological(anomalies)

    assert hypothesis is not None
    assert hypothesis.target == "notary"


def test_cascade_playbook_uses_custom_callers(custom_topology) -> None:
    from healing_engine.playbooks import _all_dependencies

    assert _all_dependencies("gateway") == {"registry", "ledger", "notary"}
    assert custom_topology.callers_of("notary") == ["ledger"]
