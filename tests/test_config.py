"""Configuration behaviour: defaults, environment overrides, derived values."""

from __future__ import annotations

import pytest

from aiops_common.config import get_settings, reload_settings


def test_defaults_are_usable_without_env_file() -> None:
    settings = get_settings()
    assert settings.env == "test"
    assert settings.dpi_platform == "mojaloop"
    assert settings.postgres.database == "aiops"
    assert settings.redis.port == 6379


def test_log_level_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_LOG_LEVEL", "debug")
    assert reload_settings().log_level == "DEBUG"


def test_postgres_dsns_use_the_right_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_USER", "svc")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_DB", "metrics")
    pg = reload_settings().postgres

    assert pg.async_dsn == "postgresql+asyncpg://svc:secret@db.internal:6543/metrics"
    assert pg.sync_dsn == "postgresql+psycopg://svc:secret@db.internal:6543/metrics"
    assert pg.libpq_dsn.startswith("postgresql://svc:")


def test_empty_redis_password_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # `REDIS_PASSWORD=` in .env means "no auth", not "the empty-string password".
    monkeypatch.setenv("REDIS_PASSWORD", "")
    redis_settings = reload_settings().redis
    assert redis_settings.password is None
    assert redis_settings.url == "redis://localhost:6379/0"


def test_redis_url_includes_password_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_PASSWORD", "hunter2")
    assert reload_settings().redis.url == "redis://:hunter2@localhost:6379/0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,6,24", [1, 6, 24]),
        ("1, 3 ,12", [1, 3, 12]),  # tolerant of whitespace
        ("24,1,6", [1, 6, 24]),  # always ascending
        ("6,6,1", [1, 6]),  # de-duplicated
    ],
)
def test_forecast_horizons_parse_from_comma_string(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[int]
) -> None:
    # A comma string is far friendlier in a .env file than the JSON array
    # pydantic-settings would otherwise demand for a list-typed field.
    monkeypatch.setenv("FORECAST_HORIZONS", raw)
    assert reload_settings().ml.forecast_horizons_hours == expected


def test_invalid_contamination_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOMALY_CONTAMINATION", "0.9")
    with pytest.raises(ValueError, match="ANOMALY_CONTAMINATION"):
        reload_settings()


def test_healing_is_dry_run_by_default() -> None:
    # A safety default worth locking down with a test: automation must not act
    # until someone deliberately switches it on.
    assert reload_settings().healing.enabled is False


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()
