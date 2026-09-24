"""Isolation Forest wrapper with calibrated 0-1 scoring (module 4).

The calibration is the part that needed thought
-----------------------------------------------
`AnomalyEvent.score` is documented as 0.0-1.0 with 1.0 most anomalous, and
`ANOMALY_SCORE_THRESHOLD` defaults to 0.65. scikit-learn's `decision_function` returns an
unbounded signed value - negative for outliers - so it has to be mapped into that range, and
*how* decides whether the threshold means anything.

The obvious mapping is percentile of the training distribution. It is wrong here. With
`contamination = 0.02` the model treats 2% of training data as anomalous, so a score equal to
the training percentile would put 0.65 at the 65th percentile: a third of perfectly normal
samples would clear the threshold and every service would alert continuously.

Instead the mapping is anchored on the model's **own decision boundary**:

    raw   = -decision_function(x)      positive means the model says outlier
    score = 0.5 * (1 + tanh(raw / scale))

So `score == 0.5` exactly where the forest flips from inlier to outlier, `score > 0.5` means
the model calls it an anomaly at all, and 0.65 sits meaningfully beyond that boundary rather
than somewhere arbitrary in the bulk of normal data. `scale` is the spread of raw scores over
the training set, so the curve adapts to each service instead of assuming a magnitude.

tanh rather than a linear clip because it is monotone, bounded, and never saturates hard: a
genuinely extreme outage still scores higher than a merely bad one, which matters when the RCA
engine ranks anomalies against each other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.log import get_logger

from .baseline import ServiceBaseline

log = get_logger(__name__)

MODEL_NAME = "isolation-forest"

# Fixed so a rebuild from the same data produces the same model. An Isolation Forest is
# genuinely sensitive to its RNG, and an unexplained change in scores is a bad afternoon.
RANDOM_STATE = 42

# Enough trees that scores are stable between runs without making scoring slow. Measured at
# 200: scoring one sample is well under a millisecond, which matters because the detector
# scores every service on every 15-second cycle.
N_ESTIMATORS = 200

# Floor on the calibration scale, so a service whose training scores barely vary cannot
# produce a divide-by-almost-zero and slam every sample to 0 or 1.
MIN_SCALE = 1e-3


@dataclass
class ServiceModel:
    """A fitted scaler, forest and baseline for one service."""

    service: str
    version: str
    scaler: StandardScaler
    forest: IsolationForest
    baseline: ServiceBaseline
    # Spread of `-decision_function` over the training set; the calibration denominator.
    scale: float
    training_rows: int
    features: tuple[str, ...] = ANOMALY_FEATURES
    # Recorded so a model can always be traced to the kind of data it learned from.
    data_sources: tuple[str, ...] = ()

    def raw_score(self, vector: list[float]) -> float:
        """Model-native anomaly score. Positive means the forest calls it an outlier."""
        matrix = self.scaler.transform(np.asarray([vector], dtype=float))
        return float(-self.forest.decision_function(matrix)[0])

    def score(self, vector: list[float]) -> tuple[float, float]:
        """Return `(normalised 0-1 score, raw score)`."""
        raw = self.raw_score(vector)
        normalised = 0.5 * (1.0 + math.tanh(raw / max(self.scale, MIN_SCALE)))
        # Clamp for the schema's `ge=0.0, le=1.0`; tanh cannot exceed it, but floating point
        # at the extremes can produce 1.0000000000000002.
        return min(max(normalised, 0.0), 1.0), raw

    @property
    def is_outlier_boundary(self) -> float:
        """The normalised score corresponding to the forest's own decision boundary."""
        return 0.5


