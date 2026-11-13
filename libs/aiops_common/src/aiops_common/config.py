"""Typed, environment-driven configuration for every AIOps service.

Every setting has a safe local default, so a service can start with no `.env` at all.
Values are read from (in order of precedence): real environment variables, then the
nearest `.env` file found by walking up from the current working directory.

Usage:
    from aiops_common.config import get_settings
    settings = get_settings()
    settings.postgres.async_dsn
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["json", "console"]
DpiPlatform = Literal["mojaloop", "mosip", "beckn"]


def running_in_kubernetes() -> bool:
    """True inside a pod. The kubelet injects KUBERNETES_SERVICE_HOST into every container."""
    return bool(os.getenv("KUBERNETES_SERVICE_HOST"))


def find_env_file(start: Path | None = None, filename: str = ".env") -> str | None:
    """Walk up from `start` looking for `filename`; return its path or None.

    This lets a service be launched from any sub-directory (or from Docker, where the
    file is absent and real env vars are used instead) without changing the code.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        env_path = candidate / filename
        if env_path.is_file():
            return str(env_path)
    return None


_ENV_FILE = find_env_file()

_BASE_CONFIG = SettingsConfigDict(
    env_file=_ENV_FILE,
    env_file_encoding="utf-8",
    extra="ignore",
    populate_by_name=True,
    case_sensitive=False,
)


class PostgresSettings(BaseSettings):
    """TimescaleDB / PostgreSQL connection details."""

    model_config = _BASE_CONFIG

    host: str = Field(default="localhost", alias="POSTGRES_HOST")
    port: int = Field(default=5432, alias="POSTGRES_PORT")
    database: str = Field(default="aiops", alias="POSTGRES_DB")
    user: str = Field(default="aiops", alias="POSTGRES_USER")
    password: str = Field(default="aiops_local_dev_password", alias="POSTGRES_PASSWORD")

    pool_size: int = Field(default=5, alias="POSTGRES_POOL_SIZE")
    max_overflow: int = Field(default=5, alias="POSTGRES_MAX_OVERFLOW")
    statement_timeout_ms: int = Field(default=30_000, alias="POSTGRES_STATEMENT_TIMEOUT_MS")
    echo_sql: bool = Field(default=False, alias="POSTGRES_ECHO_SQL")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_dsn(self) -> str:
        """SQLAlchemy DSN for the asyncpg driver (used by application services)."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_dsn(self) -> str:
        """SQLAlchemy DSN for psycopg3 (used by migrations, training jobs, notebooks)."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def libpq_dsn(self) -> str:
        """Plain libpq URI, for tools such as `psql` or Grafana datasource provisioning."""
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


