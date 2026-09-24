"""HTTP surface of the metrics agent (module 3).

Small on purpose. The agent's real work happens on a timer, not in response to requests;
these endpoints exist to make that work inspectable and to let the verification script and a
demo operator drive it deliberately.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from aiops_common.log import get_logger

from .agent import MetricsAgent
from .backfill import Backfiller
from .collector import DISCOVERY_QUERY, ERROR_RATIO_QUERY, GAUGE_QUERIES, REPLICAS_QUERY

log = get_logger(__name__)

router = APIRouter()


def _agent(request: Request) -> MetricsAgent:
    agent: MetricsAgent | None = getattr(request.app.state, "agent", None)
    if agent is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="Agent not started")
    return agent


def _backfiller(request: Request) -> Backfiller:
    backfiller: Backfiller | None = getattr(request.app.state, "backfiller", None)
    if backfiller is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Backfiller not available")
    return backfiller


class BackfillRequest(BaseModel):
    hours: int = Field(default=24, ge=1, le=360, description="How far back to replay")
    step_seconds: int = Field(
        default=60,
        ge=15,
        le=3600,
        description="Resolution. 60s matches the service_metrics_1m rollup.",
    )
    publish: bool = Field(
        default=False,
        description=(
            "Also emit to aiops.metrics. Off by default: replaying days of history would "
            "evict live events the anomaly detector has not consumed yet."
        ),
    )


@router.get("/status", tags=["agent"], summary="Last collection cycle")
async def status(request: Request) -> dict[str, Any]:
    return _agent(request).status.as_dict()


@router.get("/queries", tags=["agent"], summary="The recording rules this agent reads")
async def queries() -> dict[str, Any]:
    """Expose the query map.

    Makes the agent's contract with Prometheus discoverable rather than buried in source,
    and lets scripts/check_metrics_agent.py assert every rule the agent depends on actually
    returns data - without duplicating the list and letting the two drift.
    """
    return {
        "discovery": DISCOVERY_QUERY,
        "gauges": GAUGE_QUERIES,
        "error_ratio": ERROR_RATIO_QUERY,
        "replicas": REPLICAS_QUERY,
    }


@router.post("/collect", tags=["agent"], summary="Run one collection cycle now")
async def collect(request: Request) -> dict[str, Any]:
    """Trigger a cycle immediately instead of waiting for the next tick.

    Returns the same shape as `/status`. Used by the verification script so it can prove a
    row count increased as a direct result of a cycle, rather than waiting and hoping the
    timer fired.
    """
    agent = _agent(request)
    try:
        await agent.run_once()
    except Exception as exc:
        # Re-raised as a 502 rather than swallowed: the caller asked for a cycle and needs
        # to know it did not happen, and the agent's own status already records the stage.
        raise HTTPException(status_code=502, detail=f"Collection failed: {exc}") from exc
    return agent.status.as_dict()


@router.post("/backfill", tags=["agent"], summary="Replay Prometheus history")
async def backfill(
    request: Request, body: Annotated[BackfillRequest, Body()]
) -> dict[str, Any]:
    """Populate history so modules 4 and 5 have something to train on immediately.

    Runs inline rather than as a background task: the caller asked for a specific range and
    needs to know whether it landed. A 24-hour backfill at 60s resolution is around 1400
    Prometheus instants, which takes seconds, not minutes.
    """
    result = await _backfiller(request).run(
        hours=body.hours, step_seconds=body.step_seconds, publish=body.publish
    )
    return {
        "requested_points": result.requested_points,
        "collected_points": result.collected_points,
        "empty_points": result.empty_points,
        "rows_written": result.rows_written,
        "coverage": round(result.coverage, 4),
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
    }
