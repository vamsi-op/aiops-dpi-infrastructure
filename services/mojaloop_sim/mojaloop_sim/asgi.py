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
from aiops_common.tracing import configure_tracing, instrument_httpx, shutdown_tracing

from .faults import MAX_DURATION_SECONDS, FaultKind
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


# A defect that ships *inside a version*. On Kubernetes the chaos `bad_deploy` experiment
# rolls out a new Deployment revision with SIM_DEFECT set, so the fault lives in the pod
# template rather than in one process's memory - and only a real `rollout undo` removes it.
SIM_VERSION = os.getenv("SIM_VERSION", "v1.0.4")
SIM_DEFECT = os.getenv("SIM_DEFECT", "").strip()


def _apply_shipped_defect() -> None:
    if not SIM_DEFECT:
        return
    try:
        kind = FaultKind(SIM_DEFECT)
    except ValueError:
        log.warning("sim.unknown_defect", defect=SIM_DEFECT, version=SIM_VERSION)
        return
    simulator.faults.inject(
        kind,
        duration_seconds=MAX_DURATION_SECONDS,
        injected_by=f"deploy:{SIM_VERSION}",
        notes=f"Defect shipped in {SIM_VERSION}",
    )
    log.info("sim.shipped_defect_active", defect=SIM_DEFECT, version=SIM_VERSION)


def _on_startup() -> None:
    _apply_shipped_defect()
    simulator.start()
    # Produce one sample immediately so a scrape arriving in the first second sees real
    # values instead of zeros, which would otherwise look like a dead service.
    simulator.tick()


async def _on_shutdown() -> None:
    await simulator.stop()
    # Flush queued spans. The kill_service fault exits via SIGTERM specifically so uvicorn
    # runs shutdown handlers, and this is the handler that makes the spans leading up to
    # the kill survive it.
    shutdown_tracing()


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

# The /sim* control surface is excluded for the same reason /health and /metrics are: those
# calls are the platform poking the simulator, not workload. Tracing them made Tempo's
# service graph show a `user -> service` edge for all five services purely because
# check_observability.py had read their state, and inflated
# traces_spanmetrics_calls_total with the platform's own introspection.
#
# The patterns are anchored deliberately. A bare `sim` would also match /simulate/request -
# the one endpoint that must be traced - because exclusions are substring matches.
SIM_CONTROL_URLS = r"/sim$,/sim/"

# After include_router: FastAPIInstrumentor wraps the ASGI app, so routes added later are
# still traced, but instrumenting before the app is fully assembled means the span name
# falls back to the raw path instead of the route template - and then every distinct URL
# becomes its own operation in Tempo's span metrics.
configure_tracing(SERVICE, app=app, excluded_urls=SIM_CONTROL_URLS)
# The simulator does not call other services yet. Module 9's cascade experiment will make
# it fan out, and at that point the trace has to cross the boundary rather than stopping
# at this process.
instrument_httpx()


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
