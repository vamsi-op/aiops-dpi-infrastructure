"""Verify the metrics collection agent end to end (module 3).

Checks, in order:

1. The agent is up, ready, and reports all three dependencies reachable.
2. Every recording rule the agent depends on actually returns data. The agent publishes its
   own query map on `/queries`, so this reads that rather than keeping a second copy that
   could drift.
3. `service_metrics` row count **grows** as a direct result of a collection cycle - driven
   by `POST /collect` rather than by waiting and hoping the timer fired.
4. The rows are real: one per up service, tagged as the agent's, with counts and the stored
   error ratio agreeing.
5. The stored values match what Prometheus reports right now. This is the check that would
   catch a normalisation bug, and nothing in the unit tests can: they assert the agent is
   self-consistent, not that it agrees with reality.
6. `aiops.metrics` received an event per sample.
7. `service_metrics_1m` populates, so the continuous aggregate the dashboard and the
   forecaster read is actually being filled.
8. The agent's own `/metrics` exposes collection latency and error counters.
9. The seeder is not writing the same window - which would silently double the platform's
   reported traffic.

Unlike the other check scripts this one imports the shared library, because half of what it
verifies lives in TimescaleDB and Redis rather than behind an HTTP endpoint. Run it with the
project virtualenv, where `aiops_common` is installed editable:

    .\\.venv\\Scripts\\python.exe scripts\\check_metrics_agent.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from aiops_common.bus import EventBus
from aiops_common.constants import TABLE_METRICS, Stream
from aiops_common.db import Database

AGENT = "http://localhost:8020"
PROMETHEUS = "http://localhost:9090"

failures: list[str] = []
warnings: list[str] = []


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(message: str) -> None:
    print(f"  [PASS] {message}")


def fail(message: str) -> None:
    print(f"  [FAIL] {message}")
    failures.append(message)


def warn(message: str) -> None:
    print(f"  [WARN] {message}")
    warnings.append(message)


def get_json(url: str, *, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def post_json(url: str, body: dict[str, Any] | None = None, *, timeout: float = 120.0) -> Any:
    data = json.dumps(body).encode() if body is not None else b""
    request = urllib.request.Request(  # noqa: S310
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def get_text(url: str, *, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read().decode()


def promql(expr: str) -> list[dict]:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    body = get_json(url)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "query failed"))
    return body["data"]["result"]


# ---------------------------------------------------------------------------
def check_agent_up() -> bool:
    section("Agent health")
    try:
        root = get_json(f"{AGENT}/", timeout=10)
    except OSError as exc:
        fail(f"metrics agent unreachable at {AGENT} ({exc}) - is -Layer all running?")
        return False
    ok(f"agent up, interval {root.get('interval_seconds')}s")

    try:
        ready = get_json(f"{AGENT}/ready", timeout=20)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        fail(f"/ready returned {exc.code}: {detail}")
        return False
    except OSError as exc:
        fail(f"/ready unreachable ({exc})")
        return False

    for name, state in (ready.get("dependencies") or {}).items():
        if state == "ok":
            ok(f"dependency {name}")
        else:
            fail(f"dependency {name}: {state}")
    return not failures


def check_queries_return_data() -> None:
    """Every rule the agent reads must have data, read from the agent's own query map."""
    section("Recording rules the agent depends on")
    try:
        contract = get_json(f"{AGENT}/queries")
    except OSError as exc:
        fail(f"could not read /queries ({exc})")
        return

    queries = [contract["discovery"], contract["error_ratio"], contract["replicas"]]
    queries.extend(contract["gauges"].values())

    # Two rules legitimately have data for only one service - only account-lookup-service
    # emits verification latency, and only quoting-service emits quote latency.
    single_service = {"dpi:verification_latency_p95:5m", "dpi:quote_latency_p95:5m"}

    for query in queries:
        try:
            result = promql(query)
        except (OSError, RuntimeError) as exc:
            fail(f"{query}: {exc}")
            continue
        if result:
            ok(f"{query} -> {len(result)} series")
        elif query in single_service:
            warn(f"{query} returned no data (only one service emits it)")
        else:
            fail(f"{query} returned no data - the agent will write zeros for this field")


