"""Metrics collection agent behaviour (module 3).

Prometheus is faked rather than run. What is worth asserting here is the normalisation and
the safety behaviour - that a down service produces no row rather than a row of zeros, that
counts and the stored error ratio agree, that a retried cycle updates instead of duplicating
- and none of that needs a real server. The live path is covered by
`scripts/check_metrics_agent.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.schemas import MetricSample, utc_now
from metrics_agent.agent import AgentMetrics, MetricsAgent
from metrics_agent.backfill import Backfiller
from metrics_agent.collector import (
    AGGREGATE_SUFFIX,
    DISCOVERY_QUERY,
    ERROR_RATIO_QUERY,
    GAUGE_QUERIES,
    REPLICAS_QUERY,
    MetricsCollector,
)
from metrics_agent.writer import INSERT_METRICS, METRIC_COLUMNS, MetricsWriter, to_row

SERVICES = ("central-ledger", "quoting-service")


class FakePrometheus:
    """Stands in for PrometheusClient.

    Returns per-query, per-service values from a dictionary. Records every query it was
    asked for, so a test can assert the agent read the recording rules rather than raw
    counters.
    """

    def __init__(
        self,
        values: dict[str, dict[str, float]] | None = None,
        *,
        up: dict[str, float] | None = None,
        instances: dict[str, list[str]] | None = None,
    ) -> None:
        self.values = values or {}
        self.up = up if up is not None else dict.fromkeys(SERVICES, 1.0)
        self.instances = instances or {s: [f"{s}:8000"] for s in SERVICES}
        self.queries_seen: list[str] = []
        self.evaluated_at: list[datetime | None] = []

    async def instant(self, query: str, at: datetime | None = None) -> list[dict[str, Any]]:
        self.queries_seen.append(query)
        self.evaluated_at.append(at)
        moment = at or utc_now()

        if query == DISCOVERY_QUERY:
            results = []
            for service, value in self.up.items():
                for instance in self.instances.get(service, [f"{service}:8000"]):
                    results.append(
                        {
                            "labels": {
                                "service": service,
                                "instance": instance,
                                "job": "mojaloop",
                            },
                            "value": value,
                            "timestamp": moment,
                        }
                    )
            return results

        per_service = self.values.get(query, {})
        return [
            {"labels": {"service": s}, "value": v, "timestamp": moment}
            for s, v in per_service.items()
        ]

    async def scalar_by_label(
        self, query: str, label: str, *, default: float = 0.0
    ) -> dict[str, float]:  # pragma: no cover - collector always passes an explicit time
        return {
            item["labels"][label]: float(item["value"] or default)
            for item in await self.instant(query)
            if label in item["labels"]
        }


class FakeDatabase:
    def __init__(self, seeded_services: list[str] | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.statements: list[str] = []
        self.seeded_services = seeded_services or []

    async def execute_many(self, sql: str, param_sets: list[dict[str, Any]]) -> int:
        self.statements.append(sql)
        self.batches.append(list(param_sets))
        return len(param_sets)

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        return [{"service": s} for s in self.seeded_services]


class FakeBus:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, Any]] = []
        self.fail = fail

    async def publish(self, stream: Any, payload: Any, **_kwargs: Any) -> str:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.published.append((str(stream), payload))
        return "1-0"


def full_values(
    *,
    tps: float = 20.0,
    error_ratio: float = 0.01,
    replicas: float = 2.0,
) -> dict[str, dict[str, float]]:
    """Every rule the collector reads, populated for both services."""
    values = {query: dict.fromkeys(SERVICES, 1.0) for query in GAUGE_QUERIES.values()}
    values[GAUGE_QUERIES["tps"]] = dict.fromkeys(SERVICES, tps)
    values[ERROR_RATIO_QUERY] = dict.fromkeys(SERVICES, error_ratio)
    values[REPLICAS_QUERY] = dict.fromkeys(SERVICES, replicas)
    return values


def make_collector(prometheus: FakePrometheus, interval: int = 15) -> MetricsCollector:
    return MetricsCollector(prometheus, interval_seconds=interval)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The query contract
# ---------------------------------------------------------------------------
def test_every_gauge_query_maps_to_a_real_metricsample_field() -> None:
    # A typo here would write nothing and raise nothing: MetricSample would take the
    # keyword, Pydantic would reject it, and the cycle would fail with a validation error
    # far from the cause. Asserting the mapping keeps the failure at the definition.
    for field in GAUGE_QUERIES:
        assert field in MetricSample.model_fields, field


def test_collector_reads_recording_rules_not_raw_counters() -> None:
    # The recording rules exist so the agent, the dashboards and the alerts share one
    # computation. Reading raw counters here would reintroduce the drift they prevent.
    for query in GAUGE_QUERIES.values():
        assert query.startswith("dpi:"), query
    assert ERROR_RATIO_QUERY.startswith("dpi:")
    assert REPLICAS_QUERY.startswith("dpi:")


def test_anomaly_features_are_all_collected_or_derived() -> None:
    """Module 4 cannot train on a feature nobody populates."""
    derived = {"error_ratio"}  # from ERROR_RATIO_QUERY via MetricSample.measured_error_ratio
    for feature in ANOMALY_FEATURES:
        assert feature in GAUGE_QUERIES or feature in derived, feature


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
async def test_collect_produces_one_sample_per_up_service() -> None:
    collector = make_collector(FakePrometheus(full_values()))
    result = await collector.collect()

    assert result.service_count == 2
    assert sorted(s.service for s in result.samples) == sorted(SERVICES)
    assert not result.missing_rules
    assert not result.services_down


async def test_down_service_gets_no_row_rather_than_zeros() -> None:
    """The most important behaviour in this module.

    A row of zeros says "this service handled no traffic and had no errors", which is what a
    quiet period looks like. Module 4 would learn that an outage is indistinguishable from
    idleness. An absent row says "we do not know", which is the truth.
    """
    prometheus = FakePrometheus(
        full_values(), up={"central-ledger": 1.0, "quoting-service": 0.0}
    )
    result = await make_collector(prometheus).collect()

    assert [s.service for s in result.samples] == ["central-ledger"]
    assert result.services_down == ["quoting-service"]


async def test_service_with_no_tps_is_skipped_not_written_as_zero() -> None:
    """Found by watching the agent's logs after Prometheus restarted.

    Every `rate()` rule needs history behind the evaluation instant, so for the first five
    minutes after a Prometheus restart the gauges resolve while `dpi:tps:rate5m` does not.
    Writing tps=0 through that window stamps a five-minute fake outage into the table after
    every restart, and module 4 would train on it as a normal quiet period.
    """
    values = full_values()
    values[GAUGE_QUERIES["tps"]] = {"central-ledger": 20.0}  # quoting-service absent

    result = await make_collector(FakePrometheus(values)).collect()

    assert [s.service for s in result.samples] == ["central-ledger"]
    assert result.services_incomplete == ["quoting-service"]
    # The service is healthy; only our view of it is incomplete. Conflating the two would
    # make a Prometheus restart look like a Mojaloop outage.
    assert result.services_down == []


async def test_nan_is_treated_as_absent_not_as_zero() -> None:
    """`histogram_quantile` returns NaN on empty buckets, which arrives as None."""
    values = full_values()
    values[GAUGE_QUERIES["latency_p99_seconds"]] = dict.fromkeys(SERVICES, 1.5)

    class NaNPrometheus(FakePrometheus):
        async def instant(self, query: str, at: datetime | None = None) -> list[dict[str, Any]]:
            results = await super().instant(query, at)
            if query == GAUGE_QUERIES["latency_p99_seconds"]:
                for item in results:
                    item["value"] = None
            return results

    result = await make_collector(NaNPrometheus(values)).collect()

    # The rule is reported as returning nothing usable, rather than the row claiming that
    # p99 latency was measured at zero.
    assert GAUGE_QUERIES["latency_p99_seconds"] in result.missing_rules


async def test_no_services_up_returns_an_empty_result_not_an_error() -> None:
    prometheus = FakePrometheus(full_values(), up=dict.fromkeys(SERVICES, 0.0))
    result = await make_collector(prometheus).collect()

    assert result.samples == []
    assert result.services_down == sorted(SERVICES)


async def test_missing_rules_are_reported_not_silently_zeroed() -> None:
    values = full_values()
    del values["dpi:settlement_lag:max"]
    result = await make_collector(FakePrometheus(values)).collect()

    assert "dpi:settlement_lag:max" in result.missing_rules
    # The field still defaults to 0 so the row is writable, but the cycle reports that the
    # zero was an absence rather than an observation.
    assert all(s.settlement_lag_seconds == 0.0 for s in result.samples)


async def test_timestamp_is_truncated_to_whole_seconds() -> None:
    at = datetime(2026, 9, 21, 12, 30, 45, 123456, tzinfo=utc_now().tzinfo)
    result = await make_collector(FakePrometheus(full_values())).collect(at=at)

    # The primary key includes the timestamp, so sub-second jitter would stop ON CONFLICT
    # from ever firing and a retried cycle would insert a near-duplicate row.
    assert all(s.timestamp.microsecond == 0 for s in result.samples)
    assert result.collected_at.microsecond == 0


async def test_every_query_is_evaluated_at_the_requested_instant() -> None:
    """The backfill depends on this: same code path, different clock."""
    at = utc_now() - timedelta(hours=3)
    prometheus = FakePrometheus(full_values())
    await make_collector(prometheus).collect(at=at)

    assert prometheus.evaluated_at, "no queries were issued"
    expected = at.replace(microsecond=0)
    assert all(moment == expected for moment in prometheus.evaluated_at)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
async def test_counts_are_derived_from_tps_over_the_interval() -> None:
    prometheus = FakePrometheus(full_values(tps=20.0, error_ratio=0.0))
    result = await make_collector(prometheus, interval=15).collect()

    # 20 tps over a 15s window.
    assert all(s.request_count == 300 for s in result.samples)


async def test_stored_error_ratio_agrees_with_the_recorded_one() -> None:
    """The stored ratio is the recorded one, and the counts agree with it when they can."""
    prometheus = FakePrometheus(full_values(tps=20.0, error_ratio=0.10))
    result = await make_collector(prometheus, interval=15).collect()

    sample = result.samples[0]
    assert sample.request_count == 300
    assert sample.error_count == 30
    assert sample.error_ratio == pytest.approx(0.10, abs=1e-9)


async def test_low_error_ratio_survives_whole_number_counts() -> None:
    """Regression: 0.15% errors at ~20 tps is 0.45 errors per 15 s window.

    The count rounds to 0. When the ratio was derived from the counts, every low-error
    service read as error-free on every sample, the training data (which never rounds that
    way) did not, and the anomaly detector flagged the difference continuously.
    """
    prometheus = FakePrometheus(full_values(tps=20.0, error_ratio=0.0015))
    result = await make_collector(prometheus, interval=15).collect()

    sample = result.samples[0]
    assert sample.error_count == 0  # the coarse count is still whole
    assert sample.error_ratio == pytest.approx(0.0015)  # but the scored ratio is measured
    # And it survives the trip over the event bus to the detector.
    reread = MetricSample.model_validate(sample.model_dump(mode="json"))
    assert reread.error_ratio == pytest.approx(0.0015)
    assert reread.feature_vector(("error_ratio",)) == [pytest.approx(0.0015)]


async def test_error_count_never_exceeds_request_count() -> None:
    # A rate() window can briefly report a ratio above 1 when a counter resets, and the
    # column is a bigint with a computed ratio clamped to 1.0 - an error count above the
    # request count would make the stored ratio disagree with the computed one.
    prometheus = FakePrometheus(full_values(tps=10.0, error_ratio=5.0))
    result = await make_collector(prometheus, interval=15).collect()

    sample = result.samples[0]
    assert sample.error_count == sample.request_count
    assert sample.error_ratio == 1.0


async def test_zero_traffic_produces_zero_counts_not_a_divide_error() -> None:
    prometheus = FakePrometheus(full_values(tps=0.0, error_ratio=0.0))
    result = await make_collector(prometheus).collect()

    sample = result.samples[0]
    assert sample.request_count == 0
    assert sample.error_ratio == 0.0


async def test_replicas_round_to_at_least_one() -> None:
    prometheus = FakePrometheus(full_values(replicas=0.0))
    result = await make_collector(prometheus).collect()
    # `dpi:replicas` is a max() over a gauge and can read 0 while a container restarts.
    # Zero replicas would make module 5's capacity calculation divide by zero.
    assert all(s.replicas >= 1 for s in result.samples)


async def test_rows_are_tagged_with_their_source() -> None:
    result = await make_collector(FakePrometheus(full_values())).collect()
    assert all(s.labels["source"] == "metrics-agent" for s in result.samples)


# ---------------------------------------------------------------------------
# Instance identity - the double-counting hazard
# ---------------------------------------------------------------------------
async def test_single_target_uses_the_real_instance_address() -> None:
    result = await make_collector(FakePrometheus(full_values())).collect()
    assert {s.instance for s in result.samples} == {
        "central-ledger:8000",
        "quoting-service:8000",
    }


async def test_multiple_targets_collapse_to_one_aggregate_row() -> None:
    """Guards a silent doubling of reported platform traffic.

    The recording rules already sum across instances, and
    `DashboardRepository.service_health` sums `tps` across a service's instances. One row
    per replica carrying an already-summed value would multiply traffic by the replica
    count, with nothing anywhere reporting an error.
    """
    prometheus = FakePrometheus(
        full_values(),
        up={"central-ledger": 1.0},
        instances={"central-ledger": ["central-ledger-1:8000", "central-ledger-2:8000"]},
    )
    result = await make_collector(prometheus).collect()

    assert len(result.samples) == 1
    assert result.samples[0].instance == f"central-ledger{AGGREGATE_SUFFIX}"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_row_contains_every_column_including_the_computed_ratio() -> None:
    sample = MetricSample(service="central-ledger", request_count=100, error_count=5)
    row = to_row(sample)

    assert set(row) == set(METRIC_COLUMNS)
    # error_ratio is a computed property, not a model field, so it would be missed by
    # anything that iterates model_fields.
    assert row["error_ratio"] == pytest.approx(0.05)


def test_written_rows_are_attributable_to_the_agent() -> None:
    """Regression: `labels` was missing from the insert.

    The column list was copied from the seeder, whose own list stops at `replicas`. The
    agent set `labels` on the model and then never wrote it, so every row landed with the
    `'{}'` default - leaving agent rows indistinguishable from seeded ones and quietly
    disabling the conflicting-producer detection that depends on the tag.
    """
    assert "labels" in METRIC_COLUMNS
    # jsonb needs an explicit cast; a plain bind parameter is rejected by Postgres.
    assert "CAST(:labels AS jsonb)" in INSERT_METRICS
    assert "labels = EXCLUDED.labels" in INSERT_METRICS


async def test_labels_are_serialised_as_json_for_the_jsonb_column() -> None:
    result = await make_collector(FakePrometheus(full_values())).collect()
    row = to_row(result.samples[0])

    assert isinstance(row["labels"], str), "a dict would be rejected by asyncpg"
    assert json.loads(row["labels"])["source"] == "metrics-agent"


def test_insert_updates_on_conflict_rather_than_ignoring() -> None:
    """A retried cycle carries better data than the attempt that failed.

    The seeder uses DO NOTHING because it generates history once. The agent re-collects on a
    schedule, and Prometheus rate() windows fill in as scrapes land, so freezing the first
    attempt would permanently keep the worse value.
    """
    assert "DO UPDATE" in INSERT_METRICS
    assert 'ON CONFLICT ("timestamp", service, instance)' in INSERT_METRICS


async def test_write_batches_every_sample_in_one_statement() -> None:
    database = FakeDatabase()
    writer = MetricsWriter(database, FakeBus())  # type: ignore[arg-type]
    samples = [MetricSample(service=s) for s in SERVICES]

    assert await writer.write(samples) == 2
    assert len(database.batches) == 1, "should be one round trip, not one per sample"


async def test_write_of_nothing_is_a_no_op() -> None:
    database = FakeDatabase()
    writer = MetricsWriter(database, FakeBus())  # type: ignore[arg-type]
    assert await writer.write([]) == 0
    assert not database.batches


async def test_publish_emits_one_event_per_service() -> None:
    bus = FakeBus()
    writer = MetricsWriter(FakeDatabase(), bus)  # type: ignore[arg-type]
    samples = [MetricSample(service=s) for s in SERVICES]

    assert await writer.publish(samples) == 2
    assert [stream for stream, _ in bus.published] == ["aiops.metrics"] * 2


async def test_bus_failure_does_not_lose_the_database_write() -> None:
    # The row is already durable by the time publishing happens. Raising here would make a
    # Redis blip look like a collection failure and lose the cycle's status.
    writer = MetricsWriter(FakeDatabase(), FakeBus(fail=True))  # type: ignore[arg-type]
    assert await writer.publish([MetricSample(service="central-ledger")]) == 0


async def test_seeded_rows_in_the_same_window_are_detected() -> None:
    writer = MetricsWriter(
        FakeDatabase(seeded_services=["central-ledger"]),  # type: ignore[arg-type]
        FakeBus(),
    )
    assert await writer.detect_conflicting_producer() == ["central-ledger"]


async def test_no_seeded_rows_reports_no_conflict() -> None:
    writer = MetricsWriter(FakeDatabase(), FakeBus())  # type: ignore[arg-type]
    assert await writer.detect_conflicting_producer() == []


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
def make_agent(
    prometheus: FakePrometheus,
    database: FakeDatabase | None = None,
    bus: FakeBus | None = None,
) -> tuple[MetricsAgent, FakeDatabase, FakeBus]:
    db = database or FakeDatabase()
    b = bus or FakeBus()
    writer = MetricsWriter(db, b)  # type: ignore[arg-type]
    metrics = AgentMetrics(CollectorRegistry())
    agent = MetricsAgent(make_collector(prometheus), writer, metrics, interval_seconds=15)
    return agent, db, b


async def test_successful_cycle_updates_status() -> None:
    agent, _, _ = make_agent(FakePrometheus(full_values()))
    status = await agent.run_once()

    assert status.cycles == 1
    assert status.rows_written == 2
    assert status.events_published == 2
    assert status.failures == 0
    assert status.last_error is None
    assert status.last_duration_seconds >= 0


async def test_failed_cycle_records_the_stage_and_reraises() -> None:
    class BrokenPrometheus(FakePrometheus):
        async def instant(self, query: str, at: datetime | None = None) -> list[dict[str, Any]]:
            raise RuntimeError("prometheus down")

    agent, _, _ = make_agent(BrokenPrometheus())
    with pytest.raises(RuntimeError):
        await agent.run_once()

    # Naming the stage is the difference between "collection failed" and a status that says
    # whether Prometheus, TimescaleDB or Redis was at fault.
    assert agent.status.failures == 1
    assert agent.status.consecutive_failures == 1
    assert agent.status.last_error is not None
    assert agent.status.last_error.startswith("collect:")


async def test_consecutive_failures_reset_on_success() -> None:
    agent, _, _ = make_agent(FakePrometheus(full_values()))
    agent.status.consecutive_failures = 4

    await agent.run_once()
    # Reported as a gauge specifically so "failing right now" can be told apart from
    # "failed four times earlier".
    assert agent.status.consecutive_failures == 0


async def test_status_serialises_for_the_status_endpoint() -> None:
    agent, _, _ = make_agent(FakePrometheus(full_values()))
    await agent.run_once()
    payload = agent.status.as_dict()

    assert payload["cycles"] == 1
    assert isinstance(payload["last_collected_at"], str)
    assert payload["last_missing_rules"] == []


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
async def test_backfill_walks_the_range_and_writes_rows() -> None:
    prometheus = FakePrometheus(full_values())
    database = FakeDatabase()
    writer = MetricsWriter(database, FakeBus())  # type: ignore[arg-type]
    backfiller = Backfiller(make_collector(prometheus), writer)

    result = await backfiller.run(hours=1, step_seconds=300)

    # 1 hour at 5-minute steps, inclusive of both ends.
    assert result.requested_points == 13
    assert result.collected_points == 13
    assert result.rows_written == 26  # two services per point


async def test_backfill_does_not_publish_by_default() -> None:
    """Replaying history onto the stream would evict live events.

    `REDIS_STREAM_MAXLEN` is 50k. A week of backfill at 60s resolution across five services
    is ~50k events, which would push out exactly the recent samples the anomaly detector
    has not read yet.
    """
    bus = FakeBus()
    writer = MetricsWriter(FakeDatabase(), bus)  # type: ignore[arg-type]
    backfiller = Backfiller(make_collector(FakePrometheus(full_values())), writer)

    await backfiller.run(hours=1, step_seconds=600)
    assert bus.published == []


async def test_backfill_stops_short_of_the_live_cycle() -> None:
    # The newest instant belongs to the live loop. Writing it here would race the loop for
    # the same primary key.
    end = utc_now().replace(microsecond=0)
    backfiller = Backfiller(
        make_collector(FakePrometheus(full_values())),
        MetricsWriter(FakeDatabase(), FakeBus()),  # type: ignore[arg-type]
    )
    result = await backfiller.run(hours=1, step_seconds=60, end=end)
    assert result.end < end


async def test_backfill_survives_a_failing_instant() -> None:
    class FlakyPrometheus(FakePrometheus):
        def __init__(self) -> None:
            super().__init__(full_values())
            self.calls = 0

        async def instant(self, query: str, at: datetime | None = None) -> list[dict[str, Any]]:
            self.calls += 1
            # Not a multiple of the queries per cycle, or every point would fail.
            if self.calls % 23 == 0:
                raise RuntimeError("transient query failure")
            return await super().instant(query, at)

    backfiller = Backfiller(
        make_collector(FlakyPrometheus()),
        MetricsWriter(FakeDatabase(), FakeBus()),  # type: ignore[arg-type]
    )
    result = await backfiller.run(hours=2, step_seconds=300)

    # Some points are lost, but the range completes rather than aborting partway.
    assert result.rows_written > 0
    assert result.collected_points < result.requested_points


async def test_service_whose_rate_window_is_still_filling_is_skipped() -> None:
    """Regression: after a stack restart rate5m ramps from ~0 to the real value over five
    minutes; stored, that read as an 18-sigma traffic drop on every service at once."""
    from metrics_agent.collector import WINDOW_COVERAGE_QUERY

    values = full_values()
    values[WINDOW_COVERAGE_QUERY] = {"central-ledger": 20.0, "quoting-service": 4.0}
    result = await make_collector(FakePrometheus(values), interval=15).collect()

    assert [s.service for s in result.samples] == ["central-ledger"]
    assert result.services_incomplete == ["quoting-service"]
    assert result.services_down == []


async def test_unknown_window_coverage_does_not_block_collection() -> None:
    # The coverage query returning nothing (e.g. a renamed counter) must not silence the agent.
    result = await make_collector(FakePrometheus(full_values()), interval=15).collect()
    assert len(result.samples) == 2


async def test_missing_core_gauge_skips_the_row_instead_of_writing_zero() -> None:
    """Regression: right after a simulator restart its gauges had no value yet; the 0.0
    default was stored and read as a -20 sigma CPU/memory/connections collapse."""
    values = full_values()
    values[GAUGE_QUERIES["memory_utilisation_ratio"]] = {"central-ledger": 0.5}
    result = await make_collector(FakePrometheus(values)).collect()
    assert [s.service for s in result.samples] == ["central-ledger"]
    assert result.services_incomplete == ["quoting-service"]


async def test_restart_inside_rate_window_is_tagged_not_dropped() -> None:
    """Rows are kept (SLO checks need them) but tagged so the detector skips the artefact."""
    from metrics_agent.collector import RESET_QUERY, RESTART_IN_WINDOW_LABEL

    values = full_values()
    values[RESET_QUERY] = {"central-ledger": 0.0, "quoting-service": 2.0}
    result = await make_collector(FakePrometheus(values)).collect()
    by_service = {s.service: s for s in result.samples}
    assert set(by_service) == set(SERVICES)
    assert by_service["quoting-service"].labels.get(RESTART_IN_WINDOW_LABEL) == "1"
    assert RESTART_IN_WINDOW_LABEL not in by_service["central-ledger"].labels


async def test_callers_of_a_restarted_dependency_are_tagged() -> None:
    """Regression: after a remediation restart, callers alarmed for five minutes on the calls
    that failed while the dependency was down. quoting-service calls central-ledger."""
    from metrics_agent.collector import (
        DEPENDENCY_RESTART_LABEL,
        RESET_QUERY,
        RESTART_IN_WINDOW_LABEL,
    )

    values = full_values()
    values[RESET_QUERY] = {"central-ledger": 1.0, "quoting-service": 0.0}
    by_service = {
        s.service: s for s in (await make_collector(FakePrometheus(values)).collect()).samples
    }
    assert by_service["quoting-service"].labels.get(DEPENDENCY_RESTART_LABEL) == "1"
    assert RESTART_IN_WINDOW_LABEL not in by_service["quoting-service"].labels
    assert DEPENDENCY_RESTART_LABEL not in by_service["central-ledger"].labels


async def test_recently_started_process_is_tagged_without_a_counter_reset() -> None:
    """Regression: after a VM/cluster restart Prometheus restarted too and saw no reset, so
    the first samples were scored on rates still filling and read as a -3 sigma traffic drop.
    A process younger than the rate window is tagged whatever resets() says."""
    from metrics_agent.collector import (
        DEPENDENCY_RESTART_LABEL,
        PROCESS_AGE_QUERY,
        RESTART_IN_WINDOW_LABEL,
    )

    values = full_values()
    values[PROCESS_AGE_QUERY] = {"central-ledger": 90.0, "quoting-service": 3600.0}
    by_service = {
        s.service: s for s in (await make_collector(FakePrometheus(values)).collect()).samples
    }
    assert by_service["central-ledger"].labels.get(RESTART_IN_WINDOW_LABEL) == "1"
    assert RESTART_IN_WINDOW_LABEL not in by_service["quoting-service"].labels
    # quoting-service calls central-ledger
    assert by_service["quoting-service"].labels.get(DEPENDENCY_RESTART_LABEL) == "1"


def test_window_coverage_is_measured_for_the_service_not_per_pod() -> None:
    """Regressions both ways: per pod, a service was skipped for five minutes after every
    rolling restart (new pod, new series); summed across pods, two pods reached "full" after
    half the window and the half-filled rate read as a traffic drop. Coverage counts the
    steps at which the service as a whole had data."""
    from metrics_agent.collector import WINDOW_COVERAGE_QUERY

    assert WINDOW_COVERAGE_QUERY == (
        "count_over_time(sum by (service) (dpi_transaction_total)[5m:15s])"
    )
