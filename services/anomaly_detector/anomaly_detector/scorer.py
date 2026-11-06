"""Score one `MetricSample` into an `AnomalyEvent` (module 4).

Pulls together the three pieces: the forest says *whether* a sample is unusual, the baseline
says *which signals* moved and by how many sigma, and `classify` turns that into a kind a
playbook can match. Kept separate from the stream consumer so it can be tested without Redis
and reused by a backfill or a what-if query later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, MetricSample

from .classify import IMPLICATION_SIGMA, classify
from .model import MODEL_NAME, ServiceModel

log = get_logger(__name__)

# Softness of the deviation-to-score curve, applied to `log1p` of the excess over
# IMPLICATION_SIGMA.
#
# The log matters. Sigma deviations span orders of magnitude - a near-constant signal like
# `error_ratio` can read thousands of sigma once a fault hits - and `tanh` on the raw value
# saturates to exactly 1.0 well before that. Every serious anomaly would then score 1.0, and
# the RCA engine, which ranks anomalies against each other, would have nothing to rank by.
# Compressing logarithmically first keeps the mapping monotone across the whole range:
# 2σ -> 0.50, 4σ -> 0.76, 10σ -> 0.91, 100σ -> 0.99, 3600σ -> 0.9998.
DEVIATION_SCALE = 1.9


@dataclass(frozen=True)
class ScoreResult:
    """A scored sample. `anomaly` is None when the sample is unremarkable."""

    service: str
    score: float
    raw_score: float
    anomaly: AnomalyEvent | None
    # Why nothing was emitted, when nothing was.
    skipped_reason: str = ""
    # The two detectors' scores, kept separate so a disagreement is visible rather than
    # averaged away. See `_deviation_score` for why there are two.
    forest_score: float = 0.0
    deviation_score: float = 0.0

    @property
    def decided_by(self) -> str:
        return "baseline" if self.deviation_score > self.forest_score else "forest"


def _deviation_score(deviations: dict[str, float]) -> float:
    """Score the largest deviation that points in a problematic direction.

    Why this exists alongside the Isolation Forest
    ----------------------------------------------
    An Isolation Forest is an *outlier* detector, not a *novelty* detector, and the difference
    bites here. Its trees contain only split thresholds learned from training data, so a point
    far outside that range is not isolated quickly - it follows the same path as a point just
    outside it, gets an average path length, and scores as **normal**.

    Measured on a model fitted to healthy central-ledger history: a sample with `error_ratio`
    of 0.35, which is 175x the baseline of 0.002, scored 0.0023. The forest called a
    catastrophic error rate perfectly ordinary. That is not a bug in the fit; it is what the
    algorithm does, and relying on it alone would have shipped a detector that misses exactly
    the faults module 9 injects.

    Standard-deviation distance does extrapolate, without bound. So the two are complementary
    and the final score is the maximum of both:

    * the **forest** catches unusual *combinations* of individually normal values, which no
      threshold rule can do and which is the reason it is here at all;
    * the **baseline** catches magnitudes outside anything ever observed, which the forest
      structurally cannot.

    Taking the max rather than a weighted blend keeps each detector's verdict legible. An
    average would let a confident baseline signal be diluted by an indifferent forest, and the
    resulting score would mean neither thing.
    """
    worst = 0.0
    for feature, deviation in deviations.items():
        # Direction matters, and `classify` owns that rule. Traffic is the only signal where
        # both directions indicate a problem; for everything else, below baseline is fine.
        magnitude = abs(deviation) if feature == "tps" else deviation
        worst = max(worst, magnitude)

    if worst < IMPLICATION_SIGMA:
        return 0.0
    excess = math.log1p(worst - IMPLICATION_SIGMA)
    return 0.5 * (1.0 + math.tanh(excess / DEVIATION_SCALE))


class Scorer:
    def __init__(self, threshold: float) -> None:
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    def score(self, sample: MetricSample, model: ServiceModel) -> ScoreResult:
        vector = sample.feature_vector(ANOMALY_FEATURES)
        forest_score, raw = model.score(vector)

        # Deviations are computed unconditionally, because they are half the decision now -
        # not just the explanation attached afterwards.
        deviations = model.baseline.deviations(sample, sample.timestamp)
        deviation_score = _deviation_score(deviations)
        normalised = max(forest_score, deviation_score)

        # Emit only past the configured decision boundary.
        #
        # Emitting only when normalised >= self._threshold prevents low-confidence fluctuations
        # from flooding the database and Redis streams with sub-threshold events.
        if normalised < self._threshold:
            return ScoreResult(
                sample.service,
                normalised,
                raw,
                None,
                skipped_reason=f"below the configured decision boundary threshold {self._threshold:.4f} (score: {normalised:.4f})",
                forest_score=forest_score,
                deviation_score=deviation_score,
            )

        result = classify(sample.service, deviations, normalised, self._threshold)

        anomaly = AnomalyEvent(
            window_start=sample.timestamp,
            service=sample.service,
            platform=sample.platform,
            kind=result.kind,
            severity=result.severity,
            score=round(normalised, 6),
            raw_score=round(raw, 6),
            threshold=self._threshold,
            contributing_features=result.contributing,
            # The values as observed, so an anomaly can be read months later without needing
            # the metrics table to still hold that window.
            feature_snapshot={
                feature: round(float(getattr(sample, feature, 0.0) or 0.0), 6)
                for feature in ANOMALY_FEATURES
            },
            feature_deviations=deviations,
            model_name=MODEL_NAME,
            model_version=model.version,
            description=result.description,
        )
        return ScoreResult(
            sample.service,
            normalised,
            raw,
            anomaly,
            forest_score=forest_score,
            deviation_score=deviation_score,
        )
