"""Canonical names used across the platform.

Anything referenced by more than one service belongs here. String literals scattered
across services are the fastest way to break an event-driven system, so the rule is:
if two modules need to agree on a name, it lives in this file.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


# ---------------------------------------------------------------------------
# AIOps services (used as log/metric labels and Docker Compose service names)
# ---------------------------------------------------------------------------
class AiopsService(StrEnum):
    METRICS_AGENT = "metrics-agent"
    ANOMALY_DETECTOR = "anomaly-detector"
    TRAFFIC_PREDICTOR = "traffic-predictor"
    RCA_ENGINE = "rca-engine"
    HEALING_ENGINE = "healing-engine"
    CHAOS_AGENT = "chaos-agent"
    API_GATEWAY = "api-gateway"
    MOJALOOP_SIM = "mojaloop-sim"


# ---------------------------------------------------------------------------
# Monitored DPI components
# ---------------------------------------------------------------------------
class MojaloopService(StrEnum):
    """Core Mojaloop microservices we monitor (mirrors the real deployment names)."""

    ML_API_ADAPTER = "ml-api-adapter"
    QUOTING_SERVICE = "quoting-service"
    CENTRAL_LEDGER = "central-ledger"
    ACCOUNT_LOOKUP = "account-lookup-service"
    TRANSACTION_REQUEST = "transaction-request-service"


MOJALOOP_SERVICES: Final[tuple[str, ...]] = tuple(s.value for s in MojaloopService)

# The dependency graph is what makes root cause analysis possible: when several
# services alarm at once, the upstream-most one is the likely cause.
# Mapping is `service -> services it calls`.
MOJALOOP_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    MojaloopService.ML_API_ADAPTER: (
        MojaloopService.CENTRAL_LEDGER,
        MojaloopService.QUOTING_SERVICE,
    ),
    MojaloopService.TRANSACTION_REQUEST: (
        MojaloopService.QUOTING_SERVICE,
        MojaloopService.ACCOUNT_LOOKUP,
    ),
    MojaloopService.QUOTING_SERVICE: (
        MojaloopService.CENTRAL_LEDGER,
        MojaloopService.ACCOUNT_LOOKUP,
    ),
    MojaloopService.CENTRAL_LEDGER: (),
    MojaloopService.ACCOUNT_LOOKUP: (),
}


# ---------------------------------------------------------------------------
# DPI-specific metric names (exported by the custom exporter, module 3)
# ---------------------------------------------------------------------------
class DpiMetric(StrEnum):
    """Business-level signals that generic infrastructure monitoring misses."""

    # Payments (Mojaloop)
    TRANSACTION_DURATION_SECONDS = "dpi_transaction_duration_seconds"
    TRANSACTION_TOTAL = "dpi_transaction_total"
    TRANSACTION_FAILED_TOTAL = "dpi_transaction_failed_total"
    SETTLEMENT_LAG_SECONDS = "dpi_settlement_lag_seconds"
    QUOTE_LATENCY_SECONDS = "dpi_quote_latency_seconds"
    # Identity (MOSIP) — kept here so the same exporter generalises
    VERIFICATION_LATENCY_SECONDS = "dpi_verification_latency_seconds"
    # Generic API health
    API_REQUEST_TOTAL = "dpi_api_request_total"
    API_SUCCESS_RATIO = "dpi_api_success_ratio"
    API_LATENCY_P99_SECONDS = "dpi_api_latency_p99_seconds"
    # Saturation
    CPU_UTILISATION_RATIO = "dpi_cpu_utilisation_ratio"
    MEMORY_UTILISATION_RATIO = "dpi_memory_utilisation_ratio"
    QUEUE_DEPTH = "dpi_queue_depth"
    ACTIVE_CONNECTIONS = "dpi_active_connections"


# Feature vector fed to the anomaly detector. Order is part of the model contract —
# append new features at the end and bump the model version, never reorder.
ANOMALY_FEATURES: Final[tuple[str, ...]] = (
    "tps",
    "error_ratio",
    "latency_p50_seconds",
    "latency_p99_seconds",
    "cpu_utilisation_ratio",
    "memory_utilisation_ratio",
    "queue_depth",
    "active_connections",
    "settlement_lag_seconds",
)


# ---------------------------------------------------------------------------
# Redis Streams — the event bus topology
# ---------------------------------------------------------------------------
class Stream(StrEnum):
    METRICS = "aiops.metrics"
    ANOMALIES = "aiops.anomalies"
    PREDICTIONS = "aiops.predictions"
    INCIDENTS = "aiops.incidents"
    ACTIONS = "aiops.actions"
    DEPLOYMENTS = "aiops.deployments"
    CHAOS = "aiops.chaos"


# Redis key namespaces (kept distinct from streams to avoid collisions).
KEY_PREFIX: Final[str] = "aiops"
CIRCUIT_BREAKER_KEY: Final[str] = f"{KEY_PREFIX}:cb"
HEALING_COOLDOWN_KEY: Final[str] = f"{KEY_PREFIX}:healing:cooldown"
HEALING_BUDGET_KEY: Final[str] = f"{KEY_PREFIX}:healing:budget"
MODEL_REGISTRY_KEY: Final[str] = f"{KEY_PREFIX}:models"


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalyKind(StrEnum):
    """What sort of deviation was observed. Drives playbook selection."""

    LATENCY_SPIKE = "latency_spike"
    ERROR_BURST = "error_burst"
    TRAFFIC_SURGE = "traffic_surge"
    TRAFFIC_DROP = "traffic_drop"
    MEMORY_PRESSURE = "memory_pressure"
    CPU_SATURATION = "cpu_saturation"
    QUEUE_BACKLOG = "queue_backlog"
    SETTLEMENT_DELAY = "settlement_delay"
    MULTIVARIATE = "multivariate"


class RootCauseCategory(StrEnum):
    """The three families of cause the RCA engine distinguishes."""

    SERVICE = "service"  # a specific microservice is unhealthy
    CHANGE = "change"  # a deployment or config change preceded the anomaly
    EVENT = "event"  # external: traffic surge, dependency outage, chaos experiment
    UNKNOWN = "unknown"


class PlaybookName(StrEnum):
    RESTART_ON_OOM = "restart_on_oom"
    CIRCUIT_BREAK_CASCADE = "circuit_break_cascade"
    ROLLBACK_BAD_DEPLOY = "rollback_bad_deploy"
    SCALE_OUT_PREEMPTIVE = "scale_out_preemptive"
    SCALE_IN_IDLE = "scale_in_idle"
    CLEAR_QUEUE_BACKLOG = "clear_queue_backlog"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    SKIPPED_BUDGET = "skipped_budget"
    SKIPPED_LOW_CONFIDENCE = "skipped_low_confidence"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ChaosExperiment(StrEnum):
    """Failure modes the chaos agent can inject for the live demo."""

    KILL_SERVICE = "kill_service"
    MEMORY_HOG = "memory_hog"
    CPU_HOG = "cpu_hog"
    NETWORK_LATENCY = "network_latency"
    ERROR_INJECTION = "error_injection"
    TRAFFIC_SPIKE = "traffic_spike"
    BAD_DEPLOY = "bad_deploy"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
# TimescaleDB hypertable / table names.
TABLE_METRICS: Final[str] = "service_metrics"
TABLE_ANOMALIES: Final[str] = "anomalies"
TABLE_PREDICTIONS: Final[str] = "traffic_predictions"
TABLE_INCIDENTS: Final[str] = "incidents"
TABLE_ACTIONS: Final[str] = "healing_actions"
TABLE_DEPLOYMENTS: Final[str] = "deployments"
TABLE_CHAOS_RUNS: Final[str] = "chaos_runs"
TABLE_MODEL_RUNS: Final[str] = "model_runs"

# Default HTTP paths every AIOps service exposes.
PATH_HEALTH: Final[str] = "/health"
PATH_READY: Final[str] = "/ready"
PATH_METRICS: Final[str] = "/metrics"
