"""The collection loop and its self-instrumentation (module 3).

An AIOps platform that stops collecting and says nothing is worse than no platform, because
every dashboard keeps rendering the last values it saw. So the agent measures itself: how
long a cycle took, how many rows it wrote, how many cycles failed and why. Those series are
exported on `/metrics` and scraped by the same Prometheus the agent reads from, which means
the platform can alert on its own blindness.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

from .collector import MetricsCollector
from .writer import MetricsWriter

log = get_logger(__name__)


@dataclass
class AgentStatus:
    """Last-cycle state, exposed on `/status` for humans and the check script."""

    running: bool = False
    cycles: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    rows_written: int = 0
    events_published: int = 0
    last_collected_at: datetime | None = None
    last_duration_seconds: float = 0.0
    last_error: str | None = None
    last_missing_rules: list[str] = field(default_factory=list)
    last_services_down: list[str] = field(default_factory=list)
    last_services_incomplete: list[str] = field(default_factory=list)
    conflicting_producers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "cycles": self.cycles,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "rows_written": self.rows_written,
            "events_published": self.events_published,
            "last_collected_at": (
                self.last_collected_at.isoformat() if self.last_collected_at else None
            ),
            "last_duration_seconds": round(self.last_duration_seconds, 4),
            "last_error": self.last_error,
            "last_missing_rules": self.last_missing_rules,
            "last_services_down": self.last_services_down,
            "last_services_incomplete": self.last_services_incomplete,
            "conflicting_producers": self.conflicting_producers,
        }


class AgentMetrics:
    """Prometheus series describing the agent's own behaviour."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.cycles = Counter(
            "aiops_collection_cycles_total",
            "Collection cycles attempted, by outcome",
            ["outcome"],
            registry=registry,
        )
        self.duration = Histogram(
            "aiops_collection_duration_seconds",
            "Wall-clock time for one collection cycle",
            # Upper buckets sit above the 15s interval on purpose: a cycle that takes
            # longer than the interval is the condition that matters, and a histogram that
            # tops out below it cannot show that it happened.
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0),
            registry=registry,
        )
        self.rows = Counter(
            "aiops_collection_rows_written_total",
            "Metric rows written to TimescaleDB",
            registry=registry,
        )
        self.events = Counter(
            "aiops_collection_events_published_total",
            "MetricSample events published to the bus",
            registry=registry,
        )
        self.errors = Counter(
            "aiops_collection_errors_total",
            "Collection failures, by stage",
            ["stage"],
            registry=registry,
        )
        self.missing_rules = Gauge(
            "aiops_collection_missing_rules",
            "Recording rules that returned no data in the last cycle",
            registry=registry,
        )
        self.services_collected = Gauge(
            "aiops_collection_services",
            "Services that produced a sample in the last cycle",
            registry=registry,
        )
        self.services_skipped = Gauge(
            "aiops_collection_services_skipped",
            "Services that are up but whose essential signal was unavailable, so no row "
            "was written rather than a false zero",
            registry=registry,
        )
        self.last_success = Gauge(
            "aiops_collection_last_success_timestamp_seconds",
            "Unix time of the last fully successful cycle",
            registry=registry,
        )
        # Reported as a gauge so it can be alerted on directly. A count that never resets
        # cannot distinguish "failed 20 times last week" from "failing right now".
        self.consecutive_failures = Gauge(
            "aiops_collection_consecutive_failures",
            "Cycles failed in a row; 0 after any success",
            registry=registry,
        )


class MetricsAgent:
    """Runs `collect -> write -> publish` on a fixed schedule."""

    def __init__(
        self,
        collector: MetricsCollector,
        writer: MetricsWriter,
        metrics: AgentMetrics,
        *,
        interval_seconds: int,
    ) -> None:
        self._collector = collector
        self._writer = writer
        self._metrics = metrics
        self._interval = interval_seconds
        self.status = AgentStatus()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="metrics-agent-loop")
            self.status.running = True
            log.info("agent.started", interval_seconds=self._interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.running = False
        log.info("agent.stopped", cycles=self.status.cycles, failures=self.status.failures)

    async def _run(self) -> None:
        # Check for a competing producer once, at start-up rather than every cycle: the
        # answer only changes when someone runs the seeder, and querying it 4 times a
        # minute would be a pointless load on the same table the agent is writing to.
        with contextlib.suppress(Exception):
            self.status.conflicting_producers = await self._writer.detect_conflicting_producer()

        while not self._stopping.is_set():
            started = time.perf_counter()
            try:
                await self.run_once()
            except Exception:
                # A failed cycle must never end the loop. Prometheus can be restarting, the
                # database can be failing over; the correct response is to record it and
                # try again next tick.
                log.exception("agent.cycle_failed")
            # Subtract the work from the sleep so the cadence stays honest. Sleeping a full
            # interval after a 3-second cycle would drift the schedule and make the gaps
            # between rows uneven, which shows up later as phantom seasonality in the
            # forecaster's training data.
            elapsed = time.perf_counter() - started
            delay = max(0.0, self._interval - elapsed)
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    # -- one cycle ---------------------------------------------------------
    async def run_once(self, at: datetime | None = None) -> AgentStatus:
        """Collect, store and publish once. Also the handler behind `POST /collect`."""
        started = time.perf_counter()
        stage = "collect"
        try:
            result = await self._collector.collect(at=at)
            stage = "write"
            rows = await self._writer.write(result.samples)
            stage = "publish"
            events = await self._writer.publish(result.samples)
        except Exception as exc:
            self._metrics.errors.labels(stage).inc()
            self._metrics.cycles.labels("failure").inc()
            self.status.failures += 1
            self.status.consecutive_failures += 1
            self._metrics.consecutive_failures.set(self.status.consecutive_failures)
            self.status.last_error = f"{stage}: {exc}"
            log.error("agent.cycle_error", stage=stage, error=str(exc))
            raise

        duration = time.perf_counter() - started
        self._metrics.duration.observe(duration)
        self._metrics.rows.inc(rows)
        self._metrics.events.inc(events)
        self._metrics.cycles.labels("success").inc()
        self._metrics.missing_rules.set(len(result.missing_rules))
        self._metrics.services_collected.set(result.service_count)
        self._metrics.services_skipped.set(len(result.services_incomplete))
        self._metrics.last_success.set(utc_now().timestamp())
        self._metrics.consecutive_failures.set(0)

        self.status.cycles += 1
        self.status.consecutive_failures = 0
        self.status.rows_written += rows
        self.status.events_published += events
        self.status.last_collected_at = result.collected_at
        self.status.last_duration_seconds = duration
        self.status.last_error = None
        self.status.last_missing_rules = result.missing_rules
        self.status.last_services_down = result.services_down
        self.status.last_services_incomplete = result.services_incomplete

        log.info(
            "agent.cycle_complete",
            rows=rows,
            events=events,
            services=result.service_count,
            duration_seconds=round(duration, 3),
            missing_rules=len(result.missing_rules),
            services_skipped=len(result.services_incomplete),
        )
        return self.status
