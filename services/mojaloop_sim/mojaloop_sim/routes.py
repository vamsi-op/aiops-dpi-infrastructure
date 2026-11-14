"""HTTP surface of the simulator.

Three groups:

* **Introspection** (`GET /sim`) - what this service currently looks like.
* **Fault injection** (`POST /sim/faults`, ...) - the hooks module 9's chaos agent drives.
* **Control** (`POST /sim/scale`) - the hook module 6's scaling playbooks drive.

Keeping these under `/sim` leaves the root namespace free for the fake business endpoints,
and makes it obvious in an access log which calls were the platform poking the simulator
versus ordinary traffic.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from aiops_common.log import get_logger

from .dependencies import DEFAULT_OPEN_SECONDS
from .faults import DEFAULT_DURATION_SECONDS, MAX_DURATION_SECONDS, FaultKind
from .simulator import Simulator

log = get_logger(__name__)

router = APIRouter()


def _sim(request: Request) -> Simulator:
    simulator: Simulator | None = getattr(request.app.state, "simulator", None)
    if simulator is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="Simulator not started")
    return simulator


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class InjectFaultRequest(BaseModel):
    kind: FaultKind = Field(description="Fault to inject; names match ChaosExperiment")
    magnitude: float | None = Field(
        default=None,
        description="Strength. Units depend on kind; omit for a sensible default.",
    )
    duration_seconds: int = Field(
        default=DEFAULT_DURATION_SECONDS,
        ge=1,
        le=MAX_DURATION_SECONDS,
        description=f"Auto-expires after this long. Hard cap {MAX_DURATION_SECONDS}s.",
    )
    injected_by: str = Field(default="manual", description="Who or what asked for this")
    notes: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Kind-specific extras, e.g. {'after_seconds': 2} for kill_service",
    )


class ScaleRequest(BaseModel):
    replicas: int = Field(ge=1, le=50)
    reason: str = Field(default="manual", description="Recorded in the log line")


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
@router.get("/sim", tags=["simulator"], summary="Current simulated state")
async def describe(request: Request) -> dict[str, Any]:
    return _sim(request).snapshot()


@router.get("/sim/faults", tags=["simulator"], summary="Active faults")
async def list_faults(request: Request) -> dict[str, Any]:
    simulator = _sim(request)
    return {
        "service": simulator.service,
        "active": [fault.to_dict() for fault in simulator.faults.active()],
        "injectable": [str(kind) for kind in FaultKind],
    }


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------
@router.post("/sim/faults", tags=["simulator"], summary="Inject a fault")
async def inject_fault(
    request: Request, body: Annotated[InjectFaultRequest, Body()]
) -> dict[str, Any]:
    """Inject a fault that automatically expires.

    `kill_service` is special: it has no ongoing effect, it terminates the process so the
    container restart policy brings it back. That is what makes module 6's restart playbook
    and module 9's kill experiment observable rather than simulated.
    """
    simulator = _sim(request)

    if body.kind is FaultKind.KILL_SERVICE:
        delay = max(0.0, float(body.parameters.get("after_seconds", 0.5)))
        return await _schedule_exit(simulator, delay=delay, injected_by=body.injected_by)

    fault = simulator.faults.inject(
        body.kind,
        magnitude=body.magnitude,
        duration_seconds=body.duration_seconds,
        injected_by=body.injected_by,
        notes=body.notes,
    )
    # Apply immediately so a caller that scrapes straight away sees the effect, rather
    # than having to wait up to a full tick.
    simulator.tick()
    return {"injected": fault.to_dict(), "service": simulator.service}


@router.delete("/sim/faults", tags=["simulator"], summary="Clear all faults")
async def clear_faults(request: Request) -> dict[str, Any]:
    simulator = _sim(request)
    removed = simulator.faults.clear()
    simulator.tick()
    return {"cleared": removed, "service": simulator.service}


@router.delete("/sim/faults/{fault_id}", tags=["simulator"], summary="Clear one fault")
async def clear_fault(request: Request, fault_id: str) -> dict[str, Any]:
    simulator = _sim(request)
    if simulator.faults.clear(fault_id) == 0:
        raise HTTPException(status_code=404, detail=f"No active fault {fault_id}")
    simulator.tick()
    return {"cleared": fault_id, "service": simulator.service}


@router.post("/sim/kill", tags=["simulator"], summary="Terminate the process")
async def kill(
    request: Request, after_seconds: float = 0.5, injected_by: str = "manual"
) -> dict[str, Any]:
    """Exit so the container restart policy restarts us.

    Deliberately not a graceful shutdown of the simulation only: the point is for Docker
    (and later Kubernetes) to observe a real restart, which is what the healing playbooks
    and the chaos scorecard measure.
    """
    return await _schedule_exit(_sim(request), delay=after_seconds, injected_by=injected_by)


async def _schedule_exit(
    simulator: Simulator, *, delay: float, injected_by: str
) -> dict[str, Any]:
    log.error(
        "sim.kill_requested",
        monitored_service=simulator.service,
        after_seconds=delay,
        injected_by=injected_by,
    )

    async def terminate() -> None:
        await asyncio.sleep(delay)
        log.error("sim.terminating", monitored_service=simulator.service)
        # SIGTERM rather than os._exit: uvicorn runs its shutdown handlers, the container
        # exits cleanly, and the restart policy takes over. A hard exit would also work but
        # would look like a crash loop in the container logs.
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(terminate())  # noqa: RUF006 - fire and forget by design
    return {
        "killing": simulator.service,
        "after_seconds": delay,
        "note": "container restart policy will bring the service back",
    }


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
@router.post("/sim/scale", tags=["simulator"], summary="Change the replica count")
async def scale(request: Request, body: Annotated[ScaleRequest, Body()]) -> dict[str, Any]:
    """Scale the service, as modules 5 and 6 will.

    Capacity is `tps_per_replica * replicas`, and every derived signal keys off
    `tps / capacity`, so this genuinely relieves or increases pressure. Without that,
    predictive scaling would be a number on a dashboard with no observable consequence.
    """
    simulator = _sim(request)
    before = simulator.model.replicas
    after = simulator.scale(body.replicas)
    state = simulator.tick()
    return {
        "service": simulator.service,
        "from_replicas": before,
        "to_replicas": after,
        "capacity_tps": round(simulator.model.capacity_tps, 2),
        "load_factor": round(state.load_factor, 3),
        "reason": body.reason,
    }


# ---------------------------------------------------------------------------
# Circuit breakers on downstream dependencies
# ---------------------------------------------------------------------------
class CircuitRequest(BaseModel):
    action: Literal["open", "close"]
    duration_seconds: int = Field(default=DEFAULT_OPEN_SECONDS, ge=5, le=3600)
    opened_by: str = Field(default="api", max_length=100)


@router.get("/sim/circuits", tags=["simulator"], summary="Circuit breakers on dependencies")
async def circuits(request: Request) -> dict[str, Any]:
    simulator = _sim(request)
    return {"service": simulator.service, **simulator.dependencies.snapshot()}


@router.post(
    "/sim/circuits/{dependency}",
    tags=["simulator"],
    summary="Open or close the circuit breaker on one dependency",
)
async def set_circuit(
    request: Request, dependency: str, body: Annotated[CircuitRequest, Body()]
) -> dict[str, Any]:
    """Trip (or reset) this service's breaker on `dependency`.

    Opened by the healing engine's `circuit_break_cascade` playbook on every caller of the
    root-cause service: the callers stop waiting on it and fail fast, so the cascade stops
    spreading. An open breaker half-opens after `duration_seconds` and closes itself once
    a probe finds the dependency healthy.
    """
    simulator = _sim(request)
    breaker = simulator.dependencies.breakers.get(dependency)
    if breaker is None:
        raise HTTPException(
            status_code=404,
            detail=f"{simulator.service} does not call {dependency!r}; "
            f"dependencies: {list(simulator.dependencies.breakers)}",
        )
    if body.action == "open":
        breaker.open(seconds=body.duration_seconds, by=body.opened_by)
    else:
        breaker.close()
    log.info(
        "sim.circuit_changed",
        monitored_service=simulator.service,
        dependency=dependency,
        state=str(breaker.state),
        by=body.opened_by,
    )
    return {"service": simulator.service, **breaker.to_dict()}


@router.get(
    "/dependency/probe",
    tags=["simulator"],
    summary="Health probe callers use for their dependency on this service",
)
async def dependency_probe(request: Request) -> JSONResponse:
    """What a caller's DependencyMonitor polls. Deliberately *traced* (unlike /sim*): each
    probe is a real service-to-service call, so Tempo's service graph has genuine
    caller -> dependency edges, and the RCA engine can read failed calls on them.

    The call fails (503) with this service's current error ratio, as real calls would. The
    state is in the body either way, so a failed probe still tells the caller how degraded
    the dependency is rather than reading as "unreachable".
    """
    simulator = _sim(request)
    snapshot = simulator.snapshot()
    state = simulator.last_state
    failed = state is not None and simulator.rng.random() < state.error_ratio
    return JSONResponse(status_code=503 if failed else 200, content=snapshot)


# ---------------------------------------------------------------------------
# Fake business endpoint
# ---------------------------------------------------------------------------
@router.post("/simulate/request", tags=["workload"], summary="Serve one simulated request")
async def simulated_request(request: Request, endpoint: str = "/transfers") -> dict[str, Any]:
    """Handle one request with the service's current latency characteristics.

    Traced. FastAPI's instrumentation produces the server span; the child spans below give
    the trace internal structure, so a slow request shows *where* it was slow rather than
    only that it was. Attributes carry the simulated state at the moment of the request,
    which makes a single trace self-contained evidence for module 7: load factor and active
    faults are right there on the span instead of having to be joined from Prometheus by
    timestamp.

    It deliberately does **not** touch the DPI counters. The background tick is the single
    writer for those; letting requests increment them too would double-count and make
    `rate()` disagree with the simulated traffic level.
    """
    simulator = _sim(request)
    state = simulator.last_state or simulator.tick()
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("mojaloop.handle_request") as span:
        span.set_attribute("dpi.service", simulator.service)
        span.set_attribute("dpi.endpoint", endpoint)
        span.set_attribute("dpi.load_factor", round(state.load_factor, 3))
        span.set_attribute("dpi.replicas", state.replicas)
        span.set_attribute("dpi.queue_depth", round(state.queue_depth, 1))
        active = [str(fault.kind) for fault in simulator.faults.active()]
        if active:
            span.set_attribute("dpi.active_faults", active)

        # Two phases so the span tree has shape. The split is proportional rather than
        # measured - the simulator has no real work to divide - but it keeps the sum equal
        # to the latency the metrics report, so traces and metrics do not contradict.
        total = min(state.latency_p50_seconds, 2.0)
        with tracer.start_as_current_span("mojaloop.validate_participants"):
            await asyncio.sleep(total * 0.3)

        with tracer.start_as_current_span("mojaloop.commit_transfer") as commit:
            await asyncio.sleep(total * 0.7)

            if simulator.rng.random() < state.error_ratio:
                # Mark the span before raising. An exception that escapes an unmarked span
                # still shows as an error via the HTTP status, but the trace then blames
                # the request as a whole rather than the phase that actually failed.
                commit.set_status(Status(StatusCode.ERROR, "simulated downstream failure"))
                span.set_status(Status(StatusCode.ERROR, "simulated downstream failure"))
                raise HTTPException(status_code=503, detail="simulated downstream failure")

        return {
            "service": simulator.service,
            "endpoint": endpoint,
            "latency_seconds": round(state.latency_p50_seconds, 4),
            "status": "committed",
            "trace_id": format(span.get_span_context().trace_id, "032x"),
        }
