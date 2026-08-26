"""Generate realistic demo data so the dashboard can be built and reviewed now.

The UI is being built before the services that feed it, so this script produces the
kind of data those services will eventually emit: a week of Mojaloop telemetry with a
believable daily and weekly rhythm, five scripted failure scenarios, and the anomalies,
incidents, remediation actions, forecasts and chaos runs that follow from them.

It is not a toy. The traffic model is the same shape the module 2 simulator will
generate, and the scenarios are the exact cases modules 4 and 7 must handle:

  1. memory leak      -> single-service saturation, restart playbook
  2. bad deploy       -> change-correlated error burst, rollback playbook
  3. traffic surge    -> external event, pre-emptive scale-out
  4. cascade          -> upstream cause, several symptomatic services, ONE incident
  5. chaos experiment -> injected CPU exhaustion with a detect/remediate scorecard

Usage:
    python scripts/seed_demo_data.py                 # 7 days, then stop
    python scripts/seed_demo_data.py --days 3
    python scripts/seed_demo_data.py --follow        # keep appending live samples
    python scripts/seed_demo_data.py --clear         # remove seeded data and exit

`--follow` matters for demos: the health grid marks a service stale after two minutes
without samples, so without a live tail the dashboard goes grey shortly after seeding.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Allow running as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import (
    ActionStatus,
    AnomalyKind,
    ChaosExperiment,
    MojaloopService,
    PlaybookName,
    RootCauseCategory,
    Severity,
    Stream,
)
from aiops_common.db import Database
from aiops_common.log import configure_logging, get_logger
from aiops_common.schemas import MetricSample, new_id, utc_now

log = get_logger("seed")

# Marks every row this script creates, so --clear can remove exactly those and nothing
# else. Real agents will use a pod name or container id here.
SEED_INSTANCE_PREFIX = "seed-"

SAMPLE_INTERVAL_SECONDS = 60
LIVE_INTERVAL_SECONDS = 15


# ---------------------------------------------------------------------------
# Service traffic profiles
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ServiceProfile:
    """How one Mojaloop microservice behaves under load."""

    name: str
    peak_tps: float
    base_latency_seconds: float
    # Latency grows super-linearly as the service approaches saturation.
    latency_load_factor: float
    baseline_error_ratio: float
    tps_per_replica: float
    replicas: int
    # Only the ledger settles funds, so only it reports settlement lag.
    reports_settlement: bool = False


PROFILES: tuple[ServiceProfile, ...] = (
    ServiceProfile(
        name=MojaloopService.ML_API_ADAPTER,
        peak_tps=180.0,
        base_latency_seconds=0.035,
        latency_load_factor=0.45,
        baseline_error_ratio=0.0015,
        tps_per_replica=60.0,
        replicas=3,
    ),
    ServiceProfile(
        name=MojaloopService.QUOTING_SERVICE,
        peak_tps=160.0,
        base_latency_seconds=0.070,
        latency_load_factor=0.90,
        baseline_error_ratio=0.0025,
        tps_per_replica=55.0,
        replicas=3,
    ),
    ServiceProfile(
        name=MojaloopService.CENTRAL_LEDGER,
        peak_tps=175.0,
        base_latency_seconds=0.090,
        latency_load_factor=1.30,
        baseline_error_ratio=0.0020,
        tps_per_replica=50.0,
        replicas=4,
        reports_settlement=True,
    ),
    ServiceProfile(
        name=MojaloopService.ACCOUNT_LOOKUP,
        peak_tps=110.0,
        base_latency_seconds=0.028,
        latency_load_factor=0.35,
        baseline_error_ratio=0.0010,
        tps_per_replica=45.0,
        replicas=2,
    ),
    ServiceProfile(
        name=MojaloopService.TRANSACTION_REQUEST,
        peak_tps=65.0,
        base_latency_seconds=0.050,
        latency_load_factor=0.50,
        baseline_error_ratio=0.0030,
        tps_per_replica=35.0,
        replicas=2,
    ),
)

PROFILE_BY_NAME = {profile.name: profile for profile in PROFILES}


def daily_shape(moment: datetime) -> float:
    """Traffic multiplier for the time of day, in the range roughly 0.25 to 1.0.

    Retail payments have two humps: a late-morning peak and a larger evening one. A
    plain sine wave would be easier but would teach the forecasting model the wrong
    shape, and a demo that only works on a sine wave proves nothing.
    """
    hour = moment.hour + moment.minute / 60.0
    morning = 0.42 * math.exp(-((hour - 11.0) ** 2) / 7.0)
    evening = 0.50 * math.exp(-((hour - 19.5) ** 2) / 5.5)
    overnight = 0.22
    return overnight + morning + evening


def weekly_shape(moment: datetime) -> float:
    """Weekend traffic is materially lower; Monday is the busiest day."""
    return {0: 1.05, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.08, 5: 0.68, 6: 0.52}[moment.weekday()]


# ---------------------------------------------------------------------------
# Fault scenarios
# ---------------------------------------------------------------------------
@dataclass
class Fault:
    """A time-bounded degradation applied on top of the baseline traffic model."""

    service: str
    start: datetime
    duration: timedelta
    kind: str
    # Multipliers and offsets applied at full intensity.
    latency_multiplier: float = 1.0
    error_ratio_add: float = 0.0
    tps_multiplier: float = 1.0
    memory_add: float = 0.0
    cpu_add: float = 0.0
    queue_add: float = 0.0
    settlement_lag_add: float = 0.0
    # "ramp" builds gradually (a leak), "spike" is immediate (a crash or surge).
    profile: str = "spike"

    @property
    def end(self) -> datetime:
        return self.start + self.duration

    def intensity(self, moment: datetime) -> float:
        """0.0 outside the window, otherwise how far the fault has developed."""
        if not (self.start <= moment <= self.end):
            return 0.0
        elapsed = (moment - self.start).total_seconds()
        total = max(self.duration.total_seconds(), 1.0)
        fraction = elapsed / total
        if self.profile == "ramp":
            # Accelerating growth, like a memory leak under increasing traffic.
            return fraction**1.6
        # Fast onset, slow recovery: what remediation actually looks like.
        return min(1.0, fraction / 0.15) if fraction < 0.15 else 1.0 - (fraction - 0.15) * 0.35


@dataclass
class Scenario:
    """A fault plus the records the AIOps pipeline would produce in response."""

    name: str
    faults: list[Fault]
    anomaly_service: str
    anomaly_kind: str
    severity: str
    incident_title: str
    incident_summary: str
    cause_category: str
    cause_target: str
    cause_confidence: float
    evidence: list[str]
    playbook: str
    action_status: str
    action_reason: str
    affected_services: list[str] = field(default_factory=list)
    deployment: dict[str, Any] | None = None
    chaos: dict[str, Any] | None = None
    detect_delay_seconds: int = 45
    remediate_delay_seconds: int = 150


def build_scenarios(now: datetime) -> list[Scenario]:
    """The five scripted incidents, positioned relative to `now`."""
    ledger = str(MojaloopService.CENTRAL_LEDGER)
    quoting = str(MojaloopService.QUOTING_SERVICE)
    adapter = str(MojaloopService.ML_API_ADAPTER)
    lookup = str(MojaloopService.ACCOUNT_LOOKUP)

    # --- 1. memory leak on the ledger, ending in a restart -----------------
    leak_start = now - timedelta(hours=30)
    leak = Scenario(
        name="memory-leak",
        faults=[
            Fault(
                service=ledger,
                start=leak_start,
                duration=timedelta(minutes=52),
                kind="memory",
                profile="ramp",
                memory_add=0.55,
                latency_multiplier=2.4,
                queue_add=140.0,
                error_ratio_add=0.012,
                settlement_lag_add=95.0,
            )
        ],
        anomaly_service=ledger,
        anomaly_kind=AnomalyKind.MEMORY_PRESSURE,
        severity=Severity.CRITICAL,
        incident_title="central-ledger approaching OOM with settlement falling behind",
        incident_summary=(
            "Memory on central-ledger climbed from 38% to 93% over 50 minutes while "
            "traffic stayed within its normal band, so this is consumption growth rather "
            "than load. Queue depth and settlement lag rose with it, indicating the "
            "service was slowing down as it approached the limit. No deployment or "
            "config change preceded the climb, which points at the running process "
            "itself. The restart playbook was selected."
        ),
        cause_category=RootCauseCategory.SERVICE,
        cause_target=ledger,
        cause_confidence=0.91,
        evidence=[
            "memory_utilisation_ratio 38% -> 93% over 50 minutes",
            "tps within 8% of the 7-day baseline for this hour, so not load driven",
            "queue_depth rose from 3 to 142 in the same window",
            "settlement_lag_seconds exceeded the 120s critical threshold",
            "no deployment recorded for central-ledger in the preceding 6 hours",
        ],
        playbook=PlaybookName.RESTART_ON_OOM,
        action_status=ActionStatus.SUCCEEDED,
        action_reason="memory_utilisation_ratio 0.93 above the 0.92 critical threshold",
        affected_services=[ledger],
        remediate_delay_seconds=2_700,
    )

    # --- 2. bad deploy on quoting-service, rolled back --------------------
    deploy_at = now - timedelta(hours=14)
    bad_deploy = Scenario(
        name="bad-deploy",
        faults=[
            Fault(
                service=quoting,
                start=deploy_at + timedelta(minutes=11),
                duration=timedelta(minutes=34),
                kind="errors",
                error_ratio_add=0.145,
                latency_multiplier=3.1,
                cpu_add=0.20,
            )
        ],
        anomaly_service=quoting,
        anomaly_kind=AnomalyKind.ERROR_BURST,
        severity=Severity.CRITICAL,
        incident_title="quoting-service error rate jumped to 14% after v2.4.1 deploy",
        incident_summary=(
            "Errors on quoting-service went from 0.3% to 14.6% eleven minutes after "
            "v2.4.1 was deployed, with p99 latency tripling at the same moment. Traffic "
            "was flat across the transition, and no upstream dependency degraded first, "
            "so load and cascade are both ruled out. The deployment is the only change "
            "in the window. Rolling back to v2.4.0 was the selected remediation."
        ),
        cause_category=RootCauseCategory.CHANGE,
        cause_target="quoting-service v2.4.0 -> v2.4.1",
        cause_confidence=0.94,
        evidence=[
            "deployment of v2.4.1 recorded 11 minutes before onset",
            "error_ratio 0.003 -> 0.146, a 48x increase",
            "latency_p99_seconds 0.31 -> 0.96",
            "tps unchanged within 3%, so not load related",
            "central-ledger and account-lookup-service stayed healthy throughout",
        ],
        playbook=PlaybookName.ROLLBACK_BAD_DEPLOY,
        action_status=ActionStatus.SUCCEEDED,
        action_reason="error_ratio 0.146 correlated with deploy 11 minutes earlier",
        affected_services=[quoting],
        deployment={
            "service": quoting,
            "occurred_at": deploy_at,
            "from_version": "v2.4.0",
            "to_version": "v2.4.1",
            "change_type": "deploy",
            "triggered_by": "ci-pipeline",
            "notes": "quote expiry refactor",
        },
        detect_delay_seconds=30,
        remediate_delay_seconds=1_980,
    )

    # --- 3. platform-wide traffic surge, scaled out pre-emptively ---------
    surge_start = now - timedelta(hours=6)
    surge = Scenario(
        name="traffic-surge",
        faults=[
            Fault(
                service=profile.name,
                start=surge_start,
                duration=timedelta(minutes=38),
                kind="surge",
                tps_multiplier=2.6,
                latency_multiplier=1.5,
                cpu_add=0.28,
                queue_add=34.0,
            )
            for profile in PROFILES
        ],
        anomaly_service=adapter,
        anomaly_kind=AnomalyKind.TRAFFIC_SURGE,
        severity=Severity.WARNING,
        incident_title="Platform-wide traffic surge to 2.6x baseline",
        incident_summary=(
            "Every service saw traffic rise to roughly 2.6x the baseline for this hour "
            "simultaneously. Error rates stayed flat and latency rose only in proportion "
            "to load, which is the signature of genuine demand rather than a fault. "
            "Because the increase was uniform across the entry point and all downstream "
            "services, the origin is external to the platform. The forecast for the next "
            "hour projected sustained load above current capacity, so ml-api-adapter was "
            "scaled out from 3 to 6 replicas ahead of the peak."
        ),
        cause_category=RootCauseCategory.EVENT,
        cause_target="external traffic surge",
        cause_confidence=0.86,
        evidence=[
            "tps 2.6x the 7-day baseline for this hour of the week",
            "increase simultaneous across all 5 services, no propagation delay",
            "error_ratio unchanged at 0.002, so services are coping",
            "latency growth proportional to load, consistent with queueing not failure",
            "1-hour forecast projected 445 TPS against 180 TPS of provisioned capacity",
        ],
        playbook=PlaybookName.SCALE_OUT_PREEMPTIVE,
        action_status=ActionStatus.SUCCEEDED,
        action_reason="forecast peak 445 TPS exceeds capacity of 3 replicas (180 TPS)",
        affected_services=[profile.name for profile in PROFILES],
        detect_delay_seconds=60,
        remediate_delay_seconds=210,
    )

    # --- 4. cascade from the ledger, contained by a circuit breaker -------
    cascade_start = now - timedelta(hours=2, minutes=30)
    cascade = Scenario(
        name="cascade",
        faults=[
            Fault(
                service=ledger,
                start=cascade_start,
                duration=timedelta(minutes=26),
                kind="latency",
                latency_multiplier=6.5,
                error_ratio_add=0.055,
                queue_add=190.0,
                settlement_lag_add=70.0,
            ),
            # Downstream services degrade a little later: that lag is the evidence RCA
            # uses to identify the ledger as the origin rather than a victim.
            Fault(
                service=quoting,
                start=cascade_start + timedelta(minutes=2),
                duration=timedelta(minutes=23),
                kind="latency",
                latency_multiplier=3.8,
                error_ratio_add=0.031,
                queue_add=95.0,
            ),
            Fault(
                service=adapter,
                start=cascade_start + timedelta(minutes=4),
                duration=timedelta(minutes=20),
                kind="latency",
                latency_multiplier=2.9,
                error_ratio_add=0.022,
                queue_add=60.0,
            ),
        ],
        anomaly_service=ledger,
        anomaly_kind=AnomalyKind.LATENCY_SPIKE,
        severity=Severity.CRITICAL,
        incident_title="central-ledger latency spike cascading to quoting and the API adapter",
        incident_summary=(
            "central-ledger p99 latency rose 6.5x, followed two minutes later by "
            "quoting-service and four minutes later by ml-api-adapter. The dependency "
            "graph has both of those calling central-ledger, and the onset order matches "
            "that direction, so they are symptoms rather than independent faults. Three "
            "services degraded but this is one incident with one cause. A circuit breaker "
            "was opened on the quoting to ledger path to stop the queue backlog spreading "
            "further upstream."
        ),
        cause_category=RootCauseCategory.SERVICE,
        cause_target=ledger,
        cause_confidence=0.88,
        evidence=[
            "central-ledger degraded first, at 2 and 4 minutes ahead of the others",
            "onset order matches the dependency graph direction (adapter -> quoting -> ledger)",
            "central-ledger queue_depth peaked at 193 while downstream queues stayed lower",
            "no deployment recorded for any affected service in the preceding 12 hours",
            "account-lookup-service, which does not call the ledger, stayed healthy",
        ],
        playbook=PlaybookName.CIRCUIT_BREAK_CASCADE,
        action_status=ActionStatus.SUCCEEDED,
        action_reason="3 services degraded with central-ledger identified as the origin",
        affected_services=[ledger, quoting, adapter],
        detect_delay_seconds=45,
        remediate_delay_seconds=420,
    )

    # --- 5. chaos experiment on account-lookup ----------------------------
    chaos_start = now - timedelta(minutes=70)
    chaos = Scenario(
        name="chaos-cpu",
        faults=[
            Fault(
                service=lookup,
                start=chaos_start,
                duration=timedelta(minutes=9),
                kind="cpu",
                cpu_add=0.62,
                latency_multiplier=4.2,
                error_ratio_add=0.018,
                queue_add=48.0,
            )
        ],
        anomaly_service=lookup,
        anomaly_kind=AnomalyKind.CPU_SATURATION,
        severity=Severity.WARNING,
        incident_title="account-lookup-service CPU saturation (chaos experiment)",
        incident_summary=(
            "Injected CPU exhaustion drove account-lookup-service to 94% utilisation with "
            "p99 latency rising 4.2x. Detection took 38 seconds and remediation completed "
            "at 2 minutes 41 seconds. This was a deliberate experiment, correlated to the "
            "active chaos run, so the cause is recorded as an event rather than a fault in "
            "the service."
        ),
        cause_category=RootCauseCategory.EVENT,
        cause_target="chaos experiment: cpu_hog",
        cause_confidence=0.97,
        evidence=[
            "active chaos run targeting account-lookup-service at onset",
            "cpu_utilisation_ratio 0.19 -> 0.94",
            "latency_p99_seconds 0.09 -> 0.38",
            "tps unchanged, so not load driven",
        ],
        playbook=PlaybookName.RESTART_ON_OOM,
        action_status=ActionStatus.SKIPPED_DRY_RUN,
        action_reason="cpu_utilisation_ratio 0.94 above the 0.90 critical threshold",
        affected_services=[lookup],
        chaos={
            "experiment": ChaosExperiment.CPU_HOG,
            "target_service": lookup,
            "duration_seconds": 540,
            "parameters": {"cores": 2, "load_percent": 95},
        },
        detect_delay_seconds=38,
        remediate_delay_seconds=161,
    )

    return [leak, bad_deploy, surge, cascade, chaos]


# ---------------------------------------------------------------------------
# Metric generation
# ---------------------------------------------------------------------------
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class MetricGenerator:
    """Produces one `service_metrics` row per service per timestamp."""

    def __init__(self, faults: list[Fault], seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self._faults: dict[str, list[Fault]] = {}
        for fault in faults:
            self._faults.setdefault(fault.service, []).append(fault)
        # Memory follows a slow sawtooth per service (gradual growth, reset on deploy or
        # restart) rather than a flat line, so the baseline is not trivially predictable.
        self._memory_phase = {profile.name: self._rng.uniform(0, 1) for profile in PROFILES}

    def row(self, profile: ServiceProfile, moment: datetime) -> dict[str, Any]:
        load = daily_shape(moment) * weekly_shape(moment)
        jitter = 1.0 + self._rng.gauss(0.0, 0.045)
        tps = max(0.5, profile.peak_tps * load * jitter)

        latency_mult = 1.0
        error_add = 0.0
        memory_add = 0.0
        cpu_add = 0.0
        queue_add = 0.0
        settlement_add = 0.0
        tps_mult = 1.0

        for fault in self._faults.get(profile.name, ()):
            intensity = fault.intensity(moment)
            if intensity <= 0:
                continue
            latency_mult *= 1.0 + (fault.latency_multiplier - 1.0) * intensity
            tps_mult *= 1.0 + (fault.tps_multiplier - 1.0) * intensity
            error_add += fault.error_ratio_add * intensity
            memory_add += fault.memory_add * intensity
            cpu_add += fault.cpu_add * intensity
            queue_add += fault.queue_add * intensity
            settlement_add += fault.settlement_lag_add * intensity

        tps *= tps_mult
        # Load ratio against provisioned capacity drives saturation effects.
        capacity = profile.tps_per_replica * profile.replicas
        load_ratio = tps / capacity

        latency_p50 = (
            profile.base_latency_seconds
            * (1.0 + profile.latency_load_factor * load_ratio**2)
            * latency_mult
            * (1.0 + self._rng.gauss(0.0, 0.05))
        )
        latency_p50 = max(0.001, latency_p50)
        latency_p95 = latency_p50 * self._rng.uniform(2.4, 3.2)
        latency_p99 = latency_p95 * self._rng.uniform(1.35, 1.8)

        error_ratio = _clamp(
            profile.baseline_error_ratio
            + max(0.0, load_ratio - 0.85) * 0.05
            + error_add
            + abs(self._rng.gauss(0.0, 0.0004))
        )

        request_count = max(1, int(tps * SAMPLE_INTERVAL_SECONDS))
        error_count = int(request_count * error_ratio)

        cpu = _clamp(0.12 + 0.62 * load_ratio + cpu_add + self._rng.gauss(0.0, 0.02), 0.0, 1.0)

        # Sawtooth memory: 12-hour cycle, reset on restart.
        phase = (self._memory_phase[profile.name] + (moment.timestamp() / 43_200.0)) % 1.0
        memory = _clamp(
            0.34 + 0.22 * phase + 0.10 * load_ratio + memory_add + self._rng.gauss(0.0, 0.012),
            0.0,
            1.0,
        )

        queue_depth = max(
            0.0,
            max(0.0, load_ratio - 0.78) * 220.0 + queue_add + abs(self._rng.gauss(0.0, 1.2)),
        )

        return {
            "timestamp": moment,
            "service": profile.name,
            "instance": f"{SEED_INSTANCE_PREFIX}{profile.name}-0",
            "platform": "mojaloop",
            "tps": round(tps, 3),
            "request_count": request_count,
            "error_count": error_count,
            "error_ratio": round(error_ratio, 6),
            "latency_p50_seconds": round(latency_p50, 5),
            "latency_p95_seconds": round(latency_p95, 5),
            "latency_p99_seconds": round(latency_p99, 5),
            "cpu_utilisation_ratio": round(cpu, 4),
            "memory_utilisation_ratio": round(memory, 4),
            "queue_depth": round(queue_depth, 2),
            "active_connections": round(max(1.0, tps * self._rng.uniform(0.7, 1.3)), 1),
            "settlement_lag_seconds": round(
                (self._rng.uniform(0.5, 3.5) + settlement_add)
                if profile.reports_settlement
                else 0.0,
                2,
            ),
            "quote_latency_seconds": round(
                latency_p50 * self._rng.uniform(0.8, 1.2)
                if profile.name == MojaloopService.QUOTING_SERVICE
                else 0.0,
                5,
            ),
            "verification_latency_seconds": 0.0,
            "replicas": profile.replicas,
        }


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
METRIC_COLUMNS = (
    "timestamp service instance platform tps request_count error_count error_ratio "
    "latency_p50_seconds latency_p95_seconds latency_p99_seconds "
    "cpu_utilisation_ratio memory_utilisation_ratio queue_depth active_connections "
    "settlement_lag_seconds quote_latency_seconds verification_latency_seconds replicas"
).split()

INSERT_METRICS = f"""
INSERT INTO service_metrics ({", ".join(f'"{c}"' for c in METRIC_COLUMNS)})
VALUES ({", ".join(f":{c}" for c in METRIC_COLUMNS)})
ON CONFLICT ("timestamp", service, instance) DO NOTHING
"""

INSERT_ANOMALY = """
INSERT INTO anomalies (
    anomaly_id, detected_at, window_start, service, platform, kind, severity,
    score, raw_score, threshold, contributing_features, feature_snapshot,
    feature_deviations, model_name, model_version, description, incident_id
) VALUES (
    :anomaly_id, :detected_at, :window_start, :service, :platform, :kind, :severity,
    :score, :raw_score, :threshold, :contributing_features, CAST(:feature_snapshot AS jsonb),
    CAST(:feature_deviations AS jsonb), :model_name, :model_version, :description, :incident_id
) ON CONFLICT (anomaly_id) DO NOTHING
"""

INSERT_INCIDENT = """
INSERT INTO incidents (
    incident_id, opened_at, closed_at, status, severity, platform, title,
    affected_services, anomaly_ids, hypotheses, action_ids, ai_summary
) VALUES (
    :incident_id, :opened_at, :closed_at, :status, :severity, :platform, :title,
    :affected_services, :anomaly_ids, CAST(:hypotheses AS jsonb), :action_ids, :ai_summary
) ON CONFLICT (incident_id) DO NOTHING
"""

INSERT_ACTION = """
INSERT INTO healing_actions (
    action_id, created_at, started_at, finished_at, playbook, target_service,
    target_instance, platform, incident_id, anomaly_id, trigger_reason, confidence,
    status, dry_run, parameters, result_message, before_snapshot, after_snapshot
) VALUES (
    :action_id, :created_at, :started_at, :finished_at, :playbook, :target_service,
    :target_instance, :platform, :incident_id, :anomaly_id, :trigger_reason, :confidence,
    :status, :dry_run, CAST(:parameters AS jsonb), :result_message,
    CAST(:before_snapshot AS jsonb), CAST(:after_snapshot AS jsonb)
) ON CONFLICT (action_id) DO NOTHING
"""

INSERT_PREDICTION = """
INSERT INTO traffic_predictions (
    prediction_id, created_at, service, platform, horizon_hours, current_tps,
    peak_predicted_tps, peak_at, points, current_replicas, recommended_replicas,
    tps_per_replica, scaling_action, model_name, model_version, backtest_mape
) VALUES (
    :prediction_id, :created_at, :service, :platform, :horizon_hours, :current_tps,
    :peak_predicted_tps, :peak_at, CAST(:points AS jsonb), :current_replicas,
    :recommended_replicas, :tps_per_replica, :scaling_action, :model_name,
    :model_version, :backtest_mape
) ON CONFLICT (prediction_id) DO NOTHING
"""

INSERT_DEPLOYMENT = """
INSERT INTO deployments (
    deployment_id, occurred_at, service, platform, from_version, to_version,
    change_type, triggered_by, notes
) VALUES (
    :deployment_id, :occurred_at, :service, :platform, :from_version, :to_version,
    :change_type, :triggered_by, :notes
) ON CONFLICT (deployment_id) DO NOTHING
"""

INSERT_CHAOS = """
INSERT INTO chaos_runs (
    run_id, started_at, ended_at, experiment, target_service, platform,
    duration_seconds, parameters, status, detected_at, detected_anomaly_id,
    incident_id, remediated_at, notes
) VALUES (
    :run_id, :started_at, :ended_at, :experiment, :target_service, :platform,
    :duration_seconds, CAST(:parameters AS jsonb), :status, :detected_at,
    :detected_anomaly_id, :incident_id, :remediated_at, :notes
) ON CONFLICT (run_id) DO NOTHING
"""

INSERT_MODEL_RUN = """
INSERT INTO model_runs (
    run_id, created_at, model_name, model_version, service, training_rows,
    duration_seconds, metrics, artifact_path, notes
) VALUES (
    :run_id, :created_at, :model_name, :model_version, :service, :training_rows,
    :duration_seconds, CAST(:metrics AS jsonb), :artifact_path, :notes
) ON CONFLICT (run_id) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
async def clear(db: Database) -> None:
    """Remove only the rows this script created."""
    deleted = await db.execute(
        "DELETE FROM service_metrics WHERE instance LIKE :prefix",
        {"prefix": f"{SEED_INSTANCE_PREFIX}%"},
    )
    # Child rows first: healing_actions references incidents.
    for table in ("healing_actions", "anomalies", "chaos_runs", "incidents"):
        await db.execute(f"DELETE FROM {table} WHERE platform = 'mojaloop'")
    for table in ("traffic_predictions", "deployments"):
        await db.execute(f"DELETE FROM {table} WHERE platform = 'mojaloop'")
    await db.execute("DELETE FROM model_runs WHERE notes LIKE 'seeded%'")
    log.info("seed.cleared", metric_rows=deleted)


