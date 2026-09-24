r"""ASGI entry point for the metrics collection agent (module 3).

Run locally:
    uvicorn metrics_agent.asgi:app --port 8020 --app-dir services/metrics_agent

Or through the stack:
    .\scripts\stack.ps1 up -Layer aiops

The agent closes the loop the platform is built around: Prometheus holds 15 days of
high-resolution metrics, but the models need a queryable feature store with months of
history, and the anomaly detector needs a push stream rather than a polling loop. This
service is the bridge - PromQL in, `service_metrics` rows and `aiops.metrics` events out.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import AiopsService
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.promql import PrometheusClient
from aiops_common.service import build_app
from aiops_common.tracing import configure_tracing, instrument_httpx, shutdown_tracing

from .agent import AgentMetrics, MetricsAgent
from .backfill import Backfiller
from .collector import MetricsCollector
from .routes import router
from .writer import MetricsWriter

SERVICE_NAME = str(AiopsService.METRICS_AGENT)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)
prometheus = PrometheusClient()

INTERVAL_SECONDS = settings.observability.scrape_interval_seconds

collector = MetricsCollector(
    prometheus,
    interval_seconds=INTERVAL_SECONDS,
    platform=settings.dpi_platform,
)
writer = MetricsWriter(database, bus)


async def _startup() -> None:
    await database.connect()
    await bus.connect()
    await prometheus.connect()
    agent: MetricsAgent = app.state.agent
    agent.start()


async def _shutdown() -> None:
    agent: MetricsAgent = app.state.agent
    await agent.stop()
    await prometheus.close()
    await bus.close()
    await database.close()
    shutdown_tracing()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Collects DPI telemetry from Prometheus on a schedule, normalises it into "
        "MetricSample rows in TimescaleDB, and publishes each sample to aiops.metrics for "
        "the anomaly detector. Also backfills history so the models can train immediately."
    ),
    readiness_checks={
        "timescaledb": database.ping,
        "redis": bus.ping,
        # Prometheus is a readiness dependency, not just a runtime one: an agent that
        # cannot query has nothing to collect, and reporting ready would make a silent
        # outage look like a healthy service producing no data.
        "prometheus": prometheus.is_healthy,
    },
    on_startup=[_startup],
    on_shutdown=[_shutdown],
)

# The agent metrics share the app's registry, so `/metrics` exposes both the standard
# aiops_http_* series and the collection series in one scrape.
agent_metrics = AgentMetrics(app.state.ctx.registry)
app.state.agent = MetricsAgent(
    collector, writer, agent_metrics, interval_seconds=INTERVAL_SECONDS
)
app.state.backfiller = Backfiller(collector, writer)
app.include_router(router)

configure_tracing(SERVICE_NAME, app=app)
# The agent's own Prometheus queries become spans, so a slow collection cycle can be
# attributed to the query rather than guessed at.
instrument_httpx()


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi_platform,
        "interval_seconds": INTERVAL_SECONDS,
        "endpoints": {
            "status": "/status",
            "queries": "/queries",
            "collect": "/collect",
            "backfill": "/backfill",
            "metrics": "/metrics",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "metrics_agent.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
