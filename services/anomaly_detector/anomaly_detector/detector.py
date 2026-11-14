"""Consume `aiops.metrics`, score each sample, persist and publish anomalies (module 4).

Reads the stream rather than polling the table. The metrics agent already publishes one event
per service per cycle, so the work arrives pushed; polling would add latency for no benefit and
duplicate the agent's own schedule. A consumer group is used rather than a plain read, for two
reasons that matter later: progress survives a restart, so a redeploy does not silently skip
the window it was down for, and a second replica can join the group when one process is no
longer enough.

The module's verification criterion is that an injected fault produces an anomaly on
`aiops.anomalies` within one scrape interval, so the path from event to publish stays short and
does nothing that can block.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from aiops_common.bus import Event, EventBus
from aiops_common.constants import TABLE_ANOMALIES, Stream
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import AnomalyEvent, MetricSample, utc_now

from .model import ServiceModel
from .scorer import Scorer

log = get_logger(__name__)

CONSUMER_GROUP = "anomaly-detector"
# Cooldown prevents multiple anomalies from the same issue
# 15s = fast demo response while preventing spam from baseline noise
ANOMALY_COOLDOWN_SECONDS = 15

# Persistence: emit only when a service has looked anomalous on this many consecutive
# samples, unless the score is severe enough to act on at once.
#
# Each 15-second sample is scored on its own, and a 2%-contamination model will
# occasionally score an ordinary sample above its boundary. Measured on live Saturday-night
# traffic, single-sample decisions flagged 8.9% of samples; requiring two in a row cut that to
# 1.5% while every injected fault (latency x3, CPU+memory hog) still scored 0.98-1.0 and
# persists for minutes. The cost is one extra scrape (15 s) of time-to-detect for moderate
# anomalies; severe ones are not delayed.
#
# Skipping persistence also needs one signal to be extreme (IMMEDIATE_SIGMA). The score
# alone is not enough: a sample with connections and CPU both ~2.1 sigma high scored 0.959,
# was emitted at once as a queue_backlog (queue depth 0), and the restart it triggered cost
# its callers five minutes of failed-call alarms.
PERSISTENCE_SAMPLES = 2
IMMEDIATE_SCORE = 0.95
IMMEDIATE_SIGMA = 4.0

INSERT_ANOMALY = f"""
INSERT INTO {TABLE_ANOMALIES} (
    anomaly_id, detected_at, window_start, service, platform, kind, severity,
    score, raw_score, threshold, contributing_features, feature_snapshot,
    feature_deviations, model_name, model_version, description, incident_id
) VALUES (
    :anomaly_id, :detected_at, :window_start, :service, :platform, :kind, :severity,
    :score, :raw_score, :threshold, :contributing_features, CAST(:feature_snapshot AS jsonb),
    CAST(:feature_deviations AS jsonb), :model_name, :model_version, :description, :incident_id
) ON CONFLICT (anomaly_id) DO NOTHING
"""


@dataclass
class DetectorStatus:
    running: bool = False
    models_loaded: int = 0
    model_version: str = ""
    samples_scored: int = 0
    anomalies_detected: int = 0
    anomalies_actionable: int = 0
    unknown_services: list[str] = field(default_factory=list)
    last_sample_at: str | None = None
    last_anomaly_at: str | None = None
    last_error: str | None = None
    threshold: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "models_loaded": self.models_loaded,
            "model_version": self.model_version,
            "samples_scored": self.samples_scored,
            "anomalies_detected": self.anomalies_detected,
            "anomalies_actionable": self.anomalies_actionable,
            "unknown_services": sorted(self.unknown_services),
            "last_sample_at": self.last_sample_at,
            "last_anomaly_at": self.last_anomaly_at,
            "last_error": self.last_error,
            "threshold": self.threshold,
        }


class DetectorMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.scored = Counter(
            "aiops_samples_scored_total",
            "Metric samples scored, by service",
            ["service"],
            registry=registry,
        )
        self.anomalies = Counter(
            "aiops_anomalies_detected_total",
            "Anomalies emitted, by service, kind and severity",
            ["service", "kind", "severity"],
            registry=registry,
        )
        self.latency = Histogram(
            "aiops_detection_latency_seconds",
            "Time from reading a metric event to publishing the anomaly",
            # The verification criterion is "within one scrape interval", so the buckets have
            # to straddle 15s to be able to show whether that held.
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0),
            registry=registry,
        )
        self.errors = Counter(
            "aiops_detection_errors_total",
            "Failures while handling an event, by stage",
            ["stage"],
            registry=registry,
        )
        self.no_model = Counter(
            "aiops_detection_no_model_total",
            "Samples dropped because no model exists for that service",
            ["service"],
            registry=registry,
        )
        self.models = Gauge(
            "aiops_models_loaded",
            "Per-service models currently in memory",
            registry=registry,
        )
        self.score = Gauge(
            "aiops_last_anomaly_score",
            "Most recent normalised score, by service",
            ["service"],
            registry=registry,
        )



# Window-rate signals; distorted for a rate window after a counter reset.
RATE_FEATURES = ("tps", "error_ratio", "latency_p50_seconds", "latency_p99_seconds")


def neutralise_rates(sample: MetricSample, model: Any) -> MetricSample:
    """The sample with its rate signals replaced by the baseline for its hour and weekday."""
    at = sample.timestamp
    update: dict[str, float] = {}
    for feature in RATE_FEATURES:
        stats = model.baseline.stats_for(feature, at.hour, at.weekday())
        if stats is None:
            continue
        # error_ratio is computed from measured_error_ratio on MetricSample.
        key = "measured_error_ratio" if feature == "error_ratio" else feature
        value = max(0.0, float(stats.mean))
        update[key] = min(value, 1.0) if feature == "error_ratio" else value
    return sample.model_copy(update=update)

class AnomalyDetector:
    """Stream consumer: metric events in, anomaly events out."""

    def __init__(
        self,
        database: Database,
        bus: EventBus,
        scorer: Scorer,
        metrics: DetectorMetrics,
        *,
        consumer_name: str = "detector-1",
        batch_size: int = 50,
        block_ms: int = 5_000,
    ) -> None:
        self._db = database
        self._bus = bus
        # Public: the /score endpoint needs to reach it to answer "what would you say about
        # this sample", which is the only way to probe the model's judgement directly.
        self.scorer = scorer
        self._metrics = metrics
        self._consumer = consumer_name
        self._batch = batch_size
        self._block_ms = block_ms
        self._models: dict[str, ServiceModel] = {}
        self._last_anomaly_emitted: dict[str, float] = {}
        # Consecutive anomalous samples per service (see PERSISTENCE_SAMPLES).
        self._unusual_streak: dict[str, int] = {}
        self.status = DetectorStatus(threshold=scorer.threshold)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- models ------------------------------------------------------------
    def set_models(self, models: dict[str, ServiceModel]) -> None:
        """Swap in a new set of models, atomically from the loop's point of view.

        Rebinding the dict rather than mutating it means an in-flight score always sees one
        coherent generation, never a half-replaced mixture after a retrain.
        """
        self._models = models
        # Prune services that now have a model. `unknown_services` is a *current* complaint,
        # not a history: on first boot the detector legitimately sees every service before any
        # model exists, and leaving those entries in place made /status report all five as
        # unmonitored for ever, even immediately after a successful train.
        self.status.unknown_services = [
            service for service in self.status.unknown_services if service not in models
        ]
        self.status.models_loaded = len(models)
        self.status.model_version = next(
            (m.version for m in models.values()), self.status.model_version
        )
        self._metrics.models.set(len(models))
        log.info("detector.models_set", count=len(models), services=sorted(models))

    @property
    def models(self) -> dict[str, ServiceModel]:
        return self._models

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self._bus.ensure_group(Stream.METRICS, CONSUMER_GROUP)
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="anomaly-detector-loop")
            self.status.running = True
            log.info("detector.started", group=CONSUMER_GROUP, consumer=self._consumer)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.running = False
        log.info(
            "detector.stopped",
            scored=self.status.samples_scored,
            anomalies=self.status.anomalies_detected,
        )

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed batch must not end the loop. Redis can be restarting, the database
                # can be failing over; record it and read again.
                self._metrics.errors.labels("consume").inc()
                log.exception("detector.consume_failed")
                await asyncio.sleep(1.0)

    async def consume_once(self) -> int:
        """Read one batch and handle it. Returns the number of samples scored."""
        messages = await self._bus.read(
            Stream.METRICS,
            CONSUMER_GROUP,
            self._consumer,
            count=self._batch,
            block_ms=self._block_ms,
        )
        handled = 0
        for message in messages:
            try:
                if await self._handle(message):
                    handled += 1
            except Exception as exc:  # noqa: BLE001 - one bad event must not stall the group
                self._metrics.errors.labels("handle").inc()
                self.status.last_error = str(exc)
                log.warning("detector.event_failed", error=str(exc), exc_info=True)
            finally:
                # Acknowledged even on failure. A message that cannot be parsed will never
                # parse, so leaving it pending would block the group for ever - the classic
                # poison-message stall. The failure is counted and logged instead.
                await self._bus.ack(Stream.METRICS, CONSUMER_GROUP, message.message_id)
        return handled

    # -- one event ---------------------------------------------------------
    async def _handle(self, message: Event) -> bool:
        started = time.perf_counter()
        payload = message.data if isinstance(message.data, dict) else {}
        if not payload:
            return False

        sample = MetricSample.model_validate(payload)
        model = self._models.get(sample.service)
        if model is None:
            # Recorded rather than ignored. A service with no model is invisible to the
            # platform, which is exactly the blind spot worth surfacing - it happens whenever
            # a new service appears and nobody retrains.
            self._metrics.no_model.labels(sample.service).inc()
            if sample.service not in self.status.unknown_services:
                self.status.unknown_services.append(sample.service)
                log.warning(
                    "detector.no_model",
                    monitored_service=sample.service,
                    detail="no trained model; samples are not being scored. POST /train",
                )
            return False

        # The service (or a dependency) restarted within the last rate window: its *rate*
        # signals are distorted for five minutes (see metrics_agent RESET_QUERY) - a remediated
        # service still shows the fault's errors, and callers still count calls that failed
        # during the restart. Its gauges (queue, memory, CPU, connections) describe the process
        # running now. So the sample is scored on its gauges, with the rates set to baseline.
        #
        # Skipping the sample outright, as this used to, blinded detection for six minutes
        # after every Kubernetes rollout - a new pod resets its counters - which is exactly
        # when a bad release shows: queue 60 and 2% errors went unscored.
        if (
            sample.labels.get("restart_in_rate_window") == "1"
            or sample.labels.get("dependency_restart_in_rate_window") == "1"
        ):
            sample = neutralise_rates(sample, model)

        result = self.scorer.score(sample, model)
        self._metrics.scored.labels(sample.service).inc()
        self._metrics.score.labels(sample.service).set(result.score)
        self.status.samples_scored += 1
        self.status.last_sample_at = sample.timestamp.isoformat()

        peak_sigma = (
            max(result.anomaly.feature_deviations.values(), default=0.0)
            if result.anomaly is not None
            else 0.0
        )
        if not self._passes_persistence(
            sample.service, result.anomaly is not None, result.score, peak_sigma=peak_sigma
        ):
            if result.anomaly is not None:
                log.debug(
                    "anomaly.awaiting_persistence",
                    monitored_service=sample.service,
                    score=round(result.score, 4),
                )
            return True
        assert result.anomaly is not None

        now = time.monotonic()
        if self._in_anomaly_cooldown(sample.service, now):
            log.debug(
                "anomaly.cooldown",
                monitored_service=sample.service,
                cooldown_seconds=ANOMALY_COOLDOWN_SECONDS,
            )
            return True

        await self._persist(result.anomaly)
        await self._publish(result.anomaly)
        self._last_anomaly_emitted[sample.service] = time.monotonic()

        self.status.anomalies_detected += 1
        if result.anomaly.is_actionable:
            self.status.anomalies_actionable += 1
        self.status.last_anomaly_at = result.anomaly.detected_at.isoformat()
        self._metrics.anomalies.labels(
            sample.service, str(result.anomaly.kind), str(result.anomaly.severity)
        ).inc()
        self._metrics.latency.observe(time.perf_counter() - started)

        log.info(
            "anomaly.detected",
            monitored_service=sample.service,
            kind=str(result.anomaly.kind),
            severity=str(result.anomaly.severity),
            score=round(result.score, 4),
            # Which of the two detectors decided. Worth having in the log: a run of
            # baseline-decided anomalies with the forest silent is the signature of faults
            # whose magnitudes fall outside the training range, which is expected, whereas
            # forest-decided ones are unusual combinations of ordinary values.
            decided_by=result.decided_by,
            forest_score=round(result.forest_score, 4),
            deviation_score=round(result.deviation_score, 4),
            actionable=result.anomaly.is_actionable,
            contributing=result.anomaly.contributing_features[:3],
        )
        return True

    def _passes_persistence(
        self, service: str, anomalous: bool, score: float, *, peak_sigma: float = 0.0
    ) -> bool:
        """Track the service's run of anomalous samples; True when one should be emitted."""
        if not anomalous:
            self._unusual_streak[service] = 0
            return False
        streak = self._unusual_streak.get(service, 0) + 1
        self._unusual_streak[service] = streak
        if streak >= PERSISTENCE_SAMPLES:
            return True
        return score >= IMMEDIATE_SCORE and peak_sigma >= IMMEDIATE_SIGMA

    def _in_anomaly_cooldown(self, service: str, now: float) -> bool:
        last_emitted = self._last_anomaly_emitted.get(service)
        return last_emitted is not None and now - last_emitted < ANOMALY_COOLDOWN_SECONDS

    async def _persist(self, anomaly: AnomalyEvent) -> None:
        await self._db.execute(
            INSERT_ANOMALY,
            {
                "anomaly_id": anomaly.anomaly_id,
                "detected_at": anomaly.detected_at,
                "window_start": anomaly.window_start,
                "service": anomaly.service,
                "platform": anomaly.platform,
                "kind": str(anomaly.kind),
                "severity": str(anomaly.severity),
                "score": anomaly.score,
                "raw_score": anomaly.raw_score,
                "threshold": anomaly.threshold,
                # A text[] column, so the list is passed through rather than serialised.
                "contributing_features": anomaly.contributing_features,
                "feature_snapshot": json.dumps(anomaly.feature_snapshot),
                "feature_deviations": json.dumps(anomaly.feature_deviations),
                "model_name": anomaly.model_name,
                "model_version": anomaly.model_version,
                "description": anomaly.description,
                "incident_id": anomaly.incident_id,
            },
        )

    async def _publish(self, anomaly: AnomalyEvent) -> None:
        # Database first, then the stream. The RCA engine will read an anomaly from the stream
        # and immediately query it by id; publishing first opens a window where that lookup
        # finds nothing.
        await self._bus.publish(Stream.ANOMALIES, anomaly, event_type="AnomalyEvent")

    def snapshot(self) -> dict[str, Any]:
        payload = self.status.as_dict()
        payload["models"] = {
            service: {
                "version": m.version,
                "training_rows": m.training_rows,
                "calibration_scale": round(m.scale, 5),
                "data_sources": list(m.data_sources),
            }
            for service, m in sorted(self._models.items())
        }
        payload["generated_at"] = utc_now().isoformat()
        return payload