async def seed_metrics(
    db: Database, generator: MetricGenerator, start: datetime, end: datetime
) -> int:
    """Write the metric history in day-sized batches."""
    total = 0
    step = timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    batch: list[dict[str, Any]] = []
    moment = start

    while moment <= end:
        for profile in PROFILES:
            batch.append(generator.row(profile, moment))
        moment += step
        if len(batch) >= 5_000:
            total += await db.execute_many(INSERT_METRICS, batch)
            log.info("seed.metrics_progress", rows=total, up_to=moment.isoformat())
            batch = []

    if batch:
        total += await db.execute_many(INSERT_METRICS, batch)
    return total


async def seed_scenarios(db: Database, scenarios: list[Scenario], now: datetime) -> None:
    """Write the anomalies, incidents, actions, deployments and chaos runs."""
    for scenario in scenarios:
        primary = scenario.faults[0]
        detected_at = primary.start + timedelta(seconds=scenario.detect_delay_seconds)
        incident_id = new_id("inc")
        action_id = new_id("act")
        anomaly_ids: list[str] = []

        if scenario.deployment:
            await db.execute(
                INSERT_DEPLOYMENT,
                {
                    "deployment_id": new_id("dep"),
                    "platform": "mojaloop",
                    **scenario.deployment,
                },
            )

        # One anomaly per affected service, so the cascade scenario genuinely produces
        # three anomalies that RCA has to collapse into a single incident.
        for fault in scenario.faults:
            if fault.service not in scenario.affected_services:
                continue
            is_primary = fault.service == scenario.anomaly_service
            anomaly_id = new_id("anom")
            anomaly_ids.append(anomaly_id)
            profile = PROFILE_BY_NAME[fault.service]
            snapshot = {
                "tps": round(profile.peak_tps * 0.8 * fault.tps_multiplier, 2),
                "error_ratio": round(profile.baseline_error_ratio + fault.error_ratio_add, 4),
                "latency_p99_seconds": round(
                    profile.base_latency_seconds * 4 * fault.latency_multiplier, 4
                ),
                "memory_utilisation_ratio": round(min(0.99, 0.42 + fault.memory_add), 4),
                "cpu_utilisation_ratio": round(min(0.99, 0.32 + fault.cpu_add), 4),
                "queue_depth": round(fault.queue_add + 4, 1),
                "settlement_lag_seconds": round(fault.settlement_lag_add, 1),
            }
            deviations = {
                "memory_utilisation_ratio": round(fault.memory_add * 8.5, 2),
                "latency_p99_seconds": round((fault.latency_multiplier - 1) * 2.4, 2),
                "error_ratio": round(fault.error_ratio_add * 42, 2),
                "queue_depth": round(fault.queue_add / 22.0, 2),
                "cpu_utilisation_ratio": round(fault.cpu_add * 7.0, 2),
            }
            # Rank features by how far they moved: this is what the UI shows as the
            # explanation for a score.
            ranked = sorted(deviations.items(), key=lambda item: -abs(item[1]))
            await db.execute(
                INSERT_ANOMALY,
                {
                    "anomaly_id": anomaly_id,
                    "detected_at": detected_at + timedelta(seconds=0 if is_primary else 120),
                    "window_start": fault.start,
                    "service": fault.service,
                    "platform": "mojaloop",
                    "kind": scenario.anomaly_kind if is_primary else AnomalyKind.MULTIVARIATE,
                    "severity": scenario.severity if is_primary else Severity.WARNING,
                    "score": 0.93 if is_primary else 0.71,
                    "raw_score": -0.42 if is_primary else -0.18,
                    "threshold": 0.65,
                    "contributing_features": [name for name, value in ranked if value > 0.5][
                        :4
                    ],
                    "feature_snapshot": json.dumps(snapshot),
                    "feature_deviations": json.dumps(dict(ranked)),
                    "model_name": "isolation-forest",
                    "model_version": "2026.08.1",
                    "description": (
                        f"{scenario.anomaly_kind} detected on {fault.service}"
                        if is_primary
                        else f"correlated degradation on {fault.service}"
                    ),
                    "incident_id": incident_id,
                },
            )

        resolved_at = primary.start + timedelta(seconds=scenario.remediate_delay_seconds)
        is_resolved = resolved_at < now
        await db.execute(
            INSERT_INCIDENT,
            {
                "incident_id": incident_id,
                "opened_at": detected_at,
                "closed_at": resolved_at if is_resolved else None,
                "status": "resolved" if is_resolved else "mitigating",
                "severity": scenario.severity,
                "platform": "mojaloop",
                "title": scenario.incident_title,
                "affected_services": scenario.affected_services,
                "anomaly_ids": anomaly_ids,
                "hypotheses": json.dumps(
                    [
                        {
                            "category": scenario.cause_category,
                            "target": scenario.cause_target,
                            "confidence": scenario.cause_confidence,
                            "summary": scenario.incident_title,
                            "evidence": scenario.evidence,
                            "correlated_metrics": {
                                "latency_multiplier": primary.latency_multiplier,
                                "error_ratio_delta": primary.error_ratio_add,
                                "memory_delta": primary.memory_add,
                            },
                        },
                        # A credible runner-up, so the UI has to render ranking rather
                        # than assuming a single answer.
                        {
                            "category": RootCauseCategory.EVENT,
                            "target": "load variation",
                            "confidence": round(max(0.05, scenario.cause_confidence - 0.55), 2),
                            "summary": "Traffic variation within the observed window",
                            "evidence": ["tps deviation from baseline was within normal range"],
                            "correlated_metrics": {},
                        },
                    ]
                ),
                "action_ids": [action_id],
                "ai_summary": scenario.incident_summary,
            },
        )

        executed = scenario.action_status == ActionStatus.SUCCEEDED
        await db.execute(
            INSERT_ACTION,
            {
                "action_id": action_id,
                "created_at": detected_at + timedelta(seconds=8),
                "started_at": detected_at + timedelta(seconds=12) if executed else None,
                "finished_at": resolved_at if executed and is_resolved else None,
                "playbook": scenario.playbook,
                "target_service": scenario.anomaly_service,
                "target_instance": f"{SEED_INSTANCE_PREFIX}{scenario.anomaly_service}-0",
                "platform": "mojaloop",
                "incident_id": incident_id,
                "anomaly_id": anomaly_ids[0] if anomaly_ids else None,
                "trigger_reason": scenario.action_reason,
                "confidence": scenario.cause_confidence,
                "status": scenario.action_status,
                "dry_run": not executed,
                "parameters": json.dumps(
                    {"target": scenario.anomaly_service, "playbook": scenario.playbook}
                ),
                "result_message": (
                    "Remediation completed; metrics recovered within SLO"
                    if executed
                    else "Action recorded but not executed (HEALING_ENABLED=false)"
                ),
                "before_snapshot": json.dumps(
                    {
                        "latency_p99_seconds": round(
                            PROFILE_BY_NAME[scenario.anomaly_service].base_latency_seconds
                            * 4
                            * primary.latency_multiplier,
                            4,
                        ),
                        "error_ratio": round(primary.error_ratio_add, 4),
                        "memory_utilisation_ratio": round(
                            min(0.99, 0.42 + primary.memory_add), 4
                        ),
                    }
                ),
                "after_snapshot": json.dumps(
                    {
                        "latency_p99_seconds": round(
                            PROFILE_BY_NAME[scenario.anomaly_service].base_latency_seconds * 4,
                            4,
                        ),
                        "error_ratio": 0.002,
                        "memory_utilisation_ratio": 0.41,
                    }
                    if executed
                    else {}
                ),
            },
        )

        if scenario.chaos:
            chaos_start = primary.start
            await db.execute(
                INSERT_CHAOS,
                {
                    "run_id": new_id("chaos"),
                    "started_at": chaos_start,
                    "ended_at": chaos_start
                    + timedelta(seconds=int(scenario.chaos["duration_seconds"])),
                    "experiment": scenario.chaos["experiment"],
                    "target_service": scenario.chaos["target_service"],
                    "platform": "mojaloop",
                    "duration_seconds": int(scenario.chaos["duration_seconds"]),
                    "parameters": json.dumps(scenario.chaos["parameters"]),
                    "status": "completed",
                    "detected_at": detected_at,
                    "detected_anomaly_id": anomaly_ids[0] if anomaly_ids else None,
                    "incident_id": incident_id,
                    "remediated_at": resolved_at,
                    "notes": "Seeded demo experiment",
                },
            )

    # Extra actions that were blocked by the safety rails. These matter: they are the
    # evidence that automation has brakes, not just an accelerator.
    ledger = str(MojaloopService.CENTRAL_LEDGER)
    for offset_minutes, status, message in (
        (
            95,
            ActionStatus.SKIPPED_COOLDOWN,
            "Same playbook fired for this target 3 minutes ago",
        ),
        (240, ActionStatus.SKIPPED_BUDGET, "Hourly action budget of 10 already consumed"),
        (
            310,
            ActionStatus.SKIPPED_LOW_CONFIDENCE,
            "RCA confidence 0.42 below the 0.7 minimum",
        ),
    ):
        created = now - timedelta(minutes=offset_minutes)
        await db.execute(
            INSERT_ACTION,
            {
                "action_id": new_id("act"),
                "created_at": created,
                "started_at": None,
                "finished_at": None,
                "playbook": PlaybookName.RESTART_ON_OOM,
                "target_service": ledger,
                "target_instance": f"{SEED_INSTANCE_PREFIX}{ledger}-0",
                "platform": "mojaloop",
                "incident_id": None,
                "anomaly_id": None,
                "trigger_reason": "memory_utilisation_ratio above warning threshold",
                "confidence": 0.42 if status == ActionStatus.SKIPPED_LOW_CONFIDENCE else 0.81,
                "status": status,
                "dry_run": True,
                "parameters": json.dumps({"target": ledger}),
                "result_message": message,
                "before_snapshot": json.dumps({}),
                "after_snapshot": json.dumps({}),
            },
        )