def fit(
    service: str,
    vectors: list[list[float]],
    baseline: ServiceBaseline,
    *,
    version: str,
    contamination: float,
    data_sources: tuple[str, ...] = (),
) -> tuple[ServiceModel, dict[str, float]]:
    """Fit a scaler and forest for one service. Returns the model and training metrics."""
    matrix = np.asarray(vectors, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError(f"{service}: no training vectors")

    # Replace any non-finite value rather than letting it poison the scaler. A single NaN in
    # a column makes that column's mean NaN, which makes every subsequent score NaN - and the
    # symptom appears at scoring time, far from the cause.
    non_finite = int((~np.isfinite(matrix)).sum())
    if non_finite:
        log.warning(
            "model.non_finite_inputs",
            monitored_service=service,
            count=non_finite,
            detail="replaced with 0.0 before scaling",
        )
        matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)

    scaler = StandardScaler().fit(matrix)
    scaled = scaler.transform(matrix)

    forest = IsolationForest(
        n_estimators=N_ESTIMATORS,
        contamination=contamination,
        random_state=RANDOM_STATE,
        # Every tree sees every sample. The default ("auto", 256) subsamples, which at our
        # data sizes adds variance between runs for no benefit in speed.
        max_samples=min(len(scaled), 4096),
        n_jobs=1,
    )
    forest.fit(scaled)

    raw = -forest.decision_function(scaled)
    scale = float(np.std(raw))
    flagged = int((raw > 0).sum())

    model = ServiceModel(
        service=service,
        version=version,
        scaler=scaler,
        forest=forest,
        baseline=baseline,
        scale=max(scale, MIN_SCALE),
        training_rows=len(scaled),
        data_sources=data_sources,
    )

    # Metrics land in `model_runs.metrics`, so training quality is traceable over time
    # rather than a number someone remembers seeing once.
    #
    # Vectorised deliberately. Calling `model.score()` per row means one `scaler.transform`
    # and one `decision_function` call per sample, and sklearn's per-call overhead dominates
    # at this size - it turned a sub-second fit into several seconds, which was noticeable
    # across a test suite that fits a model per test. `raw` is already the whole column, so
    # the tanh mapping applies to it directly.
    normalised = 0.5 * (1.0 + np.tanh(raw / max(scale, MIN_SCALE)))
    metrics = {
        "training_rows": float(len(scaled)),
        "features": float(matrix.shape[1]),
        "contamination": float(contamination),
        "calibration_scale": model.scale,
        # What fraction of the training data the forest itself calls an outlier. Should land
        # near `contamination`; far off means the data is not as homogeneous as assumed.
        "train_outlier_ratio": flagged / len(scaled),
        "raw_score_mean": float(np.mean(raw)),
        "raw_score_p99": float(np.percentile(raw, 99)),
        # The headline sanity figure: how much of the *training* data would clear the alerting
        # threshold. On clean history this should be a fraction of a percent. If it is high,
        # the detector will be noisy in production and the threshold or the data is wrong -
        # and this is the number that says so before deployment rather than after.
        "train_above_0_65": float((normalised >= 0.65).mean()),
        "train_above_boundary": float((normalised > 0.5).mean()),
    }

    log.info(
        "model.fitted",
        monitored_service=service,
        rows=len(scaled),
        version=version,
        calibration_scale=round(model.scale, 4),
        train_outlier_ratio=round(metrics["train_outlier_ratio"], 4),
        train_above_threshold=round(metrics["train_above_0_65"], 5),
    )
    return model, metrics


def save(model: ServiceModel, directory: Path) -> Path:
    """Persist a model. Baseline travels with it, never separately.

    A scaler fitted on one dataset and a baseline computed from another would produce
    deviations that disagree with the score, so they are stored as one artifact.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{MODEL_NAME}-{model.service}.joblib"
    joblib.dump(
        {
            "service": model.service,
            "version": model.version,
            "scaler": model.scaler,
            "forest": model.forest,
            "baseline": model.baseline.to_dict(),
            "scale": model.scale,
            "training_rows": model.training_rows,
            "features": list(model.features),
            "data_sources": list(model.data_sources),
        },
        path,
    )
    return path


def load(path: Path) -> ServiceModel:
    payload: dict[str, Any] = joblib.load(path)
    features = tuple(payload.get("features", ANOMALY_FEATURES))
    if features != ANOMALY_FEATURES:
        # `ANOMALY_FEATURES` is documented as append-only and part of the model contract.
        # Loading a model fitted on a different vector would silently score the wrong columns,
        # so refuse rather than produce confident nonsense.
        raise ValueError(
            f"{path.name} was fitted on {features}, but ANOMALY_FEATURES is now "
            f"{ANOMALY_FEATURES}. Retrain: the feature vector is part of the contract."
        )
    return ServiceModel(
        service=str(payload["service"]),
        version=str(payload["version"]),
        scaler=payload["scaler"],
        forest=payload["forest"],
        baseline=ServiceBaseline.from_dict(payload["baseline"]),
        scale=float(payload["scale"]),
        training_rows=int(payload.get("training_rows", 0)),
        features=features,
        data_sources=tuple(payload.get("data_sources", ())),
    )


def load_all(directory: Path) -> dict[str, ServiceModel]:
    """Load every model in `directory`, keyed by service."""
    models: dict[str, ServiceModel] = {}
    if not directory.is_dir():
        log.warning("model.artifact_dir_missing", path=str(directory))
        return models
    for path in sorted(directory.glob(f"{MODEL_NAME}-*.joblib")):
        try:
            model = load(path)
        except Exception as exc:  # noqa: BLE001 - one bad artifact must not block the rest
            log.error("model.load_failed", path=str(path), error=str(exc))
            continue
        models[model.service] = model
    log.info("model.loaded", count=len(models), services=sorted(models))
    return models
