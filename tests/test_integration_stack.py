"""End-to-end checks against the running docker compose stack.

Skipped by default. Enable with:
    $env:AIOPS_RUN_INTEGRATION = '1'; pytest tests -v -m integration

These prove the pieces module 1 delivers actually work together: the Timescale schema
accepts a real `MetricSample`, the continuous aggregate rolls it up, and an event
published on the bus comes back out of a consumer group intact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio

from aiops_common.bus import EventBus
from aiops_common.constants import ANOMALY_FEATURES, TABLE_METRICS, Stream
from aiops_common.db import Database
from aiops_common.errors import StorageError
from aiops_common.schemas import AnomalyEvent, MetricSample, new_id, utc_now

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = Database()
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def bus() -> AsyncIterator[EventBus]:
    event_bus = EventBus(source="integration-test")
    await event_bus.connect()
    yield event_bus
    await event_bus.close()


@pytest_asyncio.fixture
async def temp_stream(bus: EventBus) -> AsyncIterator[str]:
    """A throwaway stream name, deleted afterwards even if the test fails."""
    name = f"{Stream.ANOMALIES}.itest.{new_id('s')}"
    try:
        yield name
    finally:
        await bus.client.delete(name)


@pytest_asyncio.fixture
async def instance(db: Database) -> AsyncIterator[str]:
    """A unique instance label whose rows are always removed afterwards.

    Cleanup lives in the fixture, not at the end of each test: a failing assertion
    must not leave rows behind to skew the next run.
    """
    label = new_id("itest")
    try:
        yield label
    finally:
        await db.execute(
            f"DELETE FROM {TABLE_METRICS} WHERE instance = :instance", {"instance": label}
        )


@pytest_asyncio.fixture
async def synthetic_service(db: Database) -> AsyncIterator[str]:
    """A unique *service* name, for tests that read through a continuous aggregate.

    The rollups group by service, not instance, so a test asserting on aggregate values
    has to own its service name outright or it will pick up whatever the simulator and
    the demo seeder have written for the real Mojaloop services.

    Teardown deletes the rows and re-refreshes the aggregate, so no materialised buckets
    are left behind.
    """
    name = f"itest-{new_id('svc')}"
    start = utc_now() - timedelta(days=1)
    try:
        yield name
    finally:
        await db.execute(
            f"DELETE FROM {TABLE_METRICS} WHERE service = :service", {"service": name}
        )
        await db.refresh_continuous_aggregate("service_metrics_1m", start, utc_now())


INSERT_SQL = f"""
INSERT INTO {TABLE_METRICS} (
    "timestamp", service, instance, platform,
    tps, request_count, error_count, error_ratio,
    latency_p50_seconds, latency_p95_seconds, latency_p99_seconds,
    cpu_utilisation_ratio, memory_utilisation_ratio,
    queue_depth, active_connections,
    settlement_lag_seconds, quote_latency_seconds, verification_latency_seconds,
    replicas
) VALUES (
    :timestamp, :service, :instance, :platform,
    :tps, :request_count, :error_count, :error_ratio,
    :latency_p50_seconds, :latency_p95_seconds, :latency_p99_seconds,
    :cpu_utilisation_ratio, :memory_utilisation_ratio,
    :queue_depth, :active_connections,
    :settlement_lag_seconds, :quote_latency_seconds, :verification_latency_seconds,
    :replicas
)
"""

_METRIC_COLUMNS = (
    "timestamp service instance platform tps request_count error_count error_ratio "
    "latency_p50_seconds latency_p95_seconds latency_p99_seconds "
    "cpu_utilisation_ratio memory_utilisation_ratio queue_depth active_connections "
    "settlement_lag_seconds quote_latency_seconds verification_latency_seconds replicas"
).split()


def _row(sample: MetricSample) -> dict[str, object]:
    """Flatten a sample into insert parameters, including the computed error ratio."""
    dumped = sample.model_dump()
    return {column: dumped[column] for column in _METRIC_COLUMNS}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
async def test_database_is_reachable_and_timescale_is_installed(db: Database) -> None:
    assert await db.ping() is True
    version = await db.timescale_version()
    assert version is not None
    assert version.startswith("2.")


async def test_all_expected_tables_exist(db: Database) -> None:
    for table in (
        "service_metrics",
        "anomalies",
        "traffic_predictions",
        "incidents",
        "healing_actions",
        "deployments",
        "chaos_runs",
        "model_runs",
    ):
        assert await db.table_exists(table), table


async def test_metric_sample_round_trips_through_timescaledb(
    db: Database, instance: str
) -> None:
    sample = MetricSample(
        service="central-ledger",
        instance=instance,
        tps=137.5,
        request_count=2_060,
        error_count=41,
        latency_p50_seconds=0.062,
        latency_p95_seconds=0.41,
        latency_p99_seconds=1.28,
        cpu_utilisation_ratio=0.68,
        memory_utilisation_ratio=0.91,
        queue_depth=73,
        active_connections=118,
        settlement_lag_seconds=4.5,
        replicas=3,
    )

    assert await db.execute(INSERT_SQL, _row(sample)) == 1

    stored = await db.fetch_one(
        f"SELECT * FROM {TABLE_METRICS} WHERE instance = :instance",
        {"instance": instance},
    )
    assert stored is not None
    assert stored["service"] == "central-ledger"
    assert stored["tps"] == pytest.approx(137.5)
    assert stored["replicas"] == 3
    # error_ratio is computed in Python, not the database, so this proves the
    # derived value survives the write path.
    assert stored["error_ratio"] == pytest.approx(41 / 2_060)


async def test_batch_insert_and_continuous_aggregate_refresh(
    db: Database, instance: str, synthetic_service: str
) -> None:
    # Place the whole 10-minute window comfortably in the past. A continuous aggregate
    # only materialises buckets that fall entirely inside the refresh window, so a
    # sample written seconds ago sits in a still-open bucket and would be excluded.
    # Module 3's backfill has to respect the same rule.
    start = utc_now() - timedelta(minutes=30)
    samples = [
        MetricSample(
            timestamp=start + timedelta(seconds=15 * i),
            service=synthetic_service,
            instance=instance,
            tps=50.0 + i,
            request_count=750 + i,
            error_count=i,
            latency_p99_seconds=0.3,
            cpu_utilisation_ratio=0.4,
            memory_utilisation_ratio=0.5,
        )
        for i in range(40)
    ]

    assert await db.execute_many(INSERT_SQL, [_row(s) for s in samples]) == 40

    # Continuous aggregates are refreshed on a schedule; force it so the assertion
    # does not depend on timing.
    await db.refresh_continuous_aggregate(
        "service_metrics_1m",
        start - timedelta(minutes=2),
        utc_now() - timedelta(minutes=1),
    )

    buckets = await db.fetch_all(
        "SELECT bucket, avg_tps, max_tps, sample_count FROM service_metrics_1m "
        "WHERE service = :service AND bucket >= :start ORDER BY bucket",
        {"service": synthetic_service, "start": start - timedelta(minutes=2)},
    )
    assert buckets, "expected the 1-minute rollup to contain buckets"
    assert sum(int(b["sample_count"]) for b in buckets) >= 40
    assert max(float(b["max_tps"]) for b in buckets) == pytest.approx(89.0)


async def test_refresh_rejects_an_unsafe_view_name(db: Database) -> None:
    # The view name is interpolated into the CALL (it cannot be a bind parameter),
    # so it is validated instead of trusted.
    with pytest.raises(StorageError, match="Invalid continuous aggregate name"):
        await db.refresh_continuous_aggregate("service_metrics_1m'); DROP TABLE x; --")


async def test_call_procedure_runs_outside_a_transaction(db: Database) -> None:
    # Proves the autocommit path works: this procedure errors if it is inside one.
    await db.call_procedure(
        "CALL refresh_continuous_aggregate('service_metrics_1h', "
        "CAST(NULL AS timestamptz), CAST(NULL AS timestamptz))"
    )


async def test_latest_view_returns_the_newest_sample(db: Database, instance: str) -> None:
    now = utc_now()
    for offset, tps in ((120, 10.0), (60, 20.0), (5, 30.0)):
        await db.execute(
            INSERT_SQL,
            _row(
                MetricSample(
                    timestamp=now - timedelta(seconds=offset),
                    service="ml-api-adapter",
                    instance=instance,
                    tps=tps,
                )
            ),
        )

    latest = await db.fetch_one(
        "SELECT tps FROM service_metrics_latest WHERE instance = :instance",
        {"instance": instance},
    )
    assert latest is not None
    assert latest["tps"] == pytest.approx(30.0)


async def test_feature_vector_matches_stored_columns(db: Database) -> None:
    """Guards the model contract: every ANOMALY_FEATURES name must be a real column."""
    columns = {
        row["column_name"]
        for row in await db.fetch_all(
            "SELECT column_name FROM information_schema.columns " "WHERE table_name = :table",
            {"table": TABLE_METRICS},
        )
    }
    missing = set(ANOMALY_FEATURES) - columns
    assert not missing, f"features with no column: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------
async def test_bus_is_reachable(bus: EventBus) -> None:
    assert await bus.ping() is True


async def test_publish_and_consume_an_anomaly(bus: EventBus, temp_stream: str) -> None:
    stream = temp_stream
    group = "itest-group"
    anomaly = AnomalyEvent(
        service="central-ledger",
        score=0.88,
        contributing_features=["memory_utilisation_ratio", "queue_depth"],
        description="integration test",
    )

    await bus.ensure_group(stream, group)
    message_id = await bus.publish(stream, anomaly, event_type="anomaly")
    assert message_id

    events = await bus.read(stream, group, "itest-consumer", block_ms=2_000)
    assert len(events) == 1

    event = events[0]
    assert event.type == "anomaly"
    assert event.source == "integration-test"

    received = AnomalyEvent.model_validate(event.data)
    assert received.anomaly_id == anomaly.anomaly_id
    assert received.score == pytest.approx(0.88)
    assert received.is_actionable is True

    await bus.ack(stream, group, event.message_id)
    assert await bus.read(stream, group, "itest-consumer", block_ms=100) == []


async def test_unacked_messages_can_be_reclaimed(bus: EventBus, temp_stream: str) -> None:
    # This is why we use streams rather than pub/sub: a consumer that dies mid-work
    # must not lose the anomaly it was holding.
    stream = temp_stream
    group = "itest-group"
    await bus.ensure_group(stream, group)
    await bus.publish(stream, AnomalyEvent(service="quoting-service", score=0.7))

    read = await bus.read(stream, group, "consumer-that-dies", block_ms=2_000)
    assert len(read) == 1  # read but deliberately never acked

    reclaimed = await bus.reclaim_stale(stream, group, "consumer-that-recovers", min_idle_ms=0)
    assert len(reclaimed) == 1
    assert reclaimed[0].message_id == read[0].message_id


async def test_tail_does_not_lose_events_on_quiet_streams(bus: EventBus) -> None:
    """Regression: XREAD's `$` sentinel used to silently drop messages.

    `$` is re-evaluated as "the stream's end" on every XREAD call, and XREAD returns as
    soon as *any* stream has data. So a stream that had not produced yet kept its `$`,
    and anything published to it between two calls fell into the gap. On the dashboard
    that showed up as incidents and healing actions randomly never appearing.

    Two streams, a message on each, one read at a time: both must survive.
    """
    stream_a = f"{Stream.ANOMALIES}.itest.{new_id('s')}"
    stream_b = f"{Stream.INCIDENTS}.itest.{new_id('s')}"
    try:
        # Establish the cursor while both streams are empty - the exact situation that
        # used to break.
        _events, cursor = await bus.tail([stream_a, stream_b], block_ms=50)

        await bus.publish(stream_a, AnomalyEvent(service="central-ledger", score=0.9))
        await bus.publish(stream_b, AnomalyEvent(service="quoting-service", score=0.8))

        # Drain across several calls, mimicking the relay loop.
        seen: list[str] = []
        for _ in range(4):
            events, cursor = await bus.tail([stream_a, stream_b], last_ids=cursor, block_ms=500)
            seen.extend(event.stream for event in events)
            if len(seen) >= 2:
                break

        assert sorted(seen) == sorted([stream_a, stream_b])
    finally:
        await bus.client.delete(stream_a, stream_b)


async def test_tail_cursor_starts_at_stream_end_not_the_beginning(bus: EventBus) -> None:
    """A new tail must not replay history - that is what the database is for."""
    stream = f"{Stream.ANOMALIES}.itest.{new_id('s')}"
    try:
        for _ in range(3):
            await bus.publish(stream, AnomalyEvent(service="central-ledger", score=0.5))

        events, cursor = await bus.tail([stream], block_ms=50)
        assert events == [], "a fresh tail replayed existing history"

        await bus.publish(stream, AnomalyEvent(service="central-ledger", score=0.99))
        events, _cursor = await bus.tail([stream], last_ids=cursor, block_ms=1_000)
        assert len(events) == 1
        assert AnomalyEvent.model_validate(events[0].data).score == pytest.approx(0.99)
    finally:
        await bus.client.delete(stream)


async def test_rate_budget_counter_expires(bus: EventBus) -> None:
    # Backs the healing engine's hourly action budget.
    key = f"aiops:itest:budget:{new_id('k')}"
    assert await bus.incr_with_ttl(key, ttl_seconds=60) == 1
    assert await bus.incr_with_ttl(key, ttl_seconds=60) == 2
    assert 0 < await bus.client.ttl(key) <= 60
    await bus.delete_flag(key)


async def test_circuit_breaker_flag_lifecycle(bus: EventBus) -> None:
    key = f"aiops:itest:cb:{new_id('k')}"
    assert await bus.get_flag(key) is None
    await bus.set_flag(key, "open", ttl_seconds=30)
    assert await bus.get_flag(key) == "open"
    await bus.delete_flag(key)
    assert await bus.get_flag(key) is None