async def seed_predictions(db: Database, now: datetime) -> None:
    """Forecasts for each service and horizon, derived from the same traffic model."""
    settings = get_settings()
    horizons = settings.ml.forecast_horizons_hours or [1, 6, 24]

    for profile in PROFILES:
        current = profile.peak_tps * daily_shape(now) * weekly_shape(now)
        for horizon in horizons:
            # Sample the horizon at a resolution that keeps the payload small but the
            # curve readable: 5-minute steps for 1h, scaling up for longer horizons.
            step_minutes = max(5, horizon * 5)
            steps = int(horizon * 60 / step_minutes)
            points = []
            peak_tps = 0.0
            peak_at = now
            for index in range(1, steps + 1):
                moment = now + timedelta(minutes=step_minutes * index)
                predicted = profile.peak_tps * daily_shape(moment) * weekly_shape(moment)
                # Uncertainty widens with distance, as any honest forecast should.
                spread = 0.06 + 0.10 * (index / max(steps, 1))
                points.append(
                    {
                        "timestamp": moment.isoformat(),
                        "predicted_tps": round(predicted, 2),
                        "lower_bound": round(predicted * (1 - spread), 2),
                        "upper_bound": round(predicted * (1 + spread), 2),
                    }
                )
                if predicted > peak_tps:
                    peak_tps, peak_at = predicted, moment

            # Headroom of 25% on top of the predicted peak, so scaling is not marginal.
            recommended = max(1, math.ceil(peak_tps * 1.25 / profile.tps_per_replica))
            action = (
                "scale_out"
                if recommended > profile.replicas
                else "scale_in"
                if recommended < profile.replicas
                else "hold"
            )
            await db.execute(
                INSERT_PREDICTION,
                {
                    "prediction_id": new_id("pred"),
                    "created_at": now,
                    "service": profile.name,
                    "platform": "mojaloop",
                    "horizon_hours": horizon,
                    "current_tps": round(current, 2),
                    "peak_predicted_tps": round(peak_tps, 2),
                    "peak_at": peak_at,
                    "points": json.dumps(points),
                    "current_replicas": profile.replicas,
                    "recommended_replicas": recommended,
                    "tps_per_replica": profile.tps_per_replica,
                    "scaling_action": action,
                    "model_name": "seasonal-gbm",
                    "model_version": "2026.08.1",
                    "backtest_mape": round(4.2 + horizon * 0.55, 2),
                },
            )

    for name, version, metrics in (
        (
            "isolation-forest",
            "2026.08.1",
            {"precision": 0.91, "recall": 0.86, "f1": 0.88, "contamination": 0.02},
        ),
        ("seasonal-gbm", "2026.08.1", {"mape": 4.75, "rmse": 6.1, "r2": 0.94}),
    ):
        await db.execute(
            INSERT_MODEL_RUN,
            {
                "run_id": new_id("run"),
                "created_at": now - timedelta(minutes=40),
                "model_name": name,
                "model_version": version,
                "service": None,
                "training_rows": 48_000,
                "duration_seconds": 22.4,
                "metrics": json.dumps(metrics),
                "artifact_path": f"ml/artifacts/{name}-{version}.joblib",
                "notes": "seeded demo training run",
            },
        )


