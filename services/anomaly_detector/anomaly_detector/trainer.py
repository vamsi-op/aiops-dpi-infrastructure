"""Train one model per service from TimescaleDB history, and record the run (module 4).

Per service, not one global model. `central-ledger` runs at 26 tps with p99 near 0.22s while
`transaction-request-service` runs at 6 tps; a shared `StandardScaler` would normalise them
onto one distribution and then every sample from the smaller service would sit far from the
pooled mean. Five models, five scalers, five baselines.

Every run writes a `model_runs` row. That table exists so model quality is traceable over time
rather than a number someone remembers, and the metrics recorded include the one figure that
predicts whether the detector will be noisy in production: what fraction of the *training* data
would clear the alerting threshold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiops_common.constants import ANOMALY_FEATURES, TABLE_METRICS, TABLE_MODEL_RUNS
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import ModelRun, utc_now

from . import model as model_module
from .baseline import build_baseline
from .model import MODEL_NAME, ServiceModel

log = get_logger(__name__)

# Training data is read from the raw table rather than the 1-minute rollup.
#
# The rollup averages away exactly the short spikes the detector needs to recognise: a
# `network_latency` fault takes p99 from 0.22s to 2.0s, and a one-minute mean of that is a
# blunted version of the thing being detected. The raw table is also what the detector scores
# against at runtime, so training and scoring see the same resolution.
SELECT_HISTORY = f"""
SELECT "timestamp", {", ".join(ANOMALY_FEATURES)},
       COALESCE(labels->>'source', 'unknown') AS source
FROM {TABLE_METRICS}
WHERE service = :service
  AND "timestamp" > now() - make_interval(days => :days)
