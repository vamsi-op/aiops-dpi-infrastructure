"""HTTP routes for the dashboard.

Everything is read-only except the chaos trigger, which proxies to the chaos agent once
module 9 exists. Route handlers stay thin: query through the repository, assemble a view
model, return it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from aiops_common.config import get_settings
from aiops_common.constants import MOJALOOP_DEPENDENCIES, MOJALOOP_SERVICES
from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

from . import health_rules
from .health_rules import DEFAULT_THRESHOLDS
from .models import (
    Anomaly,
    AutomationSummary,
    ChaosRun,
    Deployment,
    HealingAction,
    Incident,
    IncidentDetail,
    MetricPoint,
    Overview,
    OverviewCounts,
    PipelineStatus,
    PlatformTotals,
    Point,
    Prediction,
    ServiceDetail,
    ServiceHealth,
    TimelineBucket,
)
from .repository import STALE_AFTER_SECONDS, DashboardRepository

log = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _repo(request: Request) -> DashboardRepository:
    return request.app.state.repository  # type: ignore[no-any-return]


def _relay(request: Request) -> Any:
    return request.app.state.relay


def _is_stale(last_seen: datetime | None) -> bool:
    if last_seen is None:
        return True
    reference = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
    return (utc_now() - reference).total_seconds() > STALE_AFTER_SECONDS


def _build_service_health(
    snapshot: dict[str, Any],
    *,
    sparkline: list[dict[str, Any]],
    open_anomalies: int,
) -> ServiceHealth:
    stale = _is_stale(snapshot.get("last_seen"))
    status, reasons = health_rules.evaluate(snapshot, stale=stale)
    error_ratio = float(snapshot.get("error_ratio") or 0.0)
    return ServiceHealth(
        **snapshot,
        status=status,
        reasons=reasons,
        success_ratio=1.0 - error_ratio,
        open_anomalies=open_anomalies,
        sparkline=[Point(**point) for point in sparkline],
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
@router.get("/overview", response_model=Overview, summary="Everything the landing page needs")
async def overview(request: Request) -> Overview:
    repo = _repo(request)
    settings = get_settings()

    snapshots = await repo.service_snapshots()
    sparklines = await repo.sparklines(minutes=60)
    counts = await repo.overview_counts()
    recent_anomalies = await repo.anomalies(hours=1, limit=500)

    per_service_anomalies: dict[str, int] = {}
    for anomaly in recent_anomalies:
        per_service_anomalies[anomaly["service"]] = (
            per_service_anomalies.get(anomaly["service"], 0) + 1
        )

    services = [
        _build_service_health(
            snapshot,
            sparkline=sparklines.get(snapshot["service"], []),
            open_anomalies=per_service_anomalies.get(snapshot["service"], 0),
        )
        for snapshot in snapshots
    ]

    # Total TPS is a sum; error ratio and latency are weighted by traffic, because an
    # idle service at 100% errors should not drag the platform figure to 50%.
    total_tps = sum(service.tps for service in services)
    if total_tps > 0:
        weighted_error = (
            sum(service.error_ratio * service.tps for service in services) / total_tps
        )
    else:
        weighted_error = (
            sum(service.error_ratio for service in services) / len(services)
            if services
            else 0.0
        )

    statuses = [service.status for service in services]
    totals = PlatformTotals(
        tps=round(total_tps, 2),
        error_ratio=weighted_error,
        latency_p99_seconds=max(
            (service.latency_p99_seconds for service in services), default=0.0
        ),
        services_total=len(services),
        services_healthy=statuses.count("healthy"),
        services_degraded=statuses.count("degraded"),
        services_critical=statuses.count("critical"),
        services_stale=statuses.count("stale"),
    )

    relay = _relay(request)
    seen: set[str] = getattr(relay, "seen_types", set())
    notes: list[str] = []
    if not snapshots:
        notes.append(
            "No metrics in the database yet. Start the Mojaloop simulator and metrics "
            "agent (modules 2-3), or run scripts/seed_demo_data.py for sample data."
        )
    if not counts.get("anomalies_24h"):
        notes.append(
            "No anomalies recorded. The anomaly detector (module 4) is not running yet."
        )
    if settings.healing.enabled:
        notes.append("Automated remediation is ENABLED. Playbooks will execute for real.")
    else:
        notes.append(
            "Automated remediation is in dry-run mode. Actions are logged, not executed."
        )

    pipeline = PipelineStatus(
        metrics_flowing=bool(snapshots)
        and any(not _is_stale(s.get("last_seen")) for s in snapshots),
        anomaly_detection_active=bool(counts.get("anomalies_24h")) or "anomaly" in seen,
        forecasting_active=bool(await repo.latest_predictions()) or "prediction" in seen,
        rca_active=bool(counts.get("open_incidents")) or "incident" in seen,
        healing_enabled=settings.healing.enabled,
        chaos_available=bool(counts.get("chaos_active")) or "chaos" in seen,
        notes=notes,
    )

    return Overview(
        generated_at=utc_now(),
        platform=settings.dpi_platform,
        platform_status=health_rules.worst_of(statuses),
        totals=totals,
        counts=OverviewCounts(**counts),
        pipeline=pipeline,
        services=services,
    )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
@router.get("/services", response_model=list[ServiceHealth])
async def list_services(request: Request) -> list[ServiceHealth]:
    repo = _repo(request)
    snapshots = await repo.service_snapshots()
    sparklines = await repo.sparklines(minutes=60)
    return [
        _build_service_health(
            snapshot, sparkline=sparklines.get(snapshot["service"], []), open_anomalies=0
        )
        for snapshot in snapshots
    ]


@router.get("/services/{service}", response_model=ServiceDetail)
async def service_detail(
    request: Request,
    service: str,
    hours: Annotated[int, Query(ge=1, le=168)] = 6,
    bucket_seconds: Annotated[int, Query(ge=15, le=3600)] = 60,
) -> ServiceDetail:
    repo = _repo(request)
    snapshots = {row["service"]: row for row in await repo.service_snapshots()}
    snapshot = snapshots.get(service)

    metrics = await repo.service_timeseries(service, hours=hours, bucket_seconds=bucket_seconds)
    if snapshot is None and not metrics:
        raise HTTPException(status_code=404, detail=f"No data for service '{service}'")

    sparklines = await repo.sparklines(minutes=60)
    health = (
        _build_service_health(snapshot, sparkline=sparklines.get(service, []), open_anomalies=0)
        if snapshot
        else ServiceHealth(service=service, status="stale", reasons=["no recent samples"])
    )

    return ServiceDetail(
        service=service,
        health=health,
        metrics=[MetricPoint(**row) for row in metrics],
        anomalies=[
            Anomaly(**row)
            for row in await repo.anomalies(hours=hours, service=service, limit=100)
        ],
        predictions=[
            Prediction(**row) for row in await repo.predictions_for_service(service, limit=6)
        ],
        actions=[
            HealingAction(**row) for row in await repo.actions(hours=hours, service=service)
        ],
    )


@router.get("/topology", summary="Service dependency graph used by root cause analysis")
async def topology() -> dict[str, Any]:
    return {
        "services": list(MOJALOOP_SERVICES),
        "edges": [
            {"from": service, "to": dependency}
            for service, dependencies in MOJALOOP_DEPENDENCIES.items()
            for dependency in dependencies
        ],
    }


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
@router.get("/anomalies", response_model=list[Anomaly])
async def list_anomalies(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
    service: str | None = None,
    min_score: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Anomaly]:
    rows = await _repo(request).anomalies(
        hours=hours, service=service, min_score=min_score, limit=limit
    )
    return [Anomaly(**row) for row in rows]


@router.get("/anomalies/timeline", response_model=list[TimelineBucket])
async def anomaly_timeline(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 15,
) -> list[TimelineBucket]:
    rows = await _repo(request).anomaly_timeline(hours=hours, bucket_minutes=bucket_minutes)
    return [TimelineBucket(**row) for row in rows]


@router.get("/anomalies/{anomaly_id}", response_model=Anomaly)
async def get_anomaly(request: Request, anomaly_id: str) -> Anomaly:
    row = await _repo(request).anomaly(anomaly_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Anomaly '{anomaly_id}' not found")
    return Anomaly(**row)


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
@router.get("/incidents", response_model=list[Incident])
async def list_incidents(
    request: Request,
    status: str | None = None,
    hours: Annotated[int | None, Query(ge=1, le=720)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Incident]:
    rows = await _repo(request).incidents(status=status, hours=hours, limit=limit)
    return [_incident_model(row) for row in rows]


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def incident_detail(request: Request, incident_id: str) -> IncidentDetail:
    repo = _repo(request)
    row = await repo.incident(incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")

    incident = _incident_model(row)
    anomalies = [Anomaly(**item) for item in await repo.incident_anomalies(incident_id)]
    actions = [HealingAction(**item) for item in await repo.actions_for_incident(incident_id)]

    # Deployments around the incident are the evidence behind a `change` hypothesis, so
    # they are returned alongside rather than needing a second request.
    deployments = [
        Deployment(**item)
        for item in await repo.deployments(hours=24, limit=50)
        if item["service"] in incident.affected_services
    ]

    return IncidentDetail(
        incident=incident, anomalies=anomalies, actions=actions, deployments=deployments
    )


def _incident_model(row: dict[str, Any]) -> Incident:
    opened, closed = row.get("opened_at"), row.get("closed_at")
    duration = (closed - opened).total_seconds() if opened and closed else None
    return Incident(**row, duration_seconds=duration)


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------
@router.get("/predictions", response_model=list[Prediction])
async def list_predictions(
    request: Request,
    horizon_hours: Annotated[int | None, Query(ge=1, le=168)] = None,
) -> list[Prediction]:
    rows = await _repo(request).latest_predictions(horizon_hours=horizon_hours)
    return [Prediction(**row) for row in rows]


# ---------------------------------------------------------------------------
# Automation / healing
# ---------------------------------------------------------------------------
@router.get("/automation", response_model=AutomationSummary)
async def automation(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> AutomationSummary:
    repo = _repo(request)
    healing = get_settings().healing
    return AutomationSummary(
        healing_enabled=healing.enabled,
        dry_run_note=(
            "Playbooks execute for real."
            if healing.enabled
            else "Dry run: playbooks are selected and recorded but never executed. "
            "Set HEALING_ENABLED=true to arm them."
        ),
        max_actions_per_hour=healing.max_actions_per_hour,
        cooldown_seconds=healing.cooldown_seconds,
        min_confidence=healing.min_confidence,
        counts_by_status=await repo.action_summary(hours=hours),
        recent=[HealingAction(**row) for row in await repo.actions(hours=hours, limit=100)],
    )


@router.get("/actions", response_model=list[HealingAction])
async def list_actions(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
    service: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[HealingAction]:
    rows = await _repo(request).actions(
        hours=hours, service=service, status=status, limit=limit
    )
    return [HealingAction(**row) for row in rows]


# ---------------------------------------------------------------------------
# Chaos experiments
# ---------------------------------------------------------------------------
@router.get("/chaos", response_model=list[ChaosRun])
async def list_chaos_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ChaosRun]:
    rows = await _repo(request).chaos_runs(limit=limit)
    return [ChaosRun(**row) for row in rows]


@router.post("/chaos", status_code=501, summary="Start a chaos experiment")
async def start_chaos_run() -> dict[str, str]:
    # Declared now so the UI can be built against a stable contract, and so the failure
    # is an honest, specific message rather than a 404 that looks like a bug.
    raise HTTPException(
        status_code=501,
        detail=(
            "The chaos agent is not deployed yet (module 9). This endpoint will proxy "
            "experiment requests to it once it exists."
        ),
    )


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------
@router.get("/deployments", response_model=list[Deployment])
async def list_deployments(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
    service: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Deployment]:
    rows = await _repo(request).deployments(hours=hours, service=service, limit=limit)
    return [Deployment(**row) for row in rows]


# ---------------------------------------------------------------------------
# Config exposed to the UI
# ---------------------------------------------------------------------------
@router.get("/config", summary="Thresholds and settings the dashboard renders against")
async def config(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "platform": settings.dpi_platform,
        "env": settings.env,
        "scrape_interval_seconds": settings.observability.scrape_interval_seconds,
        "forecast_horizons_hours": settings.ml.forecast_horizons_hours,
        "anomaly_score_threshold": settings.ml.anomaly_score_threshold,
        "healing": {
            "enabled": settings.healing.enabled,
            "max_actions_per_hour": settings.healing.max_actions_per_hour,
            "cooldown_seconds": settings.healing.cooldown_seconds,
            "min_confidence": settings.healing.min_confidence,
        },
        "display_thresholds": {
            "error_ratio_warning": DEFAULT_THRESHOLDS.error_ratio_warning,
            "error_ratio_critical": DEFAULT_THRESHOLDS.error_ratio_critical,
            "latency_p99_warning_seconds": DEFAULT_THRESHOLDS.latency_p99_warning_seconds,
            "latency_p99_critical_seconds": DEFAULT_THRESHOLDS.latency_p99_critical_seconds,
            "cpu_warning": DEFAULT_THRESHOLDS.cpu_warning,
            "cpu_critical": DEFAULT_THRESHOLDS.cpu_critical,
            "memory_warning": DEFAULT_THRESHOLDS.memory_warning,
            "memory_critical": DEFAULT_THRESHOLDS.memory_critical,
        },
        "live_clients": request.app.state.connections.client_count,
    }