async def check_rows_grow(db: Database) -> dict[str, Any]:
    """Row count must increase because a cycle ran, not because time passed."""
    section("Rows are written")
    before = int(await db.fetch_value(f"SELECT count(*) FROM {TABLE_METRICS}") or 0)
    print(f"  [INFO] {before} rows before")

    try:
        status = post_json(f"{AGENT}/collect")
    except urllib.error.HTTPError as exc:
        fail(f"POST /collect returned {exc.code}: {exc.read().decode()[:300]}")
        return {}
    except OSError as exc:
        fail(f"POST /collect failed ({exc})")
        return {}

    after = int(await db.fetch_value(f"SELECT count(*) FROM {TABLE_METRICS}") or 0)
    written = status.get("rows_written", 0)

    if after > before:
        ok(f"row count grew {before} -> {after} after one cycle")
    else:
        # Not automatically a failure: a cycle repeated inside the same whole second hits
        # the same primary key and correctly updates in place rather than inserting.
        warn(
            f"row count unchanged at {after}. Expected if this cycle landed in the same "
            "second as the previous one - ON CONFLICT DO UPDATE overwrites in place"
        )

    if status.get("last_error"):
        fail(f"cycle reported an error: {status['last_error']}")
    else:
        ok(f"cycle clean: {written} rows written cumulatively, {status.get('cycles')} cycles")

    if status.get("last_missing_rules"):
        fail(f"rules returned no data: {status['last_missing_rules']}")
    if status.get("last_services_down"):
        warn(f"services down and therefore not collected: {status['last_services_down']}")
    if status.get("last_services_incomplete"):
        # Expected for a few minutes after Prometheus restarts, because rate() rules need
        # history behind them. Persisting past that means the recording rules are broken.
        warn(
            "services up but skipped for lack of a tps value: "
            f"{status['last_services_incomplete']} - normal for ~5 minutes after a "
            "Prometheus restart, a real problem if it persists"
        )

    return status


async def check_rows_are_sane(db: Database) -> None:
    section("Rows are correct")
    rows = await db.fetch_all(
        f"""
        SELECT service, instance, tps, request_count, error_count, error_ratio,
               latency_p99_seconds, cpu_utilisation_ratio, memory_utilisation_ratio,
               replicas, labels, "timestamp"
        FROM {TABLE_METRICS}
        WHERE labels->>'source' = 'metrics-agent'
        ORDER BY "timestamp" DESC, service
        LIMIT 20
        """
    )
    if not rows:
        fail("no rows tagged source=metrics-agent - the agent has not written anything")
        return

    newest = max(row["timestamp"] for row in rows)
    latest = [row for row in rows if row["timestamp"] == newest]
    ok(f"{len(latest)} services in the newest window ({newest.isoformat()})")

    for row in latest:
        service = row["service"]
        problems = []
        if float(row["tps"] or 0) <= 0:
            problems.append("tps is zero")
        if int(row["request_count"] or 0) <= 0:
            problems.append("request_count is zero")
        if int(row["error_count"] or 0) > int(row["request_count"] or 0):
            problems.append("error_count exceeds request_count")
        # The stored ratio must equal the counts it was derived from, or the dashboard and
        # the anomaly detector are reading two different numbers.
        requests = int(row["request_count"] or 0)
        expected = (int(row["error_count"] or 0) / requests) if requests else 0.0
        if abs(float(row["error_ratio"] or 0) - expected) > 1e-6:
            problems.append(
                f"error_ratio {row['error_ratio']} disagrees with counts ({expected:.6f})"
            )
        if int(row["replicas"] or 0) < 1:
            problems.append("replicas below 1")

        if problems:
            fail(f"{service}: {', '.join(problems)}")
        else:
            ok(
                f"{service}: {float(row['tps']):.2f} tps, "
                f"{requests} txns, ratio {float(row['error_ratio']):.4f}, "
                f"instance={row['instance']}"
            )


