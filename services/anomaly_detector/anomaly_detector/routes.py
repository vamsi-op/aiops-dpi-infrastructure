"""HTTP surface of the anomaly detector (module 4)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.log import get_logger
from aiops_common.schemas import MetricSample

from .classify import DOMINANT_SIGMA, IMPLICATION_SIGMA, MULTIVARIATE_COUNT
from .detector import AnomalyDetector
from .trainer import Trainer

log = get_logger(__name__)

router = APIRouter()


def _detector(request: Request) -> AnomalyDetector:
    detector: AnomalyDetector | None = getattr(request.app.state, "detector", None)
    if detector is None:  # pragma: no cover - only if start-up failed
        raise HTTPException(status_code=503, detail="Detector not started")
    return detector


def _trainer(request: Request) -> Trainer:
    trainer: Trainer | None = getattr(request.app.state, "trainer", None)
    if trainer is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Trainer not available")
    return trainer


class TrainRequest(BaseModel):
    days: int = Field(
        default=28,
        ge=1,
        le=90,
        description=(
            "Days of history to train on. 28 matches scripts/replay_history.py and gives "
            "every hour-of-day bucket ~28 samples."
        ),
    )
    activate: bool = Field(
        default=True,
        description="Load the newly trained models into the running detector",
    )


class ScoreRequest(BaseModel):
    """A sample to score without waiting for one to arrive on the bus."""

    sample: dict[str, Any] = Field(description="A MetricSample as JSON; `service` is required")


@router.get("/status", tags=["detector"], summary="Detector and model state")
async def status(request: Request) -> dict[str, Any]:
    return _detector(request).snapshot()


@router.get("/config", tags=["detector"], summary="Thresholds this detector applies")
async def config(request: Request) -> dict[str, Any]:
    """Expose the decision constants.

    Discoverable rather than buried in source, so the verification script can assert against
    the values actually in force instead of duplicating them and letting the two drift.
    """
    detector = _detector(request)
    return {
        "features": list(ANOMALY_FEATURES),
        "score_threshold": detector.status.threshold,
        "decision_boundary": 0.5,
        "implication_sigma": IMPLICATION_SIGMA,
        "dominant_sigma": DOMINANT_SIGMA,
        "multivariate_count": MULTIVARIATE_COUNT,
    }


@router.post("/train", tags=["detector"], summary="Train a model per service")
async def train(request: Request, body: Annotated[TrainRequest, Body()]) -> dict[str, Any]:
    """Fit models from TimescaleDB history and record each run in `model_runs`.

    Runs inline. Five services over 28 days is a few seconds of work, and a caller asking for
    a retrain needs to know whether it succeeded - a background task would return 202 and leave
    them polling to find out.
    """
    trainer = _trainer(request)
    detector = _detector(request)
    try:
        report, models = await trainer.train_all(days=body.days)
    except Exception as exc:
        # Re-raised as a 500 rather than swallowed: a caller asking for a retrain needs to
        # know it did not happen.
        raise HTTPException(status_code=500, detail=f"Training failed: {exc}") from exc

    if body.activate and models:
        detector.set_models(models)

    payload = report.as_dict()
    payload["activated"] = bool(body.activate and models)
    return payload


@router.post("/score", tags=["detector"], summary="Score one sample now")
async def score(request: Request, body: Annotated[ScoreRequest, Body()]) -> dict[str, Any]:
    """Score a supplied sample against the live model.

    Exists so the detector's judgement can be probed directly. The alternative - inject a
    fault, wait, and see whether an anomaly appears - conflates the model being wrong with the
    pipeline being slow, and gives no way to ask "what would you say about *this* sample".
    """
    detector = _detector(request)
    try:
        sample = MetricSample.model_validate(body.sample)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Not a valid MetricSample: {exc}") from exc

    model = detector.models.get(sample.service)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"No model for {sample.service!r}. Trained: {sorted(detector.models)}",
        )

    result = detector.scorer.score(sample, model)
    return {
        "service": result.service,
        "score": round(result.score, 6),
        "raw_score": round(result.raw_score, 6),
        # Both detectors reported separately. A forest score near zero next to a high
        # deviation score is the normal, expected shape for an out-of-range fault - see the
        # note on `_deviation_score` - and hiding that behind one number makes the detector
        # look broken when it is working as designed.
        "forest_score": round(result.forest_score, 6),
        "deviation_score": round(result.deviation_score, 6),
        "decided_by": result.decided_by,
        "is_anomaly": result.anomaly is not None,
        "skipped_reason": result.skipped_reason,
        "anomaly": result.anomaly.model_dump(mode="json") if result.anomaly else None,
    }
