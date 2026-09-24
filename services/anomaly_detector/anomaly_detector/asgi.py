r"""ASGI entry point for the anomaly detector (module 4).

Run locally:
    uvicorn anomaly_detector.asgi:app --port 8021 --app-dir services/anomaly_detector

Or through the stack:
    .\scripts\stack.ps1 up -Layer all

This is the first service that makes a judgement rather than moving data. It consumes
`aiops.metrics`, scores each sample against a per-service Isolation Forest, and publishes
`aiops.anomalies` for the RCA engine and the healing engine to act on.

Models are loaded from disk at start-up if they exist, so a restart does not require a retrain.
If the artifact directory is empty the service still starts and reports `models_loaded: 0`
rather than failing: an unhealthy detector is easier to diagnose than one that will not boot,
and `POST /train` is the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import AiopsService
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.service import build_app
from aiops_common.tracing import configure_tracing, shutdown_tracing

from . import model as model_module
from .detector import AnomalyDetector, DetectorMetrics
from .routes import router
from .scorer import Scorer
from .trainer import Trainer

SERVICE_NAME = str(AiopsService.ANOMALY_DETECTOR)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)

ARTIFACT_DIR = Path(settings.ml.artifact_dir)


async def _startup() -> None:
    await database.connect()
    await bus.connect()

    detector: AnomalyDetector = app.state.detector
    # Load from disk before consuming, so the first events off the stream are scored rather
    # than dropped as "no model".
    detector.set_models(model_module.load_all(ARTIFACT_DIR))
    if not detector.models:
        log.warning(
            "detector.no_models_on_disk",
            artifact_dir=str(ARTIFACT_DIR),
            detail="nothing will be scored until POST /train runs",
        )
    await detector.start()


async def _shutdown() -> None:
    detector: AnomalyDetector = app.state.detector
    await detector.stop()
    await bus.close()
    await database.close()
    shutdown_tracing()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Detects anomalies in DPI telemetry. Consumes aiops.metrics, scores each sample "
        "against a per-service Isolation Forest with hour-of-day-aware baselines, and "
        "publishes aiops.anomalies with feature attribution and a kind that maps to a "
        "remediation playbook."
    ),
    readiness_checks={"timescaledb": database.ping, "redis": bus.ping},
    on_startup=[_startup],
    on_shutdown=[_shutdown],
)

detector_metrics = DetectorMetrics(app.state.ctx.registry)
scorer = Scorer(threshold=settings.ml.anomaly_score_threshold)
detector = AnomalyDetector(database, bus, scorer, detector_metrics)

app.state.detector = detector
app.state.trainer = Trainer(
    database,
    artifact_dir=ARTIFACT_DIR,
    contamination=settings.ml.contamination,
    min_training_rows=settings.ml.min_training_rows,
)
app.include_router(router)

configure_tracing(SERVICE_NAME, app=app)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi_platform,
        "artifact_dir": str(ARTIFACT_DIR),
        "threshold": settings.ml.anomaly_score_threshold,
        "endpoints": {
            "status": "/status",
            "config": "/config",
            "train": "/train",
            "score": "/score",
            "metrics": "/metrics",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "anomaly_detector.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
