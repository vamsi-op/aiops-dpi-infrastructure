"""Data contracts shared between services.

These models are the wire format for the Redis event bus *and* the response format of
the HTTP APIs, so a change here is a change to the platform's public interface. Keep
fields additive and optional where possible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .constants import (
    ActionStatus,
    AnomalyKind,
    ChaosExperiment,
    PlaybookName,
    RootCauseCategory,
    Severity,
)


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. Always use this, never `datetime.now()`."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Short, readable, sortable-enough identifier, e.g. `inc_9f2c1a4b`."""
    return f"{prefix}_{uuid4().hex[:12]}"


class AiopsModel(BaseModel):
    """Base model for every contract on the wire.

    `extra="forbid"` is deliberate: a typo in an event payload should fail at the
    boundary rather than silently arriving as a missing feature three services later.
    """

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_assignment=True,
        ser_json_timedelta="float",
        # Several fields are legitimately named `model_name` / `model_version`
        # (they describe ML models). Pydantic reserves the `model_` prefix by default.
        protected_namespaces=(),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        """Allow a serialised model to be re-validated.

        `model_dump()` includes computed fields such as `error_ratio`, but they are not
        constructor arguments, so `extra="forbid"` would reject them on the way back in.
        Events are published as JSON and re-validated by consumers, so that round trip
        has to work. Stripping only the *known* computed names keeps real typos failing.
        """
        if isinstance(data, dict) and cls.model_computed_fields:
            computed = set(cls.model_computed_fields)
            if computed & data.keys():
                return {key: value for key, value in data.items() if key not in computed}
        return data


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class MetricSample(AiopsModel):
    """One observation window for one monitored service.

    This is the row written to the `service_metrics` hypertable and the feature vector
    consumed by the anomaly detector and traffic predictor.
    """

    timestamp: datetime = Field(default_factory=utc_now)
    service: str = Field(description="Monitored DPI service, e.g. `central-ledger`")
    instance: str = Field(default="unknown", description="Pod / container identifier")
    platform: str = Field(default="mojaloop", description="mojaloop | mosip | beckn")

    # Traffic
    tps: float = Field(default=0.0, ge=0, description="Transactions per second")
    request_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)

    # Latency (seconds)
    latency_p50_seconds: float = Field(default=0.0, ge=0)
    latency_p95_seconds: float = Field(default=0.0, ge=0)
    latency_p99_seconds: float = Field(default=0.0, ge=0)

    # Saturation
    cpu_utilisation_ratio: float = Field(default=0.0, ge=0, description="0.0–1.0+")
    memory_utilisation_ratio: float = Field(default=0.0, ge=0, description="0.0–1.0+")
    queue_depth: float = Field(default=0.0, ge=0)
    active_connections: float = Field(default=0.0, ge=0)

    # DPI-specific
    settlement_lag_seconds: float = Field(default=0.0, ge=0)
    quote_latency_seconds: float = Field(default=0.0, ge=0)
    verification_latency_seconds: float = Field(default=0.0, ge=0)

    # Topology
    replicas: int = Field(default=1, ge=0)

    labels: dict[str, str] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_ratio(self) -> float:
        """Fraction of requests that failed in this window (0.0–1.0)."""
        if self.request_count <= 0:
            return 0.0
        return min(self.error_count / self.request_count, 1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_ratio(self) -> float:
        return 1.0 - self.error_ratio

    def feature_vector(self, feature_names: tuple[str, ...]) -> list[float]:
        """Project this sample onto an ordered feature list for the ML models."""
        return [float(getattr(self, name, 0.0) or 0.0) for name in feature_names]


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
class AnomalyEvent(AiopsModel):
    """An unusual pattern detected in one service's telemetry."""

    anomaly_id: str = Field(default_factory=lambda: new_id("anom"))
    detected_at: datetime = Field(default_factory=utc_now)
    window_start: datetime | None = None

    service: str
    platform: str = "mojaloop"
    kind: AnomalyKind = AnomalyKind.MULTIVARIATE
    severity: Severity = Severity.WARNING

    # Normalised 0.0–1.0; 1.0 = most anomalous. Derived from the model's raw score.
    score: float = Field(ge=0.0, le=1.0)
    raw_score: float | None = Field(default=None, description="Model-native score")
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    # Which features drove the decision, most influential first.
    contributing_features: list[str] = Field(default_factory=list)
    feature_snapshot: dict[str, float] = Field(default_factory=dict)
    # Per-feature deviation in standard deviations from the learned baseline.
    feature_deviations: dict[str, float] = Field(default_factory=dict)

    model_name: str = "isolation-forest"
    model_version: str = "0"
    description: str = ""

    # Set by the RCA engine once this anomaly is folded into an incident.
    incident_id: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_actionable(self) -> bool:
        return self.score >= self.threshold


# ---------------------------------------------------------------------------
# Traffic prediction
# ---------------------------------------------------------------------------
class ForecastPoint(AiopsModel):
    timestamp: datetime
    predicted_tps: float = Field(ge=0)
    lower_bound: float = Field(default=0.0, ge=0)
    upper_bound: float = Field(default=0.0, ge=0)


class PredictionEvent(AiopsModel):
    """A traffic forecast for one service over one horizon, plus a scaling recommendation."""

    prediction_id: str = Field(default_factory=lambda: new_id("pred"))
    created_at: datetime = Field(default_factory=utc_now)

    service: str
    platform: str = "mojaloop"
    horizon_hours: int = Field(ge=1)

    current_tps: float = Field(default=0.0, ge=0)
    peak_predicted_tps: float = Field(default=0.0, ge=0)
    peak_at: datetime | None = None
    points: list[ForecastPoint] = Field(default_factory=list)

    current_replicas: int = Field(default=1, ge=0)
    recommended_replicas: int = Field(default=1, ge=0)
    # Capacity assumption used to turn predicted TPS into a replica count.
    tps_per_replica: float = Field(default=50.0, gt=0)

    model_name: str = "seasonal-naive"
    model_version: str = "0"
    # Mean absolute percentage error from the last backtest, if known.
    backtest_mape: float | None = Field(default=None, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scaling_action(self) -> Literal["scale_out", "scale_in", "hold"]:
        if self.recommended_replicas > self.current_replicas:
            return "scale_out"
        if self.recommended_replicas < self.current_replicas:
            return "scale_in"
        return "hold"


# ---------------------------------------------------------------------------
# Deployments & change tracking (input to root cause analysis)
# ---------------------------------------------------------------------------
class DeploymentEvent(AiopsModel):
    """A change to a monitored service — the `change` axis of root cause analysis."""

    deployment_id: str = Field(default_factory=lambda: new_id("dep"))
    occurred_at: datetime = Field(default_factory=utc_now)
    service: str
    platform: str = "mojaloop"
    from_version: str | None = None
    to_version: str
    change_type: Literal["deploy", "rollback", "config", "scale"] = "deploy"
    triggered_by: str = "unknown"
    notes: str = ""


# ---------------------------------------------------------------------------
# Root cause analysis / incidents
# ---------------------------------------------------------------------------
class RootCauseHypothesis(AiopsModel):
    """One candidate explanation, with the evidence that supports it."""

    category: RootCauseCategory
    target: str = Field(description="Service, deployment id, or event name")
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    evidence: list[str] = Field(default_factory=list)
    correlated_metrics: dict[str, float] = Field(default_factory=dict)


class Incident(AiopsModel):
    """A correlated group of anomalies, its likely cause, and the remediation taken."""

    incident_id: str = Field(default_factory=lambda: new_id("inc"))
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None
    status: Literal["open", "mitigating", "resolved", "acknowledged"] = "open"
    severity: Severity = Severity.WARNING

    platform: str = "mojaloop"
    title: str = ""
    affected_services: list[str] = Field(default_factory=list)
    anomaly_ids: list[str] = Field(default_factory=list)

    # Ordered best-first. `primary_cause` is a convenience accessor.
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    # Human-readable narrative generated for the dashboard.
    ai_summary: str = ""
    # Optional resolution reason published when status transitions to 'resolved'.
    reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def primary_cause(self) -> RootCauseHypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_seconds(self) -> float | None:
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


# ---------------------------------------------------------------------------
# Self-healing
# ---------------------------------------------------------------------------
class HealingAction(AiopsModel):
    """One remediation attempt: what was decided, why, and what happened."""

    action_id: str = Field(default_factory=lambda: new_id("act"))
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    playbook: PlaybookName
    target_service: str
    target_instance: str | None = None
    platform: str = "mojaloop"

    incident_id: str | None = None
    anomaly_id: str | None = None
    trigger_reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    status: ActionStatus = ActionStatus.PROPOSED
    dry_run: bool = True
    execution_mode: str = "simulated"
    attempt_number: int = Field(default=1, ge=1)
    requires_manual_intervention: bool = Field(default=False)
    actual_replicas_before: int | None = None
    actual_replicas_after: int | None = None
    simulated_replicas_after: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    result_message: str = ""
    # Metric snapshots either side of the action, to prove it worked.
    before_snapshot: dict[str, float] = Field(default_factory=dict)
    after_snapshot: dict[str, float] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def was_executed(self) -> bool:
        return self.status in {ActionStatus.SUCCEEDED, ActionStatus.FAILED}


# ---------------------------------------------------------------------------
# Chaos engineering
# ---------------------------------------------------------------------------
class ChaosRun(AiopsModel):
    """A deliberately injected failure, used to prove detection and self-healing work."""

    run_id: str = Field(default_factory=lambda: new_id("chaos"))
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None

    experiment: ChaosExperiment
    target_service: str
    platform: str = "mojaloop"
    duration_seconds: int = Field(default=60, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)

    status: Literal["running", "completed", "aborted", "failed"] = "running"
    # Filled in once the platform reacts — this is the demo's scorecard.
    detected_at: datetime | None = None
    detected_anomaly_id: str | None = None
    incident_id: str | None = None
    remediated_at: datetime | None = None
    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def time_to_detect_seconds(self) -> float | None:
        if self.detected_at is None:
            return None
        return (self.detected_at - self.started_at).total_seconds()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def time_to_remediate_seconds(self) -> float | None:
        if self.remediated_at is None:
            return None
        return (self.remediated_at - self.started_at).total_seconds()


# ---------------------------------------------------------------------------
# Operational plumbing
# ---------------------------------------------------------------------------
class ModelRun(AiopsModel):
    """Audit record of a training run, so model quality is traceable over time."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    created_at: datetime = Field(default_factory=utc_now)
    model_name: str
    model_version: str
    service: str | None = None
    training_rows: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    artifact_path: str = ""
    notes: str = ""


class HealthStatus(AiopsModel):
    """Response body of `/health` and `/ready`."""

    status: Literal["ok", "degraded", "error"] = "ok"
    service: str
    version: str = "0.1.0"
    env: str = "local"
    checked_at: datetime = Field(default_factory=utc_now)
    dependencies: dict[str, str] = Field(default_factory=dict)
    detail: str = ""
