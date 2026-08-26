"""FastAPI application factory shared by every AIOps service.

Guarantees each service exposes the same operational surface:
  * `GET /health`  — process is alive (liveness)
  * `GET /ready`   — dependencies are reachable (readiness)
  * `GET /metrics` — Prometheus exposition, so the platform monitors itself
  * uniform structured logging, request ids, and error handling

Usage:
    from aiops_common.service import build_app, ServiceContext

    def register(app, ctx: ServiceContext): ...

    app = build_app(
        service_name="anomaly-detector",
        description="Isolation Forest based anomaly detection for DPI services",
        readiness_checks={"redis": bus.ping, "timescaledb": db.ping},
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .config import Settings, get_settings
from .constants import PATH_HEALTH, PATH_METRICS, PATH_READY
from .errors import AiopsError
from .log import bind_request_context, clear_request_context, configure_logging, get_logger
from .schemas import HealthStatus, new_id, utc_now

ReadinessCheck = Callable[[], Awaitable[Any] | Any]
Hook = Callable[[], Awaitable[None] | None]

SERVICE_VERSION = "0.1.0"


@dataclass
class ServiceContext:
    """Per-service shared state, reachable from routes via `request.app.state.ctx`."""

    service_name: str
    settings: Settings
    registry: CollectorRegistry
    started_at: float = field(default_factory=time.monotonic)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at


def _self_metrics(registry: CollectorRegistry, service_name: str) -> dict[str, Any]:
    """Instrumentation for the AIOps services themselves — we dogfood our own monitoring."""
    return {
        "requests": Counter(
            "aiops_http_requests_total",
            "HTTP requests handled by this AIOps service",
            ["service", "method", "path", "status"],
            registry=registry,
        ),
        "latency": Histogram(
            "aiops_http_request_duration_seconds",
            "HTTP request duration",
            ["service", "method", "path"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=registry,
        ),
        "up": Gauge(
            "aiops_service_up",
            "1 when the service has completed start-up",
            ["service"],
            registry=registry,
        ),
        "ready": Gauge(
            "aiops_service_ready",
            "1 when all readiness dependencies are reachable",
            ["service"],
            registry=registry,
        ),
    }


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def build_app(
    service_name: str,
    *,
    description: str = "",
    version: str = SERVICE_VERSION,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
    on_startup: Sequence[Hook] = (),
    on_shutdown: Sequence[Hook] = (),
    cors_origins: Sequence[str] = ("*",),
    registry: CollectorRegistry | None = None,
) -> FastAPI:
    """Create a configured FastAPI app with the standard operational endpoints."""
    configure_logging(service_name)
    log = get_logger(service_name)
    settings = get_settings()
    reg = registry or CollectorRegistry()
    metrics = _self_metrics(reg, service_name)
    checks = readiness_checks or {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "service.starting",
            version=version,
            env=settings.env,
            env_file=settings.env_file_in_use,
        )
        for hook in on_startup:
            await _maybe_await(hook())
        metrics["up"].labels(service_name).set(1)
        log.info("service.started")
        try:
            yield
        finally:
            metrics["up"].labels(service_name).set(0)
            for hook in reversed(on_shutdown):
                try:
                    await _maybe_await(hook())
                except Exception:
                    log.exception("service.shutdown_hook_failed")
            log.info("service.stopped")

    app = FastAPI(
        title=f"AIOps · {service_name}",
        description=description or f"{service_name} component of the DPI AIOps platform",
        version=version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.ctx = ServiceContext(service_name=service_name, settings=settings, registry=reg)
    app.state.metrics = metrics

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or new_id("req")
        bind_request_context(request_id=request_id, path=request.url.path)
        # Use the route template (e.g. /anomalies/{id}) so labels stay low-cardinality.
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            clear_request_context()
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        path_label = getattr(route, "path", request.url.path)
        metrics["requests"].labels(
            service_name, request.method, path_label, str(response.status_code)
        ).inc()
        metrics["latency"].labels(service_name, request.method, path_label).observe(elapsed)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(AiopsError)
    async def aiops_error_handler(_request: Request, exc: AiopsError) -> JSONResponse:
        log.warning("request.domain_error", error=exc.message, **exc.context)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "context": {key: str(value) for key, value in exc.context.items()},
            },
        )

    @app.get(PATH_HEALTH, response_model=HealthStatus, tags=["operations"])
    async def health() -> HealthStatus:
        """Liveness: the process is running. Never touches dependencies."""
        return HealthStatus(
            status="ok",
            service=service_name,
            version=version,
            env=settings.env,
            detail=f"uptime {app.state.ctx.uptime_seconds:.0f}s",
        )

    @app.get(PATH_READY, response_model=HealthStatus, tags=["operations"])
    async def ready() -> JSONResponse:
        """Readiness: every registered dependency answered."""
        results: dict[str, str] = {}
        healthy = True
        for name, check in checks.items():
            try:
                await _maybe_await(check())
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                healthy = False
                results[name] = f"error: {exc}"
        metrics["ready"].labels(service_name).set(1 if healthy else 0)
        payload = HealthStatus(
            status="ok" if healthy else "degraded",
            service=service_name,
            version=version,
            env=settings.env,
            dependencies=results,
            checked_at=utc_now(),
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content=payload.model_dump(mode="json"),
        )

    @app.get(PATH_METRICS, include_in_schema=False)
    async def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=generate_latest(reg).decode(), media_type=CONTENT_TYPE_LATEST
        )

    return app