async def check_matches_prometheus(db: Database) -> None:
    """The stored value must agree with Prometheus, not merely be self-consistent."""
    section("Stored values agree with Prometheus")
    try:
        live = {
            item["metric"]["service"]: float(item["value"][1])
            for item in promql("dpi:tps:rate5m")
            if "service" in item["metric"]
        }
    except (OSError, RuntimeError) as exc:
        fail(f"could not read dpi:tps:rate5m ({exc})")
        return

    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT ON (service) service, tps, "timestamp"
        FROM {TABLE_METRICS}
        WHERE labels->>'source' = 'metrics-agent'
        ORDER BY service, "timestamp" DESC
        """
    )
    if not rows:
        fail("no agent rows to compare")
        return

    for row in rows:
        service = row["service"]
        stored = float(row["tps"] or 0)
        current = live.get(service)
        if current is None:
            warn(f"{service}: no live dpi:tps:rate5m series to compare against")
            continue
        # Generous tolerance. The stored row was collected a moment ago and traffic follows
        # a moving curve, so an exact match would be suspicious rather than reassuring.
        if current <= 0:
            warn(f"{service}: Prometheus reports zero traffic")
        elif abs(stored - current) / current < 0.25:
            ok(f"{service}: stored {stored:.2f} tps ~ live {current:.2f} tps")
        else:
            fail(f"{service}: stored {stored:.2f} tps disagrees with live {current:.2f} tps")


async def check_bus(bus: EventBus) -> None:
    section("Event bus")
    try:
        length = await bus.client.xlen(str(Stream.METRICS))
    except Exception as exc:  # noqa: BLE001
        fail(f"could not read {Stream.METRICS} ({exc})")
        return

    if length <= 0:
        fail(f"{Stream.METRICS} is empty - the anomaly detector would have nothing to read")
        return
    ok(f"{Stream.METRICS} holds {length} events")

    entries = await bus.client.xrevrange(str(Stream.METRICS), count=1)
    if not entries:
        fail("stream reports a length but returned no entries")
        return

    _message_id, fields = entries[0]
    event_type = fields.get("type")
    if event_type != "MetricSample":
        fail(f"newest event is {event_type!r}, expected 'MetricSample'")
        return

    payload = json.loads(fields.get("data", "{}"))
    required = {"service", "timestamp", "tps", "error_ratio", "replicas"}
    missing = sorted(required - set(payload))
    if missing:
        fail(f"published event is missing fields the detector needs: {missing}")
    else:
        ok(
            f"newest event: {payload['service']} at {payload['timestamp']}, "
            f"tps={payload['tps']}"
        )


async def check_continuous_aggregate(db: Database) -> None:
    """The 1-minute rollup is what the dashboard charts and module 5 actually read."""
    section("Continuous aggregate")
    # Refresh explicitly rather than waiting up to a minute for the policy to fire. The
    # policy's existence is a schema concern; what matters here is that the view can be
    # filled from the agent's rows at all.
    #
    # Bounded to the policy's own start_offset (2 hours) and stopping 30s short of now, for
    # the same reason the policy does: a bucket that is still being written must not be
    # materialised half-formed.
    now = datetime.now(tz=UTC)
    try:
        await db.refresh_continuous_aggregate(
            "service_metrics_1m",
            now - timedelta(hours=2),
            now - timedelta(seconds=30),
        )
        ok("refreshed service_metrics_1m")
    except Exception as exc:  # noqa: BLE001 - the row count below is the real check
        warn(f"could not refresh service_metrics_1m ({exc}); reading whatever exists")

    rows = await db.fetch_all(
        """
        SELECT service, bucket, avg_tps, request_count, sample_count
        FROM service_metrics_1m
        ORDER BY bucket DESC
        LIMIT 10
        """
    )
    if not rows:
        fail(
            "service_metrics_1m is empty. The agent's rows are not reaching the rollup, so "
            "the dashboard charts and module 5's training set would both be blank"
        )
        return

    ok(f"service_metrics_1m has data, newest bucket {rows[0]['bucket'].isoformat()}")
    for row in rows[:5]:
        print(
            f"         {row['service']:28} avg_tps={float(row['avg_tps'] or 0):7.2f} "
            f"samples={row['sample_count']}"
        )

    # The rollups must cover the raw data, not just exist.
    #
    # Both continuous aggregate policies have a bounded `start_offset` - 2 hours for the
    # 1-minute view and 3 days for the 1-hour one - so the scheduled refresh never touches
    # older buckets. Load 28 days of history and `service_metrics_1h` keeps three unless
    # something refreshes the rest explicitly. Module 5 would then train on a ninth of its
    # data with every component reporting healthy, which is why "has data" is not enough.
    raw = (
        await db.fetch_all(
            f'SELECT min("timestamp") AS a, max("timestamp") AS b FROM {TABLE_METRICS}'
        )
    )[0]
    # Compared at the *oldest* bucket, not on total span. The bug this guards against is a
    # bounded `start_offset` leaving old history unmaterialised, which shows up as a rollup
    # that begins days after the raw data does. The newest bucket legitimately lags, because
    # both policies hold an `end_offset` back so a bucket still being written is never
    # materialised - so asserting on total span fails for a correct system, which is exactly
    # what the first version of this check did.
    for view, bucket_seconds in (("service_metrics_1m", 60), ("service_metrics_1h", 3600)):
        span = (await db.fetch_all(f"SELECT min(bucket) AS a, max(bucket) AS b FROM {view}"))[0]
        if not span["a"]:
            fail(f"{view} is empty")
            continue

        raw_days = (raw["b"] - raw["a"]).total_seconds() / 86400
        # One bucket width of slack: `time_bucket` floors to a boundary, so the first bucket
        # can only begin at or after the first raw row.
        drift = (span["a"] - raw["a"]).total_seconds()
        if drift <= bucket_seconds:
            ok(
                f"{view} starts within one bucket of the raw data "
                f"({raw_days:.1f}d of history materialised)"
            )
        else:
            fail(
                f"{view} starts {drift / 86400:.1f}d after the raw data, which goes back "
                f"{raw_days:.1f}d. A refresh policy's start_offset will not backfill this - "
                "refresh_continuous_aggregate over the full range "
                "(scripts/replay_history.py does this)"
            )

        # Trailing edge, checked separately and generously.
        lag = (raw["b"] - span["b"]).total_seconds()
        if lag > max(4 * bucket_seconds, 900):
            warn(
                f"{view} newest bucket lags the newest raw row by {lag / 60:.0f} min, "
                "more than the policy's end_offset explains"
            )


async def check_no_double_counting(db: Database) -> None:
    """Exactly one instance per service inside the gateway's aggregation window.

    `DashboardRepository.service_health` **sums** `tps` across the instances reporting within
    120 seconds of a service's newest sample. Three producers can write this table - the
    agent (`<service>:8000`), the replay tool (`<service>:replay`) and the seeder
    (`seed-<service>-0`) - and any two of them landing inside that window inflates reported
    platform traffic with no error anywhere to explain it. This is the check that catches it.
    """
    section("No double-counted instances")
    rows = await db.fetch_all(
        f"""
        WITH anchor AS (
            SELECT service, max("timestamp") AS last_seen
            FROM {TABLE_METRICS}
            WHERE "timestamp" > now() - interval '24 hours'
            GROUP BY service
        ),
        in_window AS (
            SELECT DISTINCT ON (m.service, m.instance)
                m.service, m.instance, m.tps
            FROM {TABLE_METRICS} m
            JOIN anchor a ON a.service = m.service
            WHERE m."timestamp" > a.last_seen - make_interval(secs => 120)
            ORDER BY m.service, m.instance, m."timestamp" DESC
        )
        SELECT service, count(*) AS instances, string_agg(instance, ', ') AS which,
               round(sum(tps)::numeric, 2) AS summed_tps
        FROM in_window GROUP BY service ORDER BY service
        """
    )
    if not rows:
        warn("no recent rows to check for overlap")
        return

    for row in rows:
        if int(row["instances"]) == 1:
            ok(f"{row['service']}: {float(row['summed_tps']):.2f} tps from one instance")
        else:
            fail(
                f"{row['service']}: {row['instances']} instances would be summed to "
                f"{float(row['summed_tps']):.2f} tps ({row['which']}). Reported traffic is "
                "inflated - clear the producer that should not be writing this window"
            )


def check_self_metrics() -> None:
    """The module's stated criterion: collection latency and error counters on /metrics."""
    section("Agent self-instrumentation")
    try:
        text = get_text(f"{AGENT}/metrics")
    except OSError as exc:
        fail(f"could not scrape the agent's /metrics ({exc})")
        return

    expected = {
        "aiops_collection_cycles_total": "cycles attempted, by outcome",
        "aiops_collection_duration_seconds": "collection latency histogram",
        "aiops_collection_rows_written_total": "rows written",
        "aiops_collection_events_published_total": "events published",
        "aiops_collection_errors_total": "errors by stage",
        "aiops_collection_missing_rules": "rules returning no data",
        "aiops_collection_services": "services collected",
        "aiops_collection_services_skipped": "services skipped for lack of a signal",
        "aiops_collection_last_success_timestamp_seconds": "last success",
        "aiops_collection_consecutive_failures": "consecutive failures",
    }
    for series, description in expected.items():
        if series in text:
            ok(f"{series} ({description})")
        else:
            fail(f"{series} missing from /metrics")

    # Prometheus must also be scraping it, or none of the above is usable for alerting.
    try:
        scraped = promql('up{job="aiops"} == 1')
    except (OSError, RuntimeError) as exc:
        warn(f"could not confirm Prometheus is scraping the agent ({exc})")
        return
    instances = {item["metric"].get("instance", "") for item in scraped}
    if any("metrics-agent" in instance for instance in instances):
        ok("Prometheus is scraping the agent")
    else:
        warn(
            "Prometheus is not scraping metrics-agent:8000 yet - the collection series "
            "exist but nothing can alert on them"
        )


