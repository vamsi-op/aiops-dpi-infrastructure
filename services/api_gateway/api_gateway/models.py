"""Response models for the dashboard API.

These are *view* models, shaped for the UI, deliberately distinct from the domain
contracts in `aiops_common.schemas`. Keeping them separate means the dashboard can gain
a derived field (a status colour, a roll-up) without touching the event contracts that
the detection and healing services depend on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .health_rules import Status


class ViewModel(BaseModel):
    """Permissive base: view models are output-only, so unknown input is not a concern."""

    model_config = {"extra": "ignore", "protected_namespaces": ()}


class Point(ViewModel):
    timestamp: datetime
    tps: float = 0.0
    error_ratio: float = 0.0


class ServiceHealth(ViewModel):
    service: str
    platform: str = "mojaloop"
    status: Status = "unknown"
    reasons: list[str] = Field(default_factory=list)
    last_seen: datetime | None = None
    instances: int = 0
    replicas: int = 1

    tps: float = 0.0
    request_count: int = 0
    error_count: int = 0
    error_ratio: float = 0.0
    success_ratio: float = 1.0

    latency_p50_seconds: float = 0.0
    latency_p95_seconds: float = 0.0
    latency_p99_seconds: float = 0.0

    cpu_utilisation_ratio: float = 0.0
    memory_utilisation_ratio: float = 0.0
    queue_depth: float = 0.0
    active_connections: float = 0.0

    settlement_lag_seconds: float = 0.0
    quote_latency_seconds: float = 0.0
    verification_latency_seconds: float = 0.0

    open_anomalies: int = 0
    sparkline: list[Point] = Field(default_factory=list)


class PlatformTotals(ViewModel):
    tps: float = 0.0
    error_ratio: float = 0.0
    latency_p99_seconds: float = 0.0
    services_total: int = 0
    services_healthy: int = 0
    services_degraded: int = 0
    services_critical: int = 0
    services_stale: int = 0


class OverviewCounts(ViewModel):
    open_incidents: int = 0
    anomalies_24h: int = 0
    critical_anomalies_24h: int = 0
    actions_24h: int = 0  # executed (succeeded, failed or running)
    actions_succeeded_24h: int = 0
    actions_blocked_24h: int = 0  # stopped by a safety rail (skipped_*)
    chaos_active: int = 0
    deployments_24h: int = 0


class PipelineStatus(ViewModel):
    """Which modules are actually deployed.

    The dashboard is built before the services that feed it, so it states plainly what
    is live and what is not, rather than rendering empty charts with no explanation.
    """

    metrics_flowing: bool = False
    anomaly_detection_active: bool = False
    forecasting_active: bool = False
    rca_active: bool = False
    healing_enabled: bool = False
    chaos_available: bool = False
    notes: list[str] = Field(default_factory=list)


class Overview(ViewModel):
    generated_at: datetime
    platform: str
    platform_status: Status
    totals: PlatformTotals
    counts: OverviewCounts
    pipeline: PipelineStatus
    services: list[ServiceHealth] = Field(default_factory=list)


class Anomaly(ViewModel):
    anomaly_id: str
    detected_at: datetime
    window_start: datetime | None = None
    service: str
    platform: str = "mojaloop"
    kind: str = "multivariate"
    severity: str = "warning"
    score: float = 0.0
    raw_score: float | None = None
    threshold: float = 0.65
    contributing_features: list[str] = Field(default_factory=list)
    feature_snapshot: dict[str, float] = Field(default_factory=dict)
    feature_deviations: dict[str, float] = Field(default_factory=dict)
    model_name: str = ""
    model_version: str = ""
    description: str = ""
    incident_id: str | None = None


class TimelineBucket(ViewModel):
    bucket: datetime
    total: int = 0
    critical: int = 0
    warning: int = 0
    info: int = 0
    max_score: float = 0.0


class Hypothesis(ViewModel):
    category: str
    target: str
    confidence: float
    summary: str
    evidence: list[str] = Field(default_factory=list)
    correlated_metrics: dict[str, float] = Field(default_factory=dict)


class Incident(ViewModel):
    incident_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    status: str = "open"
    severity: str = "warning"
    platform: str = "mojaloop"
    title: str = ""
    affected_services: list[str] = Field(default_factory=list)
    anomaly_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    ai_summary: str = ""
    duration_seconds: float | None = None


class HealingAction(ViewModel):
    action_id: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    playbook: str
    target_service: str
    target_instance: str | None = None
    platform: str = "mojaloop"
    incident_id: str | None = None
    anomaly_id: str | None = None
    trigger_reason: str = ""
    confidence: float = 0.0
    status: str = "proposed"
    dry_run: bool = True
    execution_mode: str = "simulated"
    actual_replicas_before: int | None = None
    actual_replicas_after: int | None = None
    simulated_replicas_after: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    result_message: str = ""
    before_snapshot: dict[str, float] = Field(default_factory=dict)
    after_snapshot: dict[str, float] = Field(default_factory=dict)


class Deployment(ViewModel):
    deployment_id: str
    occurred_at: datetime
    service: str
    platform: str = "mojaloop"
    from_version: str | None = None
    to_version: str
    change_type: str = "deploy"
    triggered_by: str = "unknown"
    notes: str = ""


class IncidentDetail(ViewModel):
    incident: Incident
    anomalies: list[Anomaly] = Field(default_factory=list)
    actions: list[HealingAction] = Field(default_factory=list)
    deployments: list[Deployment] = Field(default_factory=list)


class ForecastPoint(ViewModel):
    timestamp: datetime
    predicted_tps: float = 0.0
    lower_bound: float = 0.0
    upper_bound: float = 0.0


class Prediction(ViewModel):
    prediction_id: str
    created_at: datetime
    service: str
    platform: str = "mojaloop"
    horizon_hours: int
    current_tps: float = 0.0
    peak_predicted_tps: float = 0.0
    peak_at: datetime | None = None
    points: list[ForecastPoint] = Field(default_factory=list)
    current_replicas: int = 1
    recommended_replicas: int = 1
    tps_per_replica: float = 50.0
    scaling_action: Literal["scale_out", "scale_in", "hold"] = "hold"
    model_name: str = ""
    model_version: str = ""
    backtest_mape: float | None = None


class ChaosRun(ViewModel):
    run_id: str
    started_at: datetime
    ended_at: datetime | None = None
    experiment: str
    target_service: str
    platform: str = "mojaloop"
    duration_seconds: int = 60
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: str = "running"
    detected_at: datetime | None = None
    detected_anomaly_id: str | None = None
    incident_id: str | None = None
    remediated_at: datetime | None = None
    notes: str = ""
    time_to_detect_seconds: float | None = None
    time_to_remediate_seconds: float | None = None


class MetricPoint(ViewModel):
    bucket: datetime
    tps: float = 0.0
    error_ratio: float = 0.0
    latency_p50_seconds: float = 0.0
    latency_p95_seconds: float = 0.0
    latency_p99_seconds: float = 0.0
    cpu_utilisation_ratio: float = 0.0
    memory_utilisation_ratio: float = 0.0
    queue_depth: float = 0.0
    settlement_lag_seconds: float = 0.0
    replicas: int = 1


class ServiceDetail(ViewModel):
    service: str
    health: ServiceHealth
    metrics: list[MetricPoint] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    actions: list[HealingAction] = Field(default_factory=list)


class AutomationSummary(ViewModel):
    healing_enabled: bool
    dry_run_note: str
    max_actions_per_hour: int
    cooldown_seconds: int
    min_confidence: float
    min_replicas: int = 1
    max_replicas: int = 3
    replica_memory_limit_mb: float = 256.0
    replica_cpu_limit: float = 0.40
    min_available_ram_mb: float = 256.0
    max_host_cpu_percent: float = 85.0
    available_ram_mb: float | None = None
    host_cpu_percent: float | None = None
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    recent: list[HealingAction] = Field(default_factory=list)
