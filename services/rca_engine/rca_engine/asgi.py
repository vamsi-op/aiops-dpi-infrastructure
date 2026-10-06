r"""ASGI entry point for the RCA engine (module 7).

Run locally:
    uvicorn rca_engine.asgi:app --port 8023 --app-dir services/rca_engine

Or through the stack:
    .\scripts\stack.ps1 up -Layer all

This service consumes anomaly events, correlates them across 4 dimensions
(temporal, topological, change, event), and produces incidents with ranked
root cause hypotheses. It manages the incident lifecycle and publishes
incidents to the aiops.incidents stream for the healing engine and dashboard.

The RCA engine uses:
- Temporal correlation: cluster anomalies in sliding time windows
- Topological correlation: detect cascades using dependency graph
- Change correlation: link anomalies to recent deployments
- Event correlation: correlate with traffic surges and chaos experiments
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
from aiops_common.service import build_app
from aiops_common.tracing import configure_tracing, shutdown_tracing

from .correlator import RCACorrelator
from .engine import RCAEngine
from .incident_manager import IncidentManager
from .routes import router

SERVICE_NAME = str(AiopsService.RCA_ENGINE)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)


async def _startup() -> None:
    await database.connect()
    await bus.connect()

    engine: RCAEngine = app.state.engine
    await engine.start()


async def _shutdown() -> None:
    engine: RCAEngine = app.state.engine
    await engine.stop()
    await bus.close()
    await database.close()
    shutdown_tracing()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Root Cause Analysis engine. Correlates anomalies across temporal, topological, "
        "change, and event dimensions. Produces ranked root cause hypotheses with evidence. "
        "Manages incident lifecycle from detection to resolution. Publishes incidents with "
        "AI-generated summaries to aiops.incidents stream."
    ),
    readiness_checks={"timescaledb": database.ping, "redis": bus.ping},
    on_startup=[_startup],
    on_shutdown=[_shutdown],
)

# Initialize RCA components
correlator = RCACorrelator(database)
incident_manager = IncidentManager(database, bus)
engine = RCAEngine(
    database,
    bus,
    correlator,
    incident_manager,
    window_seconds=300,  # 5 minute correlation window
    auto_resolve_seconds=300,  # Auto-resolve after 5 minutes of no new anomalies
)

app.state.engine = engine
app.state.correlator = correlator
app.state.incident_manager = incident_manager
app.include_router(router)

configure_tracing(SERVICE_NAME, app=app)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi.platform,
        "correlation_methods": ["temporal", "topological", "change", "event"],
        "endpoints": {
            "status": "/status",
            "config": "/config",
            "incidents": "/incidents",
            "correlate": "/correlate",
            "metrics": "/metrics",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "rca_engine.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
