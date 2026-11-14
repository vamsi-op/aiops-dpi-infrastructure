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
from collections.abc import Sequence
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

# Below this, a feature's spread in training is float noise, not variation (see `fit`). The
# smallest genuine spread among the features is ~1e-5 (error_ratio in raw units).
MIN_FEATURE_SCALE = 1e-9

# Features where only the *high* side is a problem, for a seasonal model's forest. Being
# faster, cooler or less loaded than usual is not an incident, but the forest is
# direction-blind: it flagged account-lookup-service "critical" (0.93) for a p99 latency
# *below* its normal. Below-normal deviations of these are therefore clipped to 0 ("as
# expected") before the forest sees them. Traffic stays two-sided - a drop is as alarming as
# a surge - matching the direction rule `classify` and the deviation score already apply.
TWO_SIDED_FEATURES: frozenset[str] = frozenset({"tps"})


def harmful_direction(deviations: list[float]) -> list[float]:
    return [
        z if feature in TWO_SIDED_FEATURES else max(z, 0.0)
        for feature, z in zip(ANOMALY_FEATURES, deviations, strict=True)
    ]


# How far a p99 deviation can be trusted depends on how many requests sit in the top 1% of
# the 5-minute window. The simulator records min(40, tps) latency observations per second,
# so the tail holds ~3 x tps of them. At night (2-8 tps) that is a handful: one slow request
# moved quoting-service's p99 estimate from 0.152 s (true) to 0.19 s and, because consecutive
# windows overlap, kept it there for five minutes - read as a latency spike, and the service
# was restarted. The deviation is scaled by sqrt(tail / TAIL_SAMPLES_FOR_FULL_TRUST): full
# weight from ~17 tps up, two-thirds at 7.5 tps, a third at 2 tps. A real latency fault
# (x3) is many sigma out and still clears easily.
TAIL_SAMPLES_FOR_FULL_TRUST = 50.0
_P99 = ANOMALY_FEATURES.index("latency_p99_seconds")
_TPS = ANOMALY_FEATURES.index("tps")


def p99_reliability(tps: float) -> float:
    tail = 0.01 * 300 * min(40.0, max(1.0, float(tps)))
    return min(1.0, math.sqrt(tail / TAIL_SAMPLES_FOR_FULL_TRUST))


def weight_by_evidence(deviations: list[float], vector: list[float]) -> list[float]:
    out = list(deviations)
    out[_P99] *= p99_reliability(vector[_TPS])
    return out


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
    # True: the forest was fitted on each feature's deviation from that hour's baseline, not
    # on raw values. See `fit`. Artifacts saved before this existed load as False and keep
    # scoring raw values until retrained.
    seasonal: bool = False
    # True: below-normal deviations of one-sided features are clipped (`harmful_direction`).
    # False for artifacts saved before this existed, which keep their original inputs.
    one_sided: bool = False
    # True: p99 deviations are weighted by tail sample count (`weight_by_evidence`).
    evidence_weighted: bool = False

    def inputs(
        self, vector: list[float], hour: int | None, weekday: int | None = None
    ) -> list[float]:
        """What the forest sees: deviations from that time's normal for a seasonal model,
        else the raw vector."""
        if self.seasonal:
            deviations = self.baseline.deviation_vector(vector, hour, weekday)
            if not self.one_sided:
                return deviations
            deviations = harmful_direction(deviations)
            return weight_by_evidence(deviations, vector) if self.evidence_weighted else deviations
        return vector

    def raw_score(
        self, vector: list[float], hour: int | None = None, weekday: int | None = None
    ) -> float:
        """Model-native anomaly score. Positive means the forest calls it an outlier.

        `hour` (0-23, UTC) and `weekday` (Monday=0) locate the sample in the week; a seasonal
        model judges it against that time's normal. Without them the service-wide statistics
        are used.
        """
        matrix = self.scaler.transform(
            np.asarray([self.inputs(vector, hour, weekday)], dtype=float)
        )
        return float(-self.forest.decision_function(matrix)[0])

    def score(
        self, vector: list[float], hour: int | None = None, weekday: int | None = None
    ) -> tuple[float, float]:
        """Return `(normalised 0-1 score, raw score)`."""
        raw = self.raw_score(vector, hour, weekday)
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
    hours: Sequence[int] | None = None,
    weekdays: Sequence[int] | None = None,
) -> tuple[ServiceModel, dict[str, float]]:
    """Fit a scaler and forest for one service. Returns the model and training metrics.

    With `hours` (each vector's hour of day, UTC; plus `weekdays`, Monday=0) the model is
    **seasonal**: the forest is fitted on every feature's deviation from that time of week's
    baseline, in sigma, instead of on raw values.

    Why: on raw values the forest learns the *level* of each signal, and with
    `contamination` it is told that the most extreme 2% of history is anomalous. On a
    service with a daily and weekly cycle that 2% is simply the busiest and quietest hours
    of the week. Measured on the 28-day training set, ~40% of Friday-evening peak samples and
    ~9% of night-time trough samples scored above the alert threshold - so the detector
    fired every Friday evening and every night, on healthy traffic. Judging each sample
    against what is normal *for that hour* makes a busy Friday evening ordinary and leaves a
    busy 3am exactly as unusual as it should be.
    """
    if not vectors:
        raise ValueError(f"{service}: no training vectors")
    seasonal = hours is not None
    if seasonal:
        if len(hours) != len(vectors):  # type: ignore[arg-type]
            raise ValueError(f"{service}: {len(vectors)} vectors but {len(hours)} hours")  # type: ignore[arg-type]
        days: Sequence[int | None] = weekdays if weekdays is not None else [None] * len(hours)  # type: ignore[arg-type]
        if len(days) != len(vectors):
            raise ValueError(f"{service}: {len(vectors)} vectors but {len(days)} weekdays")
        vectors = [
            weight_by_evidence(
                harmful_direction(baseline.deviation_vector(vector, hour, weekday)), vector
            )
            for vector, hour, weekday in zip(vectors, hours, days, strict=True)  # type: ignore[arg-type]
        ]
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
    # A column that is constant in training (error_ratio on a healthy service, settlement lag
    # on services that do not report it) gets a standard deviation that is *almost* zero -
    # float noise around 1e-14 - rather than exactly zero, so sklearn does not apply its own
    # unit-scale guard. Scoring then divides live float noise by it: Prometheus reporting an
    # error ratio of 0.002000000000000021 instead of 0.002 scaled to 14.5 sigma and a
    # perfectly normal sample scored 0.97 "critical". Treat effectively-zero spread as zero.
    scaler.scale_ = np.where(scaler.scale_ < MIN_FEATURE_SCALE, 1.0, scaler.scale_)
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
        seasonal=seasonal,
        one_sided=seasonal,
        evidence_weighted=seasonal,
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
        "seasonal": 1.0 if seasonal else 0.0,
    }

    log.info(
        "model.fitted",
        monitored_service=service,
        rows=len(scaled),
        version=version,
        calibration_scale=round(model.scale, 4),
        train_outlier_ratio=round(metrics["train_outlier_ratio"], 4),
        train_above_threshold=round(metrics["train_above_0_65"], 5),
        seasonal=seasonal,
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
            "seasonal": model.seasonal,
            "one_sided": model.one_sided,
            "evidence_weighted": model.evidence_weighted,
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
        seasonal=bool(payload.get("seasonal", False)),
        one_sided=bool(payload.get("one_sided", False)),
        evidence_weighted=bool(payload.get("evidence_weighted", False)),
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
