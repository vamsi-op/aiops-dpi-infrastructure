r"""ASGI entry point for the traffic predictor (module 5).

Run locally:
    uvicorn traffic_predictor.asgi:app --port 8022 --app-dir services/traffic_predictor

Or through the stack:
    .\scripts\stack.ps1 up -Layer all

Where module 4 reacts, this one anticipates. It forecasts each service's traffic over 1, 6 and 24
hour horizons from the 1-hour rollup, turns the predicted peak into a replica count, and publishes
`aiops.predictions` for the healing engine to act on before the spike rather than after it.
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

from .predictor import PredictorMetrics, TrafficPredictor
from .routes import router

SERVICE_NAME = str(AiopsService.TRAFFIC_PREDICTOR)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)
prometheus = PrometheusClient()


async def _startup() -> None:
    await database.connect()
    await bus.connect()
    await prometheus.connect()
    predictor: TrafficPredictor = app.state.predictor
    predictor.start()


async def _shutdown() -> None:
    predictor: TrafficPredictor = app.state.predictor
    await predictor.stop()
    await prometheus.close()
    await bus.close()
    await database.close()
    shutdown_tracing()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Forecasts DPI traffic on 1, 6 and 24 hour horizons from the 1-hour rollup, backtests "
        "against a seasonal-naive baseline, and turns the predicted peak into a replica "
        "recommendation published on aiops.predictions."
    ),
    readiness_checks={
        "timescaledb": database.ping,
        "redis": bus.ping,
        # Prometheus supplies the measured capacity a recommendation depends on. A forecast
        # without it still works but falls back to an assumed figure, so it belongs in readiness.
        "prometheus": prometheus.is_healthy,
    },
    on_startup=[_startup],
    on_shutdown=[_shutdown],
)

predictor_metrics = PredictorMetrics(app.state.ctx.registry)
app.state.predictor = TrafficPredictor(
    database,
    bus,
    prometheus,
    predictor_metrics,
    horizons=settings.ml.forecast_horizons_hours,
    history_days=settings.ml.forecast_history_days,
    interval_seconds=settings.ml.forecast_interval_seconds,
    min_headroom=settings.ml.scaling_min_headroom,
    platform=settings.dpi_platform,
)
app.include_router(router)

configure_tracing(SERVICE_NAME, app=app)
instrument_httpx()


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi_platform,
        "horizons_hours": settings.ml.forecast_horizons_hours,
        "history_days": settings.ml.forecast_history_days,
        "endpoints": {
            "status": "/status",
            "forecast": "/forecast/{service}",
            "accuracy": "/accuracy",
            "run": "/run",
            "metrics": "/metrics",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "traffic_predictor.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