async def follow(db: Database, generator: MetricGenerator) -> None:
    """Keep appending samples so the dashboard stays live during a demo.

    Also publishes each batch to the `aiops.metrics` stream, exactly as the module 3
    metrics agent will. That makes the whole live path real rather than simulated: the
    gateway tails Redis, fans the events out over WebSocket, and the browser reacts.
    """
    bus = EventBus(source="seed")
    await bus.connect()
    log.info("seed.follow_started", interval_seconds=LIVE_INTERVAL_SECONDS)
    try:
        while True:
            moment = utc_now().replace(microsecond=0)
            rows = [generator.row(profile, moment) for profile in PROFILES]
            await db.execute_many(INSERT_METRICS, rows)

            for row in rows:
                # `MetricSample` validates the payload, so anything the dashboard
                # receives has already passed the same contract the real agent uses.
                sample = MetricSample(
                    **{key: value for key, value in row.items() if key != "error_ratio"}
                )
                await bus.publish(Stream.METRICS, sample, event_type="metric_sample")

            log.info("seed.follow_tick", at=moment.isoformat(), rows=len(rows))
            await asyncio.sleep(LIVE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        log.info("seed.follow_stopped")
        raise
    finally:
        await bus.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="days of history to generate")
    parser.add_argument("--clear", action="store_true", help="delete seeded data and exit")
    parser.add_argument(
        "--follow",
        action="store_true",
        help="after seeding, keep appending live samples until interrupted",
    )
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="skip metric history (useful with --follow for a quick live-only demo)",
    )
    args = parser.parse_args()

    configure_logging("seed")
    db = Database()
    await db.connect()

    try:
        if args.clear:
            await clear(db)
            return 0

        now = utc_now().replace(second=0, microsecond=0)
        scenarios = build_scenarios(now)
        faults = [fault for scenario in scenarios for fault in scenario.faults]
        generator = MetricGenerator(faults)

        await clear(db)

        if not args.skip_history:
            start = now - timedelta(days=args.days)
            log.info("seed.metrics_start", days=args.days, services=len(PROFILES))
            written = await seed_metrics(db, generator, start, now)
            log.info("seed.metrics_done", rows=written)

            # Materialise the rollups so the forecast and history views have data
            # immediately instead of waiting for the refresh policies.
            for view in ("service_metrics_1m", "service_metrics_1h"):
                await db.refresh_continuous_aggregate(
                    view, start - timedelta(hours=1), now - timedelta(minutes=2)
                )
            log.info("seed.aggregates_refreshed")

        await seed_scenarios(db, scenarios, now)
        log.info("seed.scenarios_done", count=len(scenarios))

        await seed_predictions(db, now)
        log.info("seed.predictions_done")

        counts = await db.fetch_one(
            """
            SELECT
                (SELECT count(*) FROM service_metrics)      AS metrics,
                (SELECT count(*) FROM anomalies)            AS anomalies,
                (SELECT count(*) FROM incidents)            AS incidents,
                (SELECT count(*) FROM healing_actions)      AS actions,
                (SELECT count(*) FROM traffic_predictions)  AS predictions,
                (SELECT count(*) FROM deployments)          AS deployments,
                (SELECT count(*) FROM chaos_runs)           AS chaos_runs
            """
        )
        log.info("seed.complete", **{k: int(v) for k, v in (counts or {}).items()})

        if args.follow:
            await follow(db, generator)
        else:
            print(
                "\nSeeded. Note: services are marked stale 2 minutes after the last "
                "sample.\nRe-run with --follow to keep the dashboard live for a demo."
            )
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