class RedisSettings(BaseSettings):
    """Redis connection details. Redis carries the event bus and ephemeral state."""

    model_config = _BASE_CONFIG

    host: str = Field(default="localhost", alias="REDIS_HOST")
    port: int = Field(default=6379, alias="REDIS_PORT")
    db: int = Field(default=0, alias="REDIS_DB")
    password: str | None = Field(default=None, alias="REDIS_PASSWORD")
    socket_timeout_seconds: float = Field(default=30.0, alias="REDIS_SOCKET_TIMEOUT_SECONDS")
    # Cap on stream length so a stalled consumer can never exhaust memory.
    stream_maxlen: int = Field(default=50_000, alias="REDIS_STREAM_MAXLEN")

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_is_none(cls, value: object) -> object:
        # An unset `REDIS_PASSWORD=` in .env arrives as "" — treat that as "no auth".
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class ObservabilitySettings(BaseSettings):
    """Endpoints of the off-the-shelf observability plane (module 2)."""

    model_config = _BASE_CONFIG

    prometheus_url: str = Field(default="http://localhost:9090", alias="PROMETHEUS_URL")
    loki_url: str = Field(default="http://localhost:3100", alias="LOKI_URL")
    tempo_url: str = Field(default="http://localhost:3200", alias="TEMPO_URL")
    alertmanager_url: str = Field(default="http://localhost:9093", alias="ALERTMANAGER_URL")
    otlp_endpoint: str = Field(default="http://localhost:4318", alias="OTLP_ENDPOINT")
    # Seconds between metric collection cycles of the metrics agent.
    scrape_interval_seconds: int = Field(default=15, alias="SCRAPE_INTERVAL_SECONDS")
    query_timeout_seconds: float = Field(default=10.0, alias="OBSERVABILITY_QUERY_TIMEOUT")

    # Tracing (module 2, part B). On by default: unlike healing, emitting a span changes
    # nothing about the system being observed, and a trace that was never recorded cannot
    # be recovered after the incident.
    tracing_enabled: bool = Field(default=True, alias="TRACING_ENABLED")
    # 1.0 - keep everything. The simulator's traffic is a handful of requests per second,
    # so sampling would only make traces intermittently missing, which is far more
    # confusing to debug than a slightly larger Tempo volume. Lower this if the platform
    # is ever pointed at real Mojaloop traffic.
    trace_sample_ratio: float = Field(default=1.0, alias="TRACE_SAMPLE_RATIO")

    @field_validator("trace_sample_ratio")
    @classmethod
    def _validate_sample_ratio(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("TRACE_SAMPLE_RATIO must be between 0.0 and 1.0 inclusive")
        return value


class MLSettings(BaseSettings):
    """Model training + inference behaviour."""

    model_config = _BASE_CONFIG

    artifact_dir: Path = Field(default=Path("ml/artifacts"), alias="MODEL_ARTIFACT_DIR")
    contamination: float = Field(default=0.02, alias="ANOMALY_CONTAMINATION")
    min_training_rows: int = Field(default=2_000, alias="ANOMALY_MIN_TRAINING_ROWS")
    # Kept as a raw string because pydantic-settings expects JSON for list-typed env
    # vars, and `FORECAST_HORIZONS=1,6,24` is far friendlier in a .env file.
    forecast_horizons: str = Field(default="1,6,24", alias="FORECAST_HORIZONS")
    retrain_interval_minutes: int = Field(default=60, alias="MODEL_RETRAIN_INTERVAL_MINUTES")
    # Anomaly score above this is escalated to the RCA engine.
    anomaly_score_threshold: float = Field(default=0.65, alias="ANOMALY_SCORE_THRESHOLD")

    # --- Forecasting (module 5) ---
    # Days of hourly history the forecaster trains on. 28 gives four weekly cycles, which is the
    # minimum that allows a full week to be held back for validation while three remain for
    # fitting. Fewer than 14 cannot validate weekly seasonality at all.
    forecast_history_days: int = Field(default=28, alias="FORECAST_HISTORY_DAYS")
    # How often to re-forecast. Minutes, not seconds: the input is an hourly rollup, so forecasting
    # faster would produce near-identical predictions and fill the table with noise.
    forecast_interval_seconds: int = Field(default=600, alias="FORECAST_INTERVAL_SECONDS")
    # Floor on the capacity headroom a scaling recommendation carries. The actual headroom is the
    # larger of this and the measured backtest MAPE, so accuracy drives the safety margin - this
    # only guarantees a minimum cushion if a backtest reports suspiciously perfect accuracy.
    scaling_min_headroom: float = Field(default=0.25, alias="SCALING_MIN_HEADROOM")

    @field_validator("scaling_min_headroom")
    @classmethod
    def _validate_headroom(cls, value: float) -> float:
        if not 0.0 <= value < 2.0:
            raise ValueError("SCALING_MIN_HEADROOM must be between 0.0 and 2.0")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def forecast_horizons_hours(self) -> list[int]:
        """Parsed, de-duplicated, ascending forecast horizons in hours."""
        parts = (part.strip() for part in self.forecast_horizons.split(","))
        horizons = {int(part) for part in parts if part}
        if not horizons:
            raise ValueError("FORECAST_HORIZONS must contain at least one positive integer")
        if any(hour <= 0 for hour in horizons):
            raise ValueError("FORECAST_HORIZONS values must be positive")
        return sorted(horizons)

    @field_validator("contamination")
    @classmethod
    def _validate_contamination(cls, value: float) -> float:
        if not 0.0 < value < 0.5:
            raise ValueError("ANOMALY_CONTAMINATION must be between 0 and 0.5 (exclusive)")
        return value


class HealingSettings(BaseSettings):
    """Safety rails for automated remediation. Defaults are deliberately conservative."""

    model_config = _BASE_CONFIG

    # Dry-run by default: playbooks are selected and logged but never executed.
    enabled: bool = Field(default=False, alias="HEALING_ENABLED")
    max_actions_per_hour: int = Field(default=10, alias="HEALING_MAX_ACTIONS_PER_HOUR")
    cooldown_seconds: int = Field(default=300, alias="HEALING_COOLDOWN_SECONDS")
    docker_socket: str = Field(default="/var/run/docker.sock", alias="DOCKER_SOCKET")
    # Which infrastructure the playbooks act on. `auto` picks Kubernetes when the process
    # runs inside a pod (KUBERNETES_SERVICE_HOST is set) and Docker otherwise, so the same
    # image works under Compose and on the kind cluster without a config change.
    executor: Literal["auto", "docker", "kubernetes"] = Field(
        default="auto", alias="HEALING_EXECUTOR"
    )
    k8s_namespace: str = Field(default="aiops", alias="K8S_NAMESPACE")
    # How long a restart, scale or rollback waits for the Deployment rollout to finish
    # before reporting failure. A rollout that never completes is a failed remediation.
    k8s_rollout_timeout_seconds: int = Field(default=90, alias="K8S_ROLLOUT_TIMEOUT_SECONDS")
    # Minimum confidence from the RCA engine before an action is allowed to run.
    min_confidence: float = Field(default=0.7, alias="HEALING_MIN_CONFIDENCE")

    # Scaling limits & resource thresholds
    min_replicas: int = Field(default=1, alias="HEALING_MIN_REPLICAS")
    max_replicas: int = Field(default=3, alias="HEALING_MAX_REPLICAS")
    max_attempts_per_incident: int = Field(default=2, alias="HEALING_MAX_ATTEMPTS_PER_INCIDENT")
    global_max_containers: int = Field(default=20, alias="HEALING_GLOBAL_MAX_CONTAINERS")
    failure_cooldown_seconds: int = Field(default=120, alias="HEALING_FAILURE_COOLDOWN_SECONDS")
    replica_memory_limit_mb: float = Field(default=256.0, alias="REPLICA_MEMORY_LIMIT_MB")
    replica_cpu_limit: float = Field(default=0.40, alias="REPLICA_CPU_LIMIT")
    min_available_ram_mb: float = Field(default=256.0, alias="SCALE_MIN_AVAILABLE_RAM_MB")
    max_host_cpu_percent: float = Field(default=85.0, alias="SCALE_MAX_HOST_CPU_PERCENT")


class SimulatorSettings(BaseSettings):
    """Mojaloop workload simulator knobs (module 2)."""

    model_config = _BASE_CONFIG

    base_tps: float = Field(default=25.0, alias="MOJALOOP_SIM_BASE_TPS")
    seasonality: bool = Field(default=True, alias="MOJALOOP_SIM_SEASONALITY")
    port: int = Field(default=8010, alias="MOJALOOP_SIM_PORT")


class Settings(BaseSettings):
    """Root settings object. Get it via `get_settings()` so it is parsed only once."""

    model_config = _BASE_CONFIG

    env: str = Field(default="local", alias="AIOPS_ENV")
    service_name: str = Field(default="aiops-service", alias="AIOPS_SERVICE_NAME")
    log_level: str = Field(default="INFO", alias="AIOPS_LOG_LEVEL")
    log_format: LogFormat = Field(default="console", alias="AIOPS_LOG_FORMAT")
    dpi_platform: DpiPlatform = Field(default="mojaloop", alias="AIOPS_DPI_PLATFORM")

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    healing: HealingSettings = Field(default_factory=HealingSettings)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_local(self) -> bool:
        return self.env.lower() in {"local", "dev", "development"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def env_file_in_use(self) -> str | None:
        """Which `.env` was loaded, if any. Handy in start-up logs."""
        return _ENV_FILE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment. Intended for tests only."""
    get_settings.cache_clear()
    return get_settings()


def current_service_name(default: str | None = None) -> str:
    """Best-effort service name for logs and metrics labels."""
    return os.getenv("AIOPS_SERVICE_NAME") or default or get_settings().service_name
