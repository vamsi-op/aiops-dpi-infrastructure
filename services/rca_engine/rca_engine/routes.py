"""HTTP surface of the RCA engine (module 7)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent

from .correlator import Correlator
from .engine import RCAEngine
from .incident_manager import IncidentManager

log = get_logger(__name__)

router = APIRouter()


def _engine(request: Request) -> RCAEngine:
    engine: RCAEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="RCA engine not started")
    return engine


def _correlator(request: Request) -> Correlator:
    correlator: Correlator | None = getattr(request.app.state, "correlator", None)
    if correlator is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Correlator not available")
    return correlator


def _incident_manager(request: Request) -> IncidentManager:
    manager: IncidentManager | None = getattr(request.app.state, "incident_manager", None)
    if manager is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Incident manager not available")
    return manager


class CorrelateRequest(BaseModel):
    """Request to manually correlate a set of anomalies."""

    anomaly_ids: list[str] = Field(
        description="List of anomaly IDs to correlate",
        min_length=1,
    )


class ResolveRequest(BaseModel):
    """Request to manually resolve an incident."""

    incident_id: str = Field(description="Incident ID to resolve")


@router.get("/status", tags=["rca"], summary="RCA engine status and metrics")
async def status(request: Request) -> dict[str, Any]:
    """Return current engine status including window size and unassigned anomalies."""
    engine = _engine(request)
    return await engine.get_status()


@router.get("/config", tags=["rca"], summary="RCA engine configuration")
async def config(request: Request) -> dict[str, Any]:
    """Expose the correlation configuration.

    Discoverable rather than buried in source, so verification scripts can assert against
    the values actually in force.
    """
    engine = _engine(request)
    correlator = _correlator(request)

    return {
        "correlation_methods": ["temporal", "topological", "change", "event"],
        "window_seconds": engine.window_seconds,
        "auto_resolve_seconds": engine.auto_resolve_seconds,
        "temporal_window_seconds": correlator.temporal_window_seconds,
        "change_window_seconds": correlator.change_window_seconds,
        "event_window_seconds": correlator.event_window_seconds,
    }


@router.get("/incidents", tags=["incidents"], summary="List recent incidents")
async def list_incidents(
    request: Request,
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    """List recent incidents with optional status filter."""
    manager = _incident_manager(request)

    query = """
        SELECT incident_id, opened_at, closed_at, status, severity, platform,
               title, affected_services, anomaly_ids, hypotheses_json, ai_summary
        FROM incidents
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY opened_at DESC
        LIMIT $2
    """

    rows = await manager.db.fetch(query, status, limit)

    incidents = []
    for row in rows:
        incidents.append({
            "incident_id": row["incident_id"],
            "opened_at": row["opened_at"].isoformat(),
            "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
            "status": row["status"],
            "severity": row["severity"],
            "platform": row["platform"],
            "title": row["title"],
            "affected_services": row["affected_services"],
            "anomaly_count": len(row["anomaly_ids"]),
            "hypothesis_count": len(row["hypotheses_json"]),
            "ai_summary": row["ai_summary"],
        })

    return {
        "incidents": incidents,
        "count": len(incidents),
        "filter": {"status": status, "limit": limit},
    }


@router.get("/incidents/{incident_id}", tags=["incidents"], summary="Get incident details")
async def get_incident(request: Request, incident_id: str) -> dict[str, Any]:
    """Get full details of a specific incident including all hypotheses."""
    manager = _incident_manager(request)

    query = """
        SELECT incident_id, opened_at, closed_at, status, severity, platform,
               title, affected_services, anomaly_ids, hypotheses_json, ai_summary
        FROM incidents
        WHERE incident_id = $1
    """

    row = await manager.db.fetchrow(query, incident_id)

    if not row:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    return {
        "incident_id": row["incident_id"],
        "opened_at": row["opened_at"].isoformat(),
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "status": row["status"],
        "severity": row["severity"],
        "platform": row["platform"],
        "title": row["title"],
        "affected_services": row["affected_services"],
        "anomaly_ids": row["anomaly_ids"],
        "hypotheses": row["hypotheses_json"],
        "ai_summary": row["ai_summary"],
    }


@router.post("/correlate", tags=["rca"], summary="Manually correlate anomalies")
async def correlate(
    request: Request,
    body: Annotated[CorrelateRequest, Body()],
) -> dict[str, Any]:
    """Manually trigger correlation for a set of anomalies.

    Useful for testing and investigation. Loads the specified anomalies from the database,
    runs all correlation methods, and returns the ranked hypotheses without creating an incident.
    """
    correlator = _correlator(request)

    # Load anomalies from database
    query = """
        SELECT anomaly_id, detected_at, service, metric_name, severity,
               anomaly_score, contributing_features, predicted_kind, incident_id
        FROM anomalies
        WHERE anomaly_id = ANY($1)
    """

    rows = await correlator.db.fetch(query, body.anomaly_ids)

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No anomalies found for IDs: {body.anomaly_ids}",
        )

    anomalies = []
    for row in rows:
        anomaly = AnomalyEvent(
            anomaly_id=row["anomaly_id"],
            detected_at=row["detected_at"],
            service=row["service"],
            metric_name=row["metric_name"],
            severity=row["severity"],
            anomaly_score=row["anomaly_score"],
            contributing_features=row["contributing_features"] or [],
            predicted_kind=row["predicted_kind"],
            incident_id=row["incident_id"],
        )
        anomalies.append(anomaly)

    # Run correlation
    hypotheses = await correlator.correlate(anomalies)

    return {
        "anomaly_count": len(anomalies),
        "hypothesis_count": len(hypotheses),
        "hypotheses": [h.model_dump() for h in hypotheses],
    }


@router.post("/incidents/{incident_id}/resolve", tags=["incidents"], summary="Resolve an incident")
async def resolve_incident(
    request: Request,
    incident_id: str,
) -> dict[str, Any]:
    """Manually resolve an incident."""
    manager = _incident_manager(request)

    await manager.resolve_incident(incident_id)

    return {
        "incident_id": incident_id,
        "status": "resolved",
        "message": "Incident resolved successfully",
    }
