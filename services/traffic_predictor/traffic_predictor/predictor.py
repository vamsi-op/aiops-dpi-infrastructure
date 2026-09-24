"""Produce, persist and publish forecasts on a schedule (module 5)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from aiops_common.bus import EventBus
from aiops_common.constants import TABLE_MODEL_RUNS, TABLE_PREDICTIONS, Stream
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.promql import PrometheusClient
from aiops_common.schemas import ForecastPoint, ModelRun, PredictionEvent, utc_now

from . import capacity as capacity_module
from .backtest import HorizonAccuracy, evaluate, headroom_for
from .forecaster import BASELINE_NAME, MODEL_NAME, fit, forecast
from .history import load_history, load_services

log = get_logger(__name__)

INSERT_PREDICTION = f"""
INSERT INTO {TABLE_PREDICTIONS} (
    prediction_id, created_at, service, platform, horizon_hours, current_tps,
    peak_predicted_tps, peak_at, points, current_replicas, recommended_replicas,
    tps_per_replica, scaling_action, model_name, model_version, backtest_mape
) VALUES (
    :prediction_id, :created_at, :service, :platform, :horizon_hours, :current_tps,
    :peak_predicted_tps, :peak_at, CAST(:points AS jsonb), :current_replicas,
    :recommended_replicas, :tps_per_replica, :scaling_action, :model_name, :model_version,
    :backtest_mape
) ON CONFLICT (prediction_id) DO NOTHING
"""

INSERT_MODEL_RUN = f"""
INSERT INTO {TABLE_MODEL_RUNS} (
    run_id, created_at, model_name, model_version, service, training_rows,
    duration_seconds, metrics, artifact_path, notes
) VALUES (
    :run_id, :created_at, :model_name, :model_version, :service, :training_rows,
    :duration_seconds, CAST(:metrics AS jsonb), :artifact_path, :notes
) ON CONFLICT (run_id) DO NOTHING
"""


@dataclass
class PredictorStatus:
    running: bool = False
    cycles: int = 0
    failures: int = 0
    predictions_written: int = 0
    services_forecast: int = 0
    services_skipped: list[str] = field(default_factory=list)
    model_version: str = ""
    last_run_at: str | None = None
    last_duration_seconds: float = 0.0
    last_error: str | None = None
    # horizon -> accuracy, from the most recent backtest.
    accuracy: dict[str, Any] = field(default_factory=dict)
    scaling_recommendations: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "cycles": self.cycles,
            "failures": self.failures,
            "predictions_written": self.predictions_written,
            "services_forecast": self.services_forecast,
            "services_skipped": self.services_skipped,
            "model_version": self.model_version,
            "last_run_at": self.last_run_at,
            "last_duration_seconds": round(self.last_duration_seconds, 3),
            "last_error": self.last_error,
            "accuracy": self.accuracy,
            "scaling_recommendations": self.scaling_recommendations,
        }


class PredictorMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.cycles = Counter(
            "aiops_forecast_cycles_total",
            "Forecast cycles, by outcome",
            ["outcome"],
            registry=registry,
        )
        self.duration = Histogram(
            "aiops_forecast_duration_seconds",
            "Wall-clock time for one forecast cycle across all services and horizons",
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
            registry=registry,
        )
        self.predictions = Counter(
            "aiops_predictions_written_total",
            "Predictions persisted, by service and horizon",
            ["service", "horizon_hours"],
            registry=registry,
        )
        self.mape = Gauge(
            "aiops_forecast_mape",
            "Backtest MAPE from the most recent evaluation",
            ["service", "horizon_hours", "model"],
            registry=registry,
        )
        self.beats_baseline = Gauge(
            "aiops_forecast_beats_baseline",
            "1 when the model's MAPE is below the seasonal-naive baseline's, else 0",
            ["service", "horizon_hours"],
            registry=registry,
        )
        self.recommended = Gauge(
            "aiops_recommended_replicas",
            "Replica count the forecast recommends",
            ["service", "horizon_hours"],
            registry=registry,
        )
        self.peak = Gauge(
            "aiops_predicted_peak_tps",
            "Predicted peak TPS over the horizon",
            ["service", "horizon_hours"],
            registry=registry,
        )
        self.errors = Counter(
            "aiops_forecast_errors_total",
            "Forecast failures, by stage",
            ["stage"],
            registry=registry,
        )


class TrafficPredictor:
    def __init__(
        self,
        database: Database,
        bus: EventBus,
        prometheus: PrometheusClient,
        metrics: PredictorMetrics,
        *,
        horizons: list[int],
        history_days: int,
        interval_seconds: int,
        min_headroom: float,
        platform: str = "mojaloop",
    ) -> None:
        self._db = database
        self._bus = bus
        self._prometheus = prometheus
        self._metrics = metrics
        self._horizons = sorted(horizons)
        self._history_days = history_days
        self._interval = interval_seconds
        self._min_headroom = min_headroom
        self._platform = platform
        self.status = PredictorStatus()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- introspection -----------------------------------------------------
    @property
    def horizons(self) -> list[int]:
        return list(self._horizons)

    @property
    def history_days(self) -> int:
        return self._history_days

    async def latest_prediction(
        self, service: str, horizon_hours: int
    ) -> dict[str, Any] | None:
        """The newest stored forecast, read back from the table.

        Read rather than recomputed on purpose: this returns exactly what module 6 would act on
        and what the dashboard would draw, not a fresh forecast that happens to differ from the
        stored one.
        """
        rows = await self._db.fetch_all(
            f"""
            SELECT prediction_id, created_at, service, horizon_hours, current_tps,
                   peak_predicted_tps, peak_at, points, current_replicas,
                   recommended_replicas, tps_per_replica, scaling_action, model_name,
                   model_version, backtest_mape
            FROM {TABLE_PREDICTIONS}
            WHERE service = :service AND horizon_hours = :horizon
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"service": service, "horizon": horizon_hours},
        )
        return dict(rows[0]) if rows else None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="traffic-predictor-loop")
            self.status.running = True
            log.info(
                "predictor.started",
                interval_seconds=self._interval,
                horizons=self._horizons,
            )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.running = False
        log.info("predictor.stopped", cycles=self.status.cycles)

    async def _run(self) -> None:
        # Forecast once at start-up rather than waiting a full interval. The interval is measured
        # in minutes because an hourly rollup does not change faster than that, and a service that
        # produced nothing for its first ten minutes looks broken.
        while not self._stopping.is_set():
            started = time.perf_counter()
            try:
                await self.run_once()
            except Exception:
                self._metrics.errors.labels("cycle").inc()
                self._metrics.cycles.labels("failure").inc()
                self.status.failures += 1
                log.exception("predictor.cycle_failed")
            delay = max(0.0, self._interval - (time.perf_counter() - started))
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    # -- one cycle ---------------------------------------------------------
    async def run_once(self) -> PredictorStatus:
        started = time.perf_counter()
        version = utc_now().strftime("%Y%m%d%H%M%S")
        services = await load_services(self._db, days=self._history_days)
        capacities = await capacity_module.read_capacity(self._prometheus)

        written = 0
        forecast_count = 0
        skipped: list[str] = []
        accuracy_report: dict[str, Any] = {}
        recommendations: dict[str, Any] = {}

        for service in services:
            history = await load_history(self._db, service, days=self._history_days)
            profile = fit(service, history)
            if profile is None:
                skipped.append(service)
                continue

            accuracy = evaluate(service, history, self._horizons)
            accuracy_report[service] = {
                str(h): a.as_dict() for h, a in sorted(accuracy.items())
            }
            for horizon, result in accuracy.items():
                if result.model_mape is not None:
                    self._metrics.mape.labels(service, str(horizon), MODEL_NAME).set(
                        result.model_mape
                    )
                if result.baseline_mape is not None:
                    self._metrics.mape.labels(service, str(horizon), BASELINE_NAME).set(
                        result.baseline_mape
                    )
                self._metrics.beats_baseline.labels(service, str(horizon)).set(
                    1.0 if result.beats_baseline else 0.0
                )

            capacity = capacities.get(service) or capacity_module.fallback_capacity(service)
            current_tps = history[-1].tps if history else 0.0
            origin = history[-1].at

            for horizon in self._horizons:
                event = self._build_event(
                    service=service,
                    horizon=horizon,
                    origin=origin,
                    current_tps=current_tps,
                    profile=profile,
                    history=history,
                    capacity=capacity,
                    accuracy=accuracy.get(horizon),
                    version=version,
                )
                await self._persist(event)
                await self._publish(event)
                written += 1
                self._metrics.predictions.labels(service, str(horizon)).inc()
                self._metrics.recommended.labels(service, str(horizon)).set(
                    event.recommended_replicas
                )
                self._metrics.peak.labels(service, str(horizon)).set(event.peak_predicted_tps)

                if event.scaling_action != "hold":
                    recommendations.setdefault(service, {})[str(horizon)] = {
                        "action": event.scaling_action,
                        "from": event.current_replicas,
                        "to": event.recommended_replicas,
                        "peak_tps": round(event.peak_predicted_tps, 2),
                        "peak_at": event.peak_at.isoformat() if event.peak_at else None,
                    }

            await self._record_run(service, version, history, accuracy)
            forecast_count += 1

        duration = time.perf_counter() - started
        self._metrics.duration.observe(duration)
        self._metrics.cycles.labels("success").inc()

        self.status.cycles += 1
        self.status.predictions_written += written
        self.status.services_forecast = forecast_count
        self.status.services_skipped = skipped
        self.status.model_version = version
        self.status.last_run_at = utc_now().isoformat()
        self.status.last_duration_seconds = duration
        self.status.last_error = None
        self.status.accuracy = accuracy_report
        self.status.scaling_recommendations = recommendations

        log.info(
            "predictor.cycle_complete",
            services=forecast_count,
            skipped=len(skipped),
            predictions=written,
            duration_seconds=round(duration, 2),
            recommending_change=len(recommendations),
        )
        return self.status

    def _build_event(
        self,
        *,
        service: str,
        horizon: int,
        origin: datetime,
        current_tps: float,
        profile: Any,
        history: list[Any],
        capacity: capacity_module.ServiceCapacity,
        accuracy: HorizonAccuracy | None,
        version: str,
    ) -> PredictionEvent:
        # `history` is required for the seasonal-naive half of the blend - see forecaster.forecast.
        raw_points = forecast(profile, history, start=origin, hours=horizon)
        points = [
            ForecastPoint(
                timestamp=at,
                predicted_tps=round(value, 4),
                lower_bound=round(lower, 4),
                upper_bound=round(upper, 4),
            )
            for at, value, lower, upper in raw_points
        ]

        peak_at, peak_tps = max(
            ((at, value) for at, value, _l, _u in raw_points),
            key=lambda pair: pair[1],
            default=(None, 0.0),
        )

        mape = accuracy.model_mape if accuracy else None
        # Headroom derived from measured accuracy, not picked. See backtest.headroom_for.
        headroom = headroom_for(mape, floor=self._min_headroom)
        recommendation = capacity_module.recommend(peak_tps, capacity, headroom=headroom)

        return PredictionEvent(
            service=service,
            platform=self._platform,
            horizon_hours=horizon,
            current_tps=round(max(0.0, current_tps), 4),
            peak_predicted_tps=round(max(0.0, peak_tps), 4),
            peak_at=peak_at,
            points=points,
            current_replicas=recommendation.current_replicas,
            recommended_replicas=recommendation.recommended_replicas,
            tps_per_replica=round(recommendation.tps_per_replica, 4),
            model_name=MODEL_NAME,
            model_version=version,
            backtest_mape=round(mape, 6) if mape is not None else None,
        )

    async def _persist(self, event: PredictionEvent) -> None:
        await self._db.execute(
            INSERT_PREDICTION,
            {
                "prediction_id": event.prediction_id,
                "created_at": event.created_at,
                "service": event.service,
                "platform": event.platform,
                "horizon_hours": event.horizon_hours,
                "current_tps": event.current_tps,
                "peak_predicted_tps": event.peak_predicted_tps,
                "peak_at": event.peak_at,
                "points": json.dumps([p.model_dump(mode="json") for p in event.points]),
                "current_replicas": event.current_replicas,
                "recommended_replicas": event.recommended_replicas,
                "tps_per_replica": event.tps_per_replica,
                # A computed property on the model, so it has to be read explicitly rather than
                # picked up by iterating fields - the same trap as MetricSample.error_ratio.
                "scaling_action": event.scaling_action,
                "model_name": event.model_name,
                "model_version": event.model_version,
                "backtest_mape": event.backtest_mape,
            },
        )

    async def _publish(self, event: PredictionEvent) -> None:
        # Database first, then the stream: module 6 will read a recommendation off the bus and
        # look it up by id before acting on it.
        await self._bus.publish(Stream.PREDICTIONS, event, event_type="PredictionEvent")

    async def _record_run(
        self,
        service: str,
        version: str,
        history: list[Any],
        accuracy: dict[int, HorizonAccuracy],
    ) -> None:
        """One `model_runs` row per service per cycle, carrying the MAPE comparison.

        This is where "we can prove it beats naive" actually lives. A MAPE in a log line is gone
        by tomorrow; in `model_runs` it is queryable per model version, which is what makes
        accuracy regressions visible across retrains.
        """
        metrics: dict[str, float] = {}
        for horizon, result in accuracy.items():
            if result.model_mape is not None:
                metrics[f"mape_{horizon}h"] = round(result.model_mape, 6)
            if result.baseline_mape is not None:
                metrics[f"baseline_mape_{horizon}h"] = round(result.baseline_mape, 6)
            metrics[f"beats_baseline_{horizon}h"] = 1.0 if result.beats_baseline else 0.0
            if result.improvement is not None:
                metrics[f"improvement_{horizon}h"] = round(result.improvement, 6)

        run = ModelRun(
            model_name=MODEL_NAME,
            model_version=version,
            service=service,
            training_rows=len(history),
            metrics=metrics,
            notes=(
                f"horizons={self._horizons} history_days={self._history_days} "
                f"baseline={BASELINE_NAME}"
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
