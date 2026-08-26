"""The shared FastAPI factory: standard endpoints every service inherits."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiops_common.errors import StorageError
from aiops_common.service import build_app


def test_health_reports_ok() -> None:
    app = build_app("unit-test-service")
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "unit-test-service"
    assert body["env"] == "test"


def test_ready_is_ok_when_all_checks_pass() -> None:
    async def db_check() -> bool:
        return True

    app = build_app("unit-test-service", readiness_checks={"timescaledb": db_check})
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"] == {"timescaledb": "ok"}


def test_ready_returns_503_when_a_dependency_is_down() -> None:
    # Readiness must fail loudly so an orchestrator stops routing traffic here.
    async def failing_check() -> None:
        raise StorageError("connection refused")

    app = build_app("unit-test-service", readiness_checks={"timescaledb": failing_check})
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "connection refused" in body["dependencies"]["timescaledb"]


def test_metrics_endpoint_exposes_prometheus_format() -> None:
    app = build_app("unit-test-service")
    with TestClient(app) as client:
        client.get("/health")  # generate one request to record
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "aiops_http_requests_total" in response.text
    assert "aiops_service_up" in response.text


def test_request_id_is_echoed_back() -> None:
    app = build_app("unit-test-service")
    with TestClient(app) as client:
        response = client.get("/health", headers={"x-request-id": "req_fixed"})
    assert response.headers["x-request-id"] == "req_fixed"


def test_request_id_is_generated_when_absent() -> None:
    app = build_app("unit-test-service")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.headers["x-request-id"].startswith("req_")


def test_domain_errors_map_to_their_status_code() -> None:
    app = build_app("unit-test-service")

    @app.get("/boom")
    async def boom() -> None:
        raise StorageError("db exploded", table="service_metrics")

    with TestClient(app) as client:
        response = client.get("/boom")

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "StorageError"
    assert body["context"]["table"] == "service_metrics"


def test_startup_and_shutdown_hooks_run_in_order() -> None:
    calls: list[str] = []

    async def start_one() -> None:
        calls.append("start_one")

    def start_two() -> None:
        calls.append("start_two")

    async def stop_one() -> None:
        calls.append("stop_one")

    app = build_app(
        "unit-test-service",
        on_startup=[start_one, start_two],
        on_shutdown=[stop_one],
    )
    with TestClient(app):
        assert calls == ["start_one", "start_two"]

    assert calls == ["start_one", "start_two", "stop_one"]


@pytest.mark.parametrize("path", ["/health", "/ready", "/metrics", "/docs"])
def test_standard_paths_exist(path: str) -> None:
    app = build_app("unit-test-service")
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
