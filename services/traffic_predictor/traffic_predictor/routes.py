"""HTTP surface of the traffic predictor (module 5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from aiops_common.log import get_logger

from .backtest import BACKTEST_ORIGINS, MIN_ACTUAL_TPS
from .capacity import MAX_REPLICAS, MIN_REPLICAS
from .forecaster import BASELINE_NAME, LEVEL_WINDOW_HOURS, MODEL_NAME
from .predictor import TrafficPredictor

log = get_logger(__name__)

router = APIRouter()


def _predictor(request: Request) -> TrafficPredictor:
    predictor: TrafficPredictor | None = getattr(request.app.state, "predictor", None)
    if predictor is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="Predictor not started")
    return predictor


@router.get("/status", tags=["predictor"], summary="Last forecast cycle")
async def status(request: Request) -> dict[str, Any]:
    return _predictor(request).status.as_dict()


@router.get("/accuracy", tags=["predictor"], summary="MAPE against the naive baseline")
async def accuracy(request: Request) -> dict[str, Any]:
    """The module's headline claim, in one place.

    Separate from `/status` because this is the number the module is judged on - "recorded MAPE
    beats a seasonal-naive baseline" - and it should not have to be dug out of a larger payload.
    """
    predictor = _predictor(request)
    report = predictor.status.accuracy
    comparisons = [
        entry
        for service in report.values()
        for entry in service.values()
        if entry.get("model_mape") is not None
    ]
    beaten = [entry for entry in comparisons if entry.get("beats_baseline")]
    return {
        "model": MODEL_NAME,
        "baseline": BASELINE_NAME,
        "comparisons": len(comparisons),
        "beats_baseline": len(beaten),
        "beats_baseline_fraction": (
            round(len(beaten) / len(comparisons), 4) if comparisons else None
        ),
        "by_service": report,
    }


@router.get("/config", tags=["predictor"], summary="Constants this predictor applies")
async def config(request: Request) -> dict[str, Any]:
    predictor = _predictor(request)
    return {
        "model": MODEL_NAME,
        "baseline": BASELINE_NAME,
        "horizons_hours": predictor.horizons,
        "history_days": predictor.history_days,
        "level_window_hours": LEVEL_WINDOW_HOURS,
        "backtest_origins": BACKTEST_ORIGINS,
        "min_actual_tps_for_mape": MIN_ACTUAL_TPS,
        "min_replicas": MIN_REPLICAS,
        "max_replicas": MAX_REPLICAS,
    }


@router.post("/run", tags=["predictor"], summary="Run one forecast cycle now")
async def run(request: Request) -> dict[str, Any]:
    """Forecast immediately instead of waiting for the next tick.

    Used by the verification script so it can assert on a cycle it triggered, rather than waiting
    an interval and hoping the timer fired.
    """
    predictor = _predictor(request)
    try:
        await predictor.run_once()
    except Exception as exc:
        # Re-raised as a 502 rather than swallowed: the caller asked for a forecast and needs to
        # know it did not happen.
        raise HTTPException(status_code=502, detail=f"Forecast failed: {exc}") from exc
    return predictor.status.as_dict()


@router.get(
    "/forecast/{service}", tags=["predictor"], summary="Newest stored forecast for a service"
)
async def latest_forecast(
    request: Request,
    service: str,
    horizon_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    """Read the most recent stored prediction, including its points.

    Reads from the table rather than recomputing, so what is returned is exactly what module 6
    would act on and what the dashboard would draw - not a fresh forecast that happens to differ.
    """
    predictor = _predictor(request)
    row = await predictor.latest_prediction(service, horizon_hours)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no {horizon_hours}h forecast stored for {service!r}",
        )

    for key in ("created_at", "peak_at"):
        if row.get(key) is not None:
            row[key] = row[key].isoformat()
    return row
