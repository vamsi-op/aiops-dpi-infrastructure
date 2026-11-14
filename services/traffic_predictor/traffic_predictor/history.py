"""Load hourly history for forecasting (module 5)."""

from __future__ import annotations

from aiops_common.db import Database
from aiops_common.log import get_logger

from .forecaster import Observation

log = get_logger(__name__)

# Reads the 1-hour continuous aggregate, not the raw table.
#
# That is the whole reason the rollup exists: 28 days of raw 15-second samples is ~160k rows per
# service, and forecasting needs the hourly shape, not the resolution. It also means the training
# set survives the raw table's 30-day retention policy, because the aggregate has no retention of
# its own - which is exactly what the schema comment in 03_timescale.sql intends.
#
# `avg_tps` rather than `max_tps`: the forecast feeds a capacity decision, and sizing to the peak
# *within* every hour on top of a peak *across* hours would compound two safety margins into one
# number nobody can reason about. Headroom is applied once, explicitly, in capacity.py.
SELECT_HOURLY = """
SELECT bucket, avg_tps, sample_count
FROM service_metrics_1h
WHERE service = :service
  AND bucket > now() - make_interval(days => :days)
ORDER BY bucket
"""

SELECT_SERVICES = """
SELECT DISTINCT service
FROM service_metrics_1h
WHERE bucket > now() - make_interval(days => :days)
ORDER BY service
"""

# An hourly bucket assembled from only a handful of samples is usually the first or last bucket of
# a collection window, and its average is not comparable with a full hour's. At a 15-second
# interval a complete hour holds 240 samples; the replay writes 4 at its coarse tier. The floor is
# therefore deliberately low - it excludes fragments, not coarse-but-complete buckets.
MIN_SAMPLES_PER_BUCKET = 2


SELECT_LATEST_TPS = """
SELECT tps
FROM service_metrics
WHERE service = :service
  AND timestamp > now() - interval '10 minutes'
ORDER BY timestamp DESC
LIMIT 1
"""


async def load_latest_tps(db: Database, service: str) -> float | None:
    """The service's most recent live throughput, if it reported in the last 10 minutes."""
    row = await db.fetch_one(SELECT_LATEST_TPS, {"service": service})
    return float(row["tps"]) if row and row.get("tps") is not None else None


async def load_services(db: Database, *, days: int) -> list[str]:
    rows = await db.fetch_all(SELECT_SERVICES, {"days": days})
    return [str(row["service"]) for row in rows]


async def load_history(db: Database, service: str, *, days: int) -> list[Observation]:
    rows = await db.fetch_all(SELECT_HOURLY, {"service": service, "days": days})
    observations = []
    partial = 0
    for row in rows:
        if int(row["sample_count"] or 0) < MIN_SAMPLES_PER_BUCKET:
            partial += 1
            continue
        observations.append(Observation(at=row["bucket"], tps=float(row["avg_tps"] or 0.0)))

    if partial:
        log.debug(
            "history.partial_buckets_skipped",
            monitored_service=service,
            skipped=partial,
            kept=len(observations),
        )
    return observations
