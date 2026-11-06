"""HTTP surface of the healing engine (module 6)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from aiops_common.log import get_logger

from .engine import HealingEngine
from .executor import ExecutorInterface
from .safety import SafetyRails

log = get_logger(__name__)

router = APIRouter()


def _engine(request: Request) -> HealingEngine:
    engine: HealingEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="Healing engine not started")
    return engine


def _safety(request: Request) -> SafetyRails:
    safety: SafetyRails | None = getattr(request.app.state, "safety", None)
    if safety is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Safety rails not available")
    return safety


def _executor(request: Request) -> ExecutorInterface:
    executor: ExecutorInterface | None = getattr(request.app.state, "executor", None)
    if executor is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Executor not available")
    return executor


@router.get("/status", tags=["healing"], summary="Healing engine status and metrics")
async def status(request: Request) -> dict[str, Any]:
    """Return current engine status including enabled state and handled anomalies."""
    engine = _engine(request)
    return await engine.get_status()


@router.get("/config", tags=["healing"], summary="Healing engine configuration")
async def config(request: Request) -> dict[str, Any]:
    """Expose the healing configuration.

    Discoverable rather than buried in source, so verification scripts can assert against
    the values actually in force.
    """
    from aiops_common.config import get_settings

    settings = get_settings()

    return {
        "enabled": settings.healing.enabled,
        "max_actions_per_hour": settings.healing.max_actions_per_hour,
        "cooldown_seconds": settings.healing.cooldown_seconds,
        "min_confidence": settings.healing.min_confidence,
        "docker_socket": settings.healing.docker_socket,
        "min_replicas": settings.healing.min_replicas,
        "max_replicas": settings.healing.max_replicas,
        "replica_memory_limit_mb": settings.healing.replica_memory_limit_mb,
        "replica_cpu_limit": settings.healing.replica_cpu_limit,
        "min_available_ram_mb": settings.healing.min_available_ram_mb,
        "max_host_cpu_percent": settings.healing.max_host_cpu_percent,
        "playbooks": [
            "restart_on_oom",
            "circuit_break_cascade",
            "rollback_bad_deploy",
            "scale_out_preemptive",
            "scale_in_idle",
            "clear_queue_backlog",
        ],
        "safety_rails": {
            "master_switch": "HEALING_ENABLED",
            "min_confidence": f">= {settings.healing.min_confidence}",
            "cooldown": f"{settings.healing.cooldown_seconds}s per service",
            "budget": f"{settings.healing.max_actions_per_hour} per hour",
            "capacity": f"RAM > {settings.healing.replica_memory_limit_mb + settings.healing.min_available_ram_mb}MB, CPU < {settings.healing.max_host_cpu_percent}%",
            "replica_bounds": f"{settings.healing.min_replicas} <= replicas <= {settings.healing.max_replicas}",
        },
    }


@router.get("/actions", tags=["healing"], summary="List recent healing actions")
async def list_actions(
    request: Request,
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    """List recent healing actions with optional status filter."""
    engine = _engine(request)

    query = """
        SELECT action_id, created_at, started_at, finished_at,
               playbook, target_service, platform, incident_id, anomaly_id,
               trigger_reason, confidence, status, dry_run, execution_mode,
               actual_replicas_before, actual_replicas_after, simulated_replicas_after,
               result_message
        FROM healing_actions
        WHERE (:status::text IS NULL OR status = :status)
        ORDER BY created_at DESC
        LIMIT :limit
    """

    rows = await engine.db.fetch_all(query, {"status": status, "limit": limit})

    actions = []
    for row in rows:
        actions.append(
            {
                "action_id": row["action_id"],
                "created_at": row["created_at"].isoformat(),
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
                "playbook": row["playbook"],
                "target_service": row["target_service"],
                "platform": row["platform"],
                "incident_id": row["incident_id"],
                "anomaly_id": row["anomaly_id"],
                "trigger_reason": row["trigger_reason"],
                "confidence": row["confidence"],
                "status": row["status"],
                "dry_run": row["dry_run"],
                "execution_mode": row.get("execution_mode")
                or (
                    "real"
                    if not row.get("dry_run", True) and row.get("status") == "succeeded"
                    else "simulated"
                ),
                "actual_replicas_before": row.get("actual_replicas_before"),
                "actual_replicas_after": row.get("actual_replicas_after"),
                "simulated_replicas_after": row.get("simulated_replicas_after"),
                "result_message": row["result_message"],
            }
        )

    return {
        "actions": actions,
        "count": len(actions),
        "filter": {"status": status, "limit": limit},
    }


@router.get("/actions/{action_id}", tags=["healing"], summary="Get action details")
async def get_action(request: Request, action_id: str) -> dict[str, Any]:
    """Get full details of a specific healing action including snapshots."""
    engine = _engine(request)

    query = """
        SELECT action_id, created_at, started_at, finished_at,
               playbook, target_service, target_instance, platform,
               incident_id, anomaly_id, trigger_reason, confidence,
               status, dry_run, execution_mode,
               actual_replicas_before, actual_replicas_after, simulated_replicas_after,
               parameters, result_message,
               before_snapshot, after_snapshot
        FROM healing_actions
        WHERE action_id = :action_id
    """

    row = await engine.db.fetch_one(query, {"action_id": action_id})

    if not row:
        raise HTTPException(status_code=404, detail=f"Action {action_id} not found")

    return {
        "action_id": row["action_id"],
        "created_at": row["created_at"].isoformat(),
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "playbook": row["playbook"],
        "target_service": row["target_service"],
        "target_instance": row["target_instance"],
        "platform": row["platform"],
        "incident_id": row["incident_id"],
        "anomaly_id": row["anomaly_id"],
        "trigger_reason": row["trigger_reason"],
        "confidence": row["confidence"],
        "status": row["status"],
        "dry_run": row["dry_run"],
        "execution_mode": row.get("execution_mode")
        or (
            "real"
            if not row.get("dry_run", True) and row.get("status") == "succeeded"
            else "simulated"
        ),
        "actual_replicas_before": row.get("actual_replicas_before"),
        "actual_replicas_after": row.get("actual_replicas_after"),
        "simulated_replicas_after": row.get("simulated_replicas_after"),
        "parameters": row["parameters"],
        "result_message": row["result_message"],
        "before_snapshot": row["before_snapshot"],
        "after_snapshot": row["after_snapshot"],
    }
