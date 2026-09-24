"""Replay Prometheus history into TimescaleDB (module 3).

Why this exists: module 4's Isolation Forest needs `ANOMALY_MIN_TRAINING_ROWS` (2000 by
default) before it will train, and module 5's forecaster needs enough of the daily curve to
tell seasonality from noise. Without a backfill, both sit idle for days after a fresh start
while the agent accumulates rows 15 seconds at a time - and a demo cannot wait.

The backfill is not a second implementation. It steps a timestamp backwards and calls the
same `MetricsCollector.collect(at=...)` the live loop uses, so a backfilled row and a live
row are produced by identical code. The only thing that differs is the clock.

Bounded by what Prometheus actually retains: `--storage.tsdb.retention.time=15d`, and
`rate()` windows need data *before* the requested instant, so the earliest useful point is
retention minus the rule window, not retention exactly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

from .collector import MetricsCollector
from .writer import MetricsWriter

log = get_logger(__name__)

# Rows are written in chunks rather than one transaction for the whole range: a 7-day
# backfill at 60s resolution is ~50k rows across five services, and a single transaction
# that size holds locks on the hypertable long enough to stall live collection.
CHUNK_ROWS = 2_000

# Prometheus is queried concurrently, but not without limit. Each collected instant is a
# dozen instant queries; letting hundreds run at once makes Prometheus the bottleneck and
# can evict the very series being read.
MAX_CONCURRENT_INSTANTS = 8


@dataclass(frozen=True)
class BackfillResult:
    requested_points: int
    collected_points: int
    rows_written: int
    empty_points: int
    start: datetime
    end: datetime

    @property
    def coverage(self) -> float:
        if self.requested_points == 0:
            return 0.0
        return self.collected_points / self.requested_points


class Backfiller:
    """Walks a time range, collecting each instant through the live code path."""

    def __init__(self, collector: MetricsCollector, writer: MetricsWriter) -> None:
        self._collector = collector
        self._writer = writer

    async def run(
        self,
        *,
        hours: int = 24,
        step_seconds: int = 60,
        end: datetime | None = None,
        publish: bool = False,
    ) -> BackfillResult:
        """Backfill `hours` of history at `step_seconds` resolution.

        `publish` defaults to False. Replaying days of history onto `aiops.metrics` would
        flood the stream past `REDIS_STREAM_MAXLEN`, evicting the live events the anomaly
        detector has not read yet - so historical rows go to the database only. The stream
        is for what is happening now; the table is for what happened.
        """
        finish = (end or utc_now()).replace(microsecond=0)
        # One step back from the end. The most recent instant is the live loop's job, and
        # writing it here would race the loop for the same primary key.
        finish -= timedelta(seconds=step_seconds)
        begin = finish - timedelta(hours=hours)

        moments = []
        cursor = begin
        while cursor <= finish:
            moments.append(cursor)
            cursor += timedelta(seconds=step_seconds)

        log.info(
            "backfill.start",
            points=len(moments),
            start=begin.isoformat(),
            end=finish.isoformat(),
            step_seconds=step_seconds,
        )

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_INSTANTS)
        collected = 0
        empty = 0
        written = 0
        pending: list = []

        async def collect_at(moment: datetime) -> list:
            async with semaphore:
                try:
                    return (await self._collector.collect(at=moment)).samples
                except Exception:  # noqa: BLE001 - one bad instant must not end the range
                    log.warning("backfill.point_failed", at=moment.isoformat())
                    return []

        # Gather in slices so memory stays bounded and progress is visible on a long run.
        slice_size = MAX_CONCURRENT_INSTANTS * 8
        for index in range(0, len(moments), slice_size):
            batch = moments[index : index + slice_size]
            slice_samples: list = []
            for samples in await asyncio.gather(*(collect_at(m) for m in batch)):
                if samples:
                    collected += 1
                    slice_samples.extend(samples)
                else:
                    # Expected at the far end of the range, where Prometheus has data but
                    # not enough history behind it for a 5-minute rate to evaluate.
                    empty += 1

            if publish:
                await self._writer.publish(slice_samples)
            pending.extend(slice_samples)

            if len(pending) >= CHUNK_ROWS:
                written += await self._writer.write(pending)
                log.info("backfill.progress", rows=written, points_done=index + len(batch))
                pending = []

        if pending:
            written += await self._writer.write(pending)

        result = BackfillResult(
            requested_points=len(moments),
            collected_points=collected,
            rows_written=written,
            empty_points=empty,
            start=begin,
            end=finish,
        )
        log.info(
            "backfill.complete",
            rows=result.rows_written,
            points=result.collected_points,
            empty=result.empty_points,
            coverage=round(result.coverage, 3),
        )
        return result
