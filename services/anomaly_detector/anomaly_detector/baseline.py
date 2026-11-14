"""Per-service, time-of-week-aware baseline statistics (module 4).

Why this exists separately from the Isolation Forest
----------------------------------------------------
The forest answers "is this sample unusual". It cannot say *why*, and an anomaly a human
cannot interpret is an alert they will learn to ignore. These baselines answer the second
question: for this service, at this time of the week, how far from normal is each individual
signal, measured in standard deviations.

That is what fills `AnomalyEvent.feature_deviations`, what ranks
`contributing_features`, and what `classify` reads to pick an `AnomalyKind` so module 6 can
match a playbook. A seasonal model's forest also scores these deviations (see `model.fit`).

Why time of week
----------------
The simulator's traffic follows a two-peak daily curve with roughly 2.7x between trough and
peak, and a weekday/weekend swing on top. A single global mean would put every busy afternoon
several sigma above "normal" and every quiet night several sigma below, so the detector would
fire twice a day, for ever, on healthy traffic. Bucketing by hour removes the daily cycle.

Hour alone still pooled busy weekdays with quiet weekends. Friday evening - the busiest hour
of the week - then sat well above an "all days at 19:00" average, and the detector raised
warnings every Friday evening on healthy traffic. So statistics are kept per
**(weekday, hour)** as well, and used whenever that bucket has enough samples
(MIN_BUCKET_SAMPLES). The 28-day training set holds 16+ rows per (weekday, hour) - 15-minute
rows over four weeks, plus 1-minute rows for the latest two days - and live collection adds
four a minute. Sparse buckets fall back to hour of day, then to the service-wide figures.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiops_common.constants import ANOMALY_FEATURES
from aiops_common.log import get_logger

log = get_logger(__name__)

# Below this many samples in an (service, hour) bucket, fall back to the service-wide
# statistics. A sigma computed from a handful of points produces enormous deviations on
# ordinary data, which is worse than being slightly less specific.
MIN_BUCKET_SAMPLES = 8

# Floor on sigma, relative to the mean, before computing a deviation.
#
# Some signals are legitimately almost constant - `error_ratio` sits at 0.002 with almost no
# variance on a healthy service. A true sigma near zero makes (value - mean) / sigma explode,
# so a jump from 0.002 to 0.004 would read as hundreds of sigma and outrank a genuine outage.
# The floor keeps deviations comparable across signals of very different scales.
RELATIVE_SIGMA_FLOOR = 0.05

# Absolute floor for signals whose mean is itself near zero.
ABSOLUTE_SIGMA_FLOOR = 1e-6


@dataclass(frozen=True)
class FeatureStats:
    mean: float
    sigma: float
    samples: int

    def deviation(self, value: float) -> float:
        """Signed deviation in sigma. Positive means above the baseline."""
        return (value - self.mean) / self.sigma if self.sigma > 0 else 0.0


@dataclass
class ServiceBaseline:
    """Baseline statistics for one service."""

    service: str
    # (hour, feature) -> stats
    hourly: dict[tuple[int, str], FeatureStats] = field(default_factory=dict)
    # (weekday, hour, feature) -> stats; weekday is Monday=0. Preferred when well populated.
    weekly: dict[tuple[int, int, str], FeatureStats] = field(default_factory=dict)
    # feature -> stats, over all hours. The fallback for sparse buckets.
    overall: dict[str, FeatureStats] = field(default_factory=dict)

    def stats_for(
        self, feature: str, hour: int, weekday: int | None = None
    ) -> FeatureStats | None:
        """Most specific well-populated statistics: (weekday, hour), then hour, then overall."""
        if weekday is not None:
            stats = self.weekly.get((weekday, hour, feature))
            if stats is not None and stats.samples >= MIN_BUCKET_SAMPLES:
                return stats
        stats = self.hourly.get((hour, feature))
        if stats is not None and stats.samples >= MIN_BUCKET_SAMPLES:
            return stats
        return self.overall.get(feature)

    def deviation_vector(
        self, values: Sequence[float], hour: int | None, weekday: int | None = None
    ) -> list[float]:
        """`values` (ordered as ANOMALY_FEATURES) as signed sigma from that time's normal.

        Unrounded, for the forest. `hour=None` uses the service-wide statistics. A feature
        with no statistics at all reads 0.0 - "as expected" - rather than its raw value, which
        would be on a different scale from every other column.
        """
        out = []
        for feature, value in zip(ANOMALY_FEATURES, values, strict=True):
            if hour is None:
                stats = self.overall.get(feature)
            else:
                stats = self.stats_for(feature, hour, weekday)
            out.append(stats.deviation(float(value)) if stats is not None else 0.0)
        return out

    def deviations(self, sample: Any, at: datetime) -> dict[str, float]:
        """Signed sigma deviation for every feature in `ANOMALY_FEATURES`."""
        hour, weekday = at.hour, at.weekday()
        out: dict[str, float] = {}
        for feature in ANOMALY_FEATURES:
            stats = self.stats_for(feature, hour, weekday)
            if stats is None:
                continue
            value = float(getattr(sample, feature, 0.0) or 0.0)
            out[feature] = round(stats.deviation(value), 3)
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, for storing alongside the model artifact."""
        return {
            "service": self.service,
            "hourly": {
                f"{hour}|{feature}": [s.mean, s.sigma, s.samples]
                for (hour, feature), s in self.hourly.items()
            },
            "weekly": {
                f"{weekday}|{hour}|{feature}": [s.mean, s.sigma, s.samples]
                for (weekday, hour, feature), s in self.weekly.items()
            },
            "overall": {f: [s.mean, s.sigma, s.samples] for f, s in self.overall.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ServiceBaseline:
        baseline = cls(service=str(payload["service"]))
        for key, (mean, sigma, samples) in payload.get("hourly", {}).items():
            hour_text, feature = key.split("|", 1)
            baseline.hourly[(int(hour_text), feature)] = FeatureStats(
                float(mean), float(sigma), int(samples)
            )
        # Absent from artifacts saved before weekday buckets existed: those fall back to hourly.
        for key, (mean, sigma, samples) in payload.get("weekly", {}).items():
            weekday_text, hour_text, feature = key.split("|", 2)
            baseline.weekly[(int(weekday_text), int(hour_text), feature)] = FeatureStats(
                float(mean), float(sigma), int(samples)
            )
        for feature, (mean, sigma, samples) in payload.get("overall", {}).items():
            baseline.overall[feature] = FeatureStats(float(mean), float(sigma), int(samples))
        return baseline


def _stats(values: list[float]) -> FeatureStats | None:
    if not values:
        return None
    mean = statistics.fmean(values)
    sigma = statistics.stdev(values) if len(values) > 1 else 0.0
    # Apply the floors here rather than at deviation time, so the stored baseline is already
    # the thing that gets used and there is no second place for the rule to drift.
    floor = max(abs(mean) * RELATIVE_SIGMA_FLOOR, ABSOLUTE_SIGMA_FLOOR)
    return FeatureStats(mean=mean, sigma=max(sigma, floor), samples=len(values))


def build_baseline(service: str, rows: list[dict[str, Any]]) -> ServiceBaseline:
    """Compute baselines from raw `service_metrics` rows.

    `rows` need a `timestamp` plus one key per feature in `ANOMALY_FEATURES`.
    """
    baseline = ServiceBaseline(service=service)
    by_hour: dict[tuple[int, str], list[float]] = {}
    by_week: dict[tuple[int, int, str], list[float]] = {}
    by_feature: dict[str, list[float]] = {f: [] for f in ANOMALY_FEATURES}

    for row in rows:
        moment = row["timestamp"]
        hour, weekday = moment.hour, moment.weekday()
        for feature in ANOMALY_FEATURES:
            raw = row.get(feature)
            if raw is None:
                continue
            value = float(raw)
            if math.isnan(value) or math.isinf(value):
                continue
            by_hour.setdefault((hour, feature), []).append(value)
            by_week.setdefault((weekday, hour, feature), []).append(value)
            by_feature[feature].append(value)

    for key, values in by_hour.items():
        stats = _stats(values)
        if stats is not None:
            baseline.hourly[key] = stats

    for key, values in by_week.items():
        stats = _stats(values)
        if stats is not None:
            baseline.weekly[key] = stats

    for feature, values in by_feature.items():
        stats = _stats(values)
        if stats is not None:
            baseline.overall[feature] = stats

    hours_covered = len({hour for hour, _ in baseline.hourly})
    log.info(
        "baseline.built",
        monitored_service=service,
        rows=len(rows),
        hours_covered=hours_covered,
        features=len(baseline.overall),
    )
    if hours_covered < 24:
        # Not fatal - `stats_for` falls back to the service-wide figures - but it means the
        # seasonality correction is partial, and that is worth knowing before wondering why
        # the detector is noisy at 3am.
        log.warning(
            "baseline.incomplete_day",
            monitored_service=service,
            hours_covered=hours_covered,
            detail="fewer than 24 hourly buckets; deviations fall back to service-wide stats",
        )
    return baseline
