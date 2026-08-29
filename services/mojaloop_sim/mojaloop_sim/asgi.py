"""ASGI entry point for one simulated Mojaloop service.

Which service this process becomes is chosen by the `SIM_SERVICE` environment variable, so
one image serves all five. Docker Compose sets it per container.

    SIM_SERVICE=central-ledger uvicorn mojaloop_sim.asgi:app --port 8000

Lives inside the package rather than as a top-level `app.py` so that every service's entry
point has a unique module path. Two services both exposing a top-level `app` module is
ambiguous to any tool that analyses the repo as a whole - mypy rejects it outright.

The DPI metrics and the platform's own `aiops_*` metrics share one registry, and therefore
one `/metrics` endpoint. Prometheus scrapes once and gets both the simulated Mojaloop
signals and the simulator's own HTTP stats.
"""

from __future__ import annotations

import os

from aiops_common.config import get_settings
from aiops_common.constants import MojaloopService
from aiops_common.log import configure_logging, get_logger
from aiops_common.service import build_app

from .routes import router
from .simulator import Simulator

SERVICE = os.getenv("SIM_SERVICE", MojaloopService.CENTRAL_LEDGER.value)

# Named after the simulated service, not "mojaloop-sim", so every log line and self-metric
# is attributable to the right container without extra labelling.
configure_logging(SERVICE)
log = get_logger(SERVICE)

settings = get_settings()
simulator = Simulator(
    SERVICE,
    base_tps=settings.simulator.base_tps,
    seasonality=settings.simulator.seasonality,
)


def _on_startup() -> None:
    simulator.start()
    # Produce one sample immediately so a scrape arriving in the first second sees real
    # values instead of zeros, which would otherwise look like a dead service.
    simulator.tick()


async def _on_shutdown() -> None:
    await simulator.stop()


app = build_app(
    service_name=SERVICE,
    description=(
        f"Simulated Mojaloop service '{SERVICE}'. Exposes DPI metrics for Prometheus and "
        "accepts fault injection and scaling commands from the AIOps platform."
    ),
    on_startup=[_on_startup],
    on_shutdown=[_on_shutdown],
    # Share the simulator's registry so /metrics carries the dpi_* series.
    registry=simulator.metrics.registry,
)
app.state.simulator = simulator
app.include_router(router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, object]:
    return {
        "service": SERVICE,
        "role": simulator.profile.description,
        "endpoints": {
            "metrics": "/metrics",
            "health": "/health",
            "state": "/sim",
            "faults": "/sim/faults",
            "scale": "/sim/scale",
            "docs": "/docs",
        },
    }
