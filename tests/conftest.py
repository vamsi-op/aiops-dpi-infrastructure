"""Shared pytest fixtures for the workspace.

Module 1 tests are pure unit tests — no Docker required. Tests that need the running
stack are marked `@pytest.mark.integration` and skipped unless AIOPS_RUN_INTEGRATION=1.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from aiops_common.config import get_settings, reload_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a clean, predictable configuration.

    The developer's real `.env` must not change test outcomes, so the values the tests
    care about are pinned here and the settings cache is cleared either side.
    """
    monkeypatch.setenv("AIOPS_ENV", "test")
    monkeypatch.setenv("AIOPS_SERVICE_NAME", "test-service")
    monkeypatch.setenv("AIOPS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("AIOPS_LOG_FORMAT", "console")
    monkeypatch.setenv("AIOPS_DPI_PLATFORM", "mojaloop")
    # Pin infrastructure hosts too. The developer's `.env` points at Docker service
    # names (`timescaledb`, `redis`); unit tests must not depend on that.
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "aiops")
    monkeypatch.setenv("POSTGRES_USER", "aiops")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("REDIS_DB", "0")
    monkeypatch.setenv("ANOMALY_CONTAMINATION", "0.02")
    monkeypatch.setenv("FORECAST_HORIZONS", "1,6,24")
    monkeypatch.setenv("HEALING_ENABLED", "false")
    reload_settings()
    yield
    # Only clear the cache on teardown — do not re-parse. A test that deliberately sets
    # an invalid value (to assert validation fires) would otherwise raise here, because
    # monkeypatch has not yet unwound the environment.
    get_settings.cache_clear()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests unless explicitly enabled."""
    if os.getenv("AIOPS_RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="needs the docker stack; set AIOPS_RUN_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