async def check_no_conflicting_producer(db: Database, status: dict[str, Any]) -> None:
    section("No competing producer")
    conflicts = status.get("conflicting_producers") or []
    seeded = int(
        await db.fetch_value(
            f"""
            SELECT count(*) FROM {TABLE_METRICS}
            WHERE instance LIKE 'seed-%'
              AND "timestamp" > now() - interval '5 minutes'
            """
        )
        or 0
    )
    if seeded or conflicts:
        fail(
            f"{seeded} seeded row(s) in the last 5 minutes (agent reported {conflicts}). "
            "The dashboard sums tps across instances, so reported traffic is inflated. "
            "Reset the database (.\\scripts\\stack.ps1 reset) to remove them."
        )
    else:
        ok("no seeded rows in the recent window; the agent is the only producer")


async def main() -> int:
    print("Metrics agent check (module 3)")
    if not check_agent_up():
        print("\nAgent is not healthy; skipping the rest.")
        return 1

    check_queries_return_data()

    db = Database()
    bus = EventBus(source="check-metrics-agent")
    await db.connect()
    await bus.connect()
    try:
        status = await check_rows_grow(db)
        await check_rows_are_sane(db)
        await check_matches_prometheus(db)
        await check_bus(bus)
        await check_continuous_aggregate(db)
        await check_no_double_counting(db)
        check_self_metrics()
        await check_no_conflicting_producer(db, status)
    finally:
        await bus.close()
        await db.close()

    print()
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for warning in warnings:
            print(f"  - {warning}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("=" * 48)
    print(" All metrics agent checks passed. Module 3 OK.")
    print("=" * 48)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
