r"""ASGI entry point for the healing engine (module 6).

Run locally:
    uvicorn healing_engine.asgi:app --port 8024 --app-dir services/healing_engine

Or through the stack:
    .\scripts\stack.ps1 up -Layer all

This service consumes anomaly and incident events, selects appropriate remediation
playbooks, checks safety rails (master switch, cooldown, budget, confidence), captures
before/after metric snapshots, and executes healing actions via Docker API.

Safety rails ensure actions are:
- Disabled by default (HEALING_ENABLED=false for dry-run)
- Rate-limited (max 10 actions/hour)
- Throttled per-service (300s cooldown)
- Confidence-gated (min 0.7 confidence from RCA)

All actions are persisted to healing_actions table, including blocked ones.
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

from .engine import HealingEngine
from .executor import DockerExecutor
from .playbooks import PlaybookExecutor
from .routes import router
from .safety import SafetyRails
from .snapshot import MetricSnapshot

SERVICE_NAME = str(AiopsService.HEALING_ENGINE)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)


async def _startup() -> None:
    await database.connect()
    await bus.connect()

    engine: HealingEngine = app.state.engine
    await engine.start()


async def _shutdown() -> None:
    engine: HealingEngine = app.state.engine
    await engine.stop()
    await bus.close()
    await database.close()
    shutdown_tracing()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Self-healing playbook engine. Consumes anomalies and incidents, selects remediation "
        "playbooks based on anomaly kind and root cause hypotheses, enforces four safety rails "
        "(master switch, cooldown, budget, confidence), captures before/after metric snapshots, "
        "and executes actions via Docker API. All actions persisted with full audit trail."
    ),
    readiness_checks={"timescaledb": database.ping, "redis": bus.ping},
    on_startup=[_startup],
    on_shutdown=[_shutdown],
)

# Initialize healing components
executor = DockerExecutor(socket_path=settings.healing.docker_socket)
safety = SafetyRails(bus, settings.healing)
snapshot = MetricSnapshot(database)
playbook_executor = PlaybookExecutor(executor)

engine = HealingEngine(
    database,
    bus,
    executor,
    safety,
    snapshot,
    playbook_executor,
)

app.state.engine = engine
app.state.safety = safety
app.state.executor = executor
app.include_router(router)

configure_tracing(SERVICE_NAME, app=app)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi.platform,
        "enabled": settings.healing.enabled,
        "playbooks": [
            "restart_on_oom",
            "circuit_break_cascade",
            "rollback_bad_deploy",
            "scale_out_preemptive",
            "scale_in_idle",
            "clear_queue_backlog",
        ],
        "safety_rails": [
            "master_switch",
            "min_confidence",
            "per_target_cooldown",
            "hourly_budget",
        ],
        "endpoints": {
            "status": "/status",
            "config": "/config",
            "actions": "/actions",
            "metrics": "/metrics",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "healing_engine.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
