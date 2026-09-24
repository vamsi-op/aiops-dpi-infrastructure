"""OpenTelemetry tracing shared by every AIOps service (module 2, part B).

Lives in the shared library rather than in each service because a trace is only useful if
every span carries the same resource attributes under the same names. Two services that
each configure their own exporter will eventually disagree about whether the attribute is
`service.name` or `service`, and the join that RCA depends on silently returns nothing.

Spans go to Tempo over OTLP/HTTP. Tempo's metrics generator then derives RED metrics and a
service graph from them and remote-writes those into Prometheus, which is what lets module
7 check the hand-maintained `MOJALOOP_DEPENDENCIES` constant against the topology actually
observed on the wire.

Usage, once, at process start-up:

    configure_tracing(SERVICE_NAME, app=app)

Tracing is opt-out via `TRACING_ENABLED=false`. It degrades to a no-op rather than
raising: a collector that is down must not stop a service from serving traffic.
"""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING, Any, Final

from .config import get_settings
from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only needed for the annotation
    from fastapi import FastAPI

log = get_logger(__name__)

# Never traced, in any service. Prometheus scrapes /metrics every 15s and Docker probes
# /health every 15s, across five simulated services plus the platform's own. Tracing those
# buries the handful of spans that represent actual transactions under thousands of
# operational ones, and Tempo's span metrics would report the platform's self-monitoring as
# DPI traffic.
#
# Matched with `re.search` against the request path, so these are substring patterns. A
# service with further endpoints to exclude passes `excluded_urls=` rather than editing
# this - the shared default should only cover what every service has.
EXCLUDED_URLS: Final[str] = "health,ready,metrics"

_provider: Any = None
_instrumented_apps: set[int] = set()


def _resource(service_name: str) -> Any:
    from opentelemetry.sdk.resources import Resource

    settings = get_settings()
    return Resource.create(
        {
            # `service.name` is what Tempo turns into the `service` label on its
            # generated metrics, which is how a span metric joins to a dpi_* metric.
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.env,
            # Mirrors prometheus.yml's external_labels and Alloy's static labels, so all
            # three signals can be filtered by the same pair.
            "dpi.platform": settings.dpi_platform,
        }
    )


def configure_tracing(
    service_name: str,
    *,
    app: FastAPI | None = None,
    endpoint: str | None = None,
    enabled: bool | None = None,
    excluded_urls: str | None = None,
) -> bool:
    """Set up the tracer provider and, if given an app, instrument it.

    `excluded_urls` is appended to `EXCLUDED_URLS` - a comma-separated list of patterns
    matched against the request path - for endpoints only this service needs to keep out
    of its traces.

    Returns True if tracing is active. Idempotent: calling it twice keeps the first
    provider, because OpenTelemetry's global provider can only be set once and the second
    attempt logs a warning rather than replacing it.
    """
    global _provider

    settings = get_settings()
    resolved_enabled = settings.observability.tracing_enabled if enabled is None else enabled
    if not resolved_enabled:
        log.debug("tracing.disabled", monitored_service=service_name)
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError:  # pragma: no cover - only if the extras are not installed
        log.warning("tracing.unavailable", reason="opentelemetry packages not installed")
        return False

    if _provider is None:
        # OpenTelemetry allows the global tracer provider to be set exactly once per
        # process. A second `set_tracer_provider` logs "Overriding of current
        # TracerProvider is not allowed" and keeps the original - so building one without
        # checking leaves this module holding a provider the process is not using, and
        # every span from `trace.get_tracer(...)` goes somewhere else. Adopt whatever is
        # already installed instead of quietly shadowing it.
        installed = trace.get_tracer_provider()
        if isinstance(installed, TracerProvider):
            _provider = installed
            log.debug(
                "tracing.adopted_existing_provider",
                monitored_service=service_name,
                reason="a TracerProvider was already registered in this process",
            )
        else:
            base = (endpoint or settings.observability.otlp_endpoint).rstrip("/")
            exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces")

            # ParentBased wrapping the ratio sampler, not the ratio sampler alone. Without
            # it a child span can be dropped while its parent was kept, producing traces
            # with holes in them - which looks like a service failing to report rather
            # than a sampling decision.
            provider = TracerProvider(
                resource=_resource(service_name),
                sampler=ParentBased(
                    TraceIdRatioBased(settings.observability.trace_sample_ratio)
                ),
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))

            trace.set_tracer_provider(provider)
            _provider = provider
            # Spans sit in the batch processor's queue for up to 5s. Without an explicit
            # flush, the last few spans before shutdown are lost - and those are precisely
            # the ones explaining why the process is shutting down.
            atexit.register(shutdown_tracing)
            log.info(
                "tracing.configured",
                monitored_service=service_name,
                endpoint=f"{base}/v1/traces",
                sample_ratio=settings.observability.trace_sample_ratio,
            )

    if app is not None:
        _instrument_app(app, excluded_urls=excluded_urls)

    return True


def excluded_url_patterns(extra: str | None = None) -> str:
    """The full exclusion list for a service: the shared defaults plus its own."""
    if not extra:
        return EXCLUDED_URLS
    return f"{EXCLUDED_URLS},{extra}"


def _instrument_app(app: FastAPI, *, excluded_urls: str | None = None) -> None:
    """Instrument a FastAPI app, at most once per app object."""
    if id(app) in _instrumented_apps:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover
        log.warning("tracing.fastapi_instrumentation_unavailable")
        return

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=excluded_url_patterns(excluded_urls),
        tracer_provider=_provider,
    )
    _instrumented_apps.add(id(app))


def instrument_httpx() -> bool:
    """Trace outgoing httpx calls so a trace spans service boundaries.

    Separate from `configure_tracing` because it patches httpx globally rather than one
    app object, and a process that only serves requests should not pay for it.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:  # pragma: no cover
        return False
    HTTPXClientInstrumentor().instrument(tracer_provider=_provider)
    return True


def current_trace_context() -> dict[str, str]:
    """Return the active trace and span ids as hex, or an empty dict if not sampled.

    The log pipeline uses this to stamp every line with its trace, which is what makes
    Grafana's log-to-trace jump work. Hex-formatted to 32/16 characters because that is
    what Tempo's API expects in a URL - an unpadded int would 404.
    """
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        return {}

    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def shutdown_tracing() -> None:
    """Flush and shut down the provider. Safe to call more than once.

    Note that this is one-way within a process. OpenTelemetry will not let the global
    provider be replaced, so a later `configure_tracing` adopts this same shut-down
    provider rather than building a working one. That is fine for its only real use -
    process exit - and is the reason this is not offered as a "restart tracing" operation.
    """
    global _provider
    if _provider is None:
        return
    provider, _provider = _provider, None
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown must never raise into the caller
        log.debug("tracing.shutdown_failed", exc_info=True)