ORDER BY "timestamp"
"""

INSERT_MODEL_RUN = f"""
INSERT INTO {TABLE_MODEL_RUNS} (
    run_id, created_at, model_name, model_version, service, training_rows,
    duration_seconds, metrics, artifact_path, notes
) VALUES (
    :run_id, :created_at, :model_name, :model_version, :service, :training_rows,
    :duration_seconds, CAST(:metrics AS jsonb), :artifact_path, :notes
)
ON CONFLICT (run_id) DO NOTHING
"""

SELECT_SERVICES = f"""
SELECT DISTINCT service
FROM {TABLE_METRICS}
WHERE "timestamp" > now() - make_interval(days => :days)
ORDER BY service
"""


@dataclass
class ServiceTrainingResult:
    service: str
    trained: bool
    rows: int
    version: str = ""
    artifact_path: str = ""
    metrics: dict[str, float] | None = None
    reason: str = ""
    data_sources: tuple[str, ...] = ()


@dataclass
class TrainingReport:
    results: list[ServiceTrainingResult]
    duration_seconds: float

    @property
    def trained(self) -> list[ServiceTrainingResult]:
        return [r for r in self.results if r.trained]

    @property
    def skipped(self) -> list[ServiceTrainingResult]:
        return [r for r in self.results if not r.trained]

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "trained": [
                {
                    "service": r.service,
                    "rows": r.rows,
                    "version": r.version,
                    "artifact": r.artifact_path,
                    "data_sources": list(r.data_sources),
                    "metrics": {k: round(v, 6) for k, v in (r.metrics or {}).items()},
                }
                for r in self.trained
            ],
            "skipped": [
                {"service": r.service, "rows": r.rows, "reason": r.reason} for r in self.skipped
            ],
        }


class Trainer:
    def __init__(
        self,
        database: Database,
        *,
        artifact_dir: Path,
        contamination: float,
        min_training_rows: int,
    ) -> None:
        self._db = database
        self._artifact_dir = artifact_dir
        self._contamination = contamination
        self._min_rows = min_training_rows

    async def discover_services(self, days: int) -> list[str]:
        rows = await self._db.fetch_all(SELECT_SERVICES, {"days": days})
        return [str(row["service"]) for row in rows]

    async def train_all(
        self, *, days: int = 28
    ) -> tuple[TrainingReport, dict[str, ServiceModel]]:
        started = time.perf_counter()
        services = await self.discover_services(days)
        if not services:
            log.warning("train.no_services", days=days)
            return TrainingReport([], time.perf_counter() - started), {}

        results: list[ServiceTrainingResult] = []
        models: dict[str, ServiceModel] = {}
        # One version stamp for the whole run, so five per-service artifacts trained from the
        # same window share an identifier. Chasing a scoring discrepancy across models with
        # five different versions is needlessly hard.
        version = utc_now().strftime("%Y%m%d%H%M%S")

        for service in services:
            result, trained_model = await self._train_one(service, days=days, version=version)
            results.append(result)
            if trained_model is not None:
                models[service] = trained_model

        report = TrainingReport(results, time.perf_counter() - started)
        log.info(
            "train.complete",
            trained=len(report.trained),
            skipped=len(report.skipped),
            duration_seconds=round(report.duration_seconds, 2),
        )
        return report, models

    async def _train_one(
        self, service: str, *, days: int, version: str
    ) -> tuple[ServiceTrainingResult, ServiceModel | None]:
        rows = await self._db.fetch_all(SELECT_HISTORY, {"service": service, "days": days})
        if len(rows) < self._min_rows:
            reason = (
                f"only {len(rows)} rows in {days}d, need {self._min_rows} "
                "(ANOMALY_MIN_TRAINING_ROWS). Run scripts/replay_history.py"
            )
            log.warning("train.insufficient_data", monitored_service=service, rows=len(rows))
            return ServiceTrainingResult(service, False, len(rows), reason=reason), None

        # Recorded on the model so provenance is never lost. A model fitted on replayed data
        # and one fitted on pipeline-observed data are different artifacts, and the difference
        # should be visible in `model_runs` without archaeology.
        sources = tuple(sorted({str(row["source"]) for row in rows}))

        started = time.perf_counter()
        baseline = build_baseline(service, rows)
        vectors = [[float(row[f] or 0.0) for f in ANOMALY_FEATURES] for row in rows]

        fitted, metrics = model_module.fit(
            service,
            vectors,
            baseline,
            version=version,
            contamination=self._contamination,
            data_sources=sources,
        )
        artifact = model_module.save(fitted, self._artifact_dir)
        duration = time.perf_counter() - started

        await self._record_run(
            service=service,
            version=version,
            rows=len(rows),
            duration=duration,
            metrics=metrics,
            artifact=artifact,
            sources=sources,
            days=days,
        )

        noisy = metrics.get("train_above_0_65", 0.0)
        if noisy > 0.05:
            # A detector that would fire on more than 5% of its own training data will be
            # ignored within a day. Loud here, where it can still be acted on.
            log.warning(
                "train.model_likely_noisy",
                monitored_service=service,
                train_above_threshold=round(noisy, 4),
                detail=(
                    "more than 5% of training data clears ANOMALY_SCORE_THRESHOLD; the "
                    "history may contain real incidents, or contamination is too high"
                ),
            )

        return (
            ServiceTrainingResult(
                service=service,
                trained=True,
                rows=len(rows),
                version=version,
                artifact_path=str(artifact),
                metrics=metrics,
                data_sources=sources,
            ),
            fitted,
        )

    async def _record_run(
        self,
        *,
        service: str,
        version: str,
        rows: int,
        duration: float,
        metrics: dict[str, float],
        artifact: Path,
        sources: tuple[str, ...],
        days: int,
    ) -> None:
        import json

        window_start = utc_now() - timedelta(days=days)
        run = ModelRun(
            model_name=MODEL_NAME,
            model_version=version,
            service=service,
            training_rows=rows,
            duration_seconds=duration,
            metrics=metrics,
            artifact_path=str(artifact),
            notes=(
                f"features={len(ANOMALY_FEATURES)} "
                f"window={window_start:%Y-%m-%d}..{utc_now():%Y-%m-%d} "
                f"data_sources={','.join(sources) or 'unknown'}"
            ),
        )
        await self._db.execute(
            INSERT_MODEL_RUN,
            {
                "run_id": run.run_id,
                "created_at": run.created_at,
                "model_name": run.model_name,
                "model_version": run.model_version,
                "service": run.service,
                "training_rows": run.training_rows,
                "duration_seconds": run.duration_seconds,
                "metrics": json.dumps(run.metrics),
                "artifact_path": run.artifact_path,
                "notes": run.notes,
            },
        )
