"""Persist `MetricSample` rows and announce them on the event bus (module 3).

Two destinations, deliberately in this order:

1. **TimescaleDB** - the durable store the models train on and the dashboard reads.
2. **`aiops.metrics`** - the live stream the anomaly detector consumes.

The database write happens first. If publishing fails the data is still recorded and the
detector simply lags; if the order were reversed, a detector could raise an anomaly
referencing a window that was never stored, and the RCA engine would later find nothing
behind it.
"""

from __future__ import annotations

import json
from typing import Any, Final

from aiops_common.bus import EventBus
from aiops_common.constants import TABLE_METRICS, Stream
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import MetricSample

log = get_logger(__name__)

# Column order is shared with scripts/seed_demo_data.py on purpose. Both write the same
# table, and the dashboard cannot tell them apart - so if these two drift, the symptom is a
# dashboard that renders differently depending on which producer filled the window.
METRIC_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp",
    "service",
    "instance",
    "platform",
    "tps",
    "request_count",
    "error_count",
    "error_ratio",
    "latency_p50_seconds",
    "latency_p95_seconds",
    "latency_p99_seconds",
    "cpu_utilisation_ratio",
    "memory_utilisation_ratio",
    "queue_depth",
    "active_connections",
    "settlement_lag_seconds",
    "quote_latency_seconds",
    "verification_latency_seconds",
    "replicas",
    # Included where the seeder's equivalent list stops. The seeder marks its rows with a
    # `seed-` instance prefix; the agent marks its own here, and the marking is load-bearing
    # rather than decorative: `detect_conflicting_producer` and every diagnostic query that
    # asks "who wrote this row" depend on it. Leaving it out once already made the agent's
    # rows indistinguishable from the seeder's.
    "labels",
)

# jsonb columns cannot take a plain bind parameter - asyncpg sends a Python dict as text and
# Postgres refuses to coerce it - so the value is serialised in `to_row` and cast here.
_JSONB_COLUMNS = frozenset({"labels"})

_COLUMN_LIST = ", ".join(f'"{column}"' for column in METRIC_COLUMNS)
_VALUE_LIST = ", ".join(
    f"CAST(:{column} AS jsonb)" if column in _JSONB_COLUMNS else f":{column}"
    for column in METRIC_COLUMNS
)

# DO UPDATE, not DO NOTHING.
#
# The seeder uses DO NOTHING because it generates history once. The agent re-collects on a
# schedule and may retry a cycle after a transient database error, so the same
# (timestamp, service, instance) can legitimately arrive twice - and the second attempt
# carries the better data, because Prometheus rate() windows fill in as scrapes land.
# DO NOTHING would permanently freeze whichever attempt happened to land first.
INSERT_METRICS: Final[str] = f"""
INSERT INTO {TABLE_METRICS} ({_COLUMN_LIST})
VALUES ({_VALUE_LIST})
ON CONFLICT ("timestamp", service, instance) DO UPDATE SET
    tps = EXCLUDED.tps,
    request_count = EXCLUDED.request_count,
    error_count = EXCLUDED.error_count,
    error_ratio = EXCLUDED.error_ratio,
    latency_p50_seconds = EXCLUDED.latency_p50_seconds,
    latency_p95_seconds = EXCLUDED.latency_p95_seconds,
    latency_p99_seconds = EXCLUDED.latency_p99_seconds,
    cpu_utilisation_ratio = EXCLUDED.cpu_utilisation_ratio,
    memory_utilisation_ratio = EXCLUDED.memory_utilisation_ratio,
    queue_depth = EXCLUDED.queue_depth,
    active_connections = EXCLUDED.active_connections,
    settlement_lag_seconds = EXCLUDED.settlement_lag_seconds,
    quote_latency_seconds = EXCLUDED.quote_latency_seconds,
    verification_latency_seconds = EXCLUDED.verification_latency_seconds,
    replicas = EXCLUDED.replicas,
    labels = EXCLUDED.labels
"""

# Rows the seeder created. The agent checks for these once at start-up; see
# `detect_conflicting_producer`.
SEED_INSTANCE_LIKE: Final[str] = "seed-%"


def to_row(sample: MetricSample) -> dict[str, Any]:
    """Flatten a sample into insert parameters.

    `error_ratio` is a computed property on the model, not a stored field, so it has to be
    read explicitly rather than picked up by iterating the model's fields. It is a real
    column because the dashboard and the 1-minute rollup both aggregate on it, and
    recomputing it in SQL on every read would be slower and easy to get subtly different.
    """
    row: dict[str, Any] = {column: getattr(sample, column) for column in METRIC_COLUMNS}
    row["error_ratio"] = sample.error_ratio
    row["labels"] = json.dumps(sample.labels or {})
    return row


class MetricsWriter:
    """Writes collected samples to TimescaleDB and publishes them to the bus."""

    def __init__(self, database: Database, bus: EventBus) -> None:
        self._db = database
        self._bus = bus

    async def write(self, samples: list[MetricSample]) -> int:
        """Batch-insert samples. Returns rows written."""
        if not samples:
            return 0
        rows = [to_row(sample) for sample in samples]
        return await self._db.execute_many(INSERT_METRICS, rows)

    async def publish(self, samples: list[MetricSample]) -> int:
        """Announce each sample on `aiops.metrics`. Returns events published.

        One event per service rather than one batched event per cycle. The anomaly detector
        scores a single service at a time, so a batched envelope would force every consumer
        to unpack and re-fan-out, and a per-service event lets a consumer group distribute
        services across workers later without a format change.
        """
        published = 0
        for sample in samples:
            try:
                await self._bus.publish(
                    Stream.METRICS, sample, event_type=type(sample).__name__
                )
                published += 1
            except Exception:  # noqa: BLE001 - a bus failure must not lose the DB write
                log.warning("publish.failed", monitored_service=sample.service, exc_info=True)
        return published

    async def detect_conflicting_producer(self, *, within_seconds: int = 300) -> list[str]:
        """Warn if the seeder is also writing recent rows.

        This is worth a dedicated check because the failure is silent and wrong rather than
        loud and broken. `DashboardRepository.service_health` **sums** `tps` across a
        service's instances within a 120-second grace window. The seeder writes
        `seed-<service>-0`; the agent writes the real scrape target. Both present in the
        same window means the dashboard reports roughly double the platform's actual
        traffic, with no error anywhere to explain it.

        Returns the affected service names.
        """
        rows = await self._db.fetch_all(
            f"""
            SELECT DISTINCT service
            FROM {TABLE_METRICS}
            WHERE instance LIKE :pattern
              AND "timestamp" > now() - make_interval(secs => :within)
            ORDER BY service
            """,
            {"pattern": SEED_INSTANCE_LIKE, "within": within_seconds},
        )
        services = [str(row["service"]) for row in rows]
        if services:
            log.warning(
                "writer.conflicting_producer",
                services=services,
                detail=(
                    "seeded rows exist in the same window as agent rows; the dashboard "
                    "sums tps across instances, so reported traffic will be inflated "
                    "until these age out. Run seed_demo_data.py --clear"
                ),
            )
        return services
