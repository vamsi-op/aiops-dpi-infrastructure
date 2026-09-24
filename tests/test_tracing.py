"""Tracing setup behaviour (module 2, part B).

These tests never reach the network. The OTLP exporter batches in a background thread and
drops failed sends silently, so a test that "sends a span" would pass with Tempo absent and
prove nothing. What is worth asserting is the wiring the rest of the platform depends on:
the resource attributes Tempo turns into labels, the trace ids the log pipeline stamps, the
idempotence that stops a second provider being installed, and the opt-out.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aiops_common import tracing
from aiops_common.log import _add_trace_context
from aiops_common.tracing import (
    EXCLUDED_URLS,
    configure_tracing,
    current_trace_context,
    excluded_url_patterns,
    shutdown_tracing,
)


@pytest.fixture(autouse=True)
def reset_tracing_state() -> Iterator[None]:
    """Clear the module's provider between tests.

    `configure_tracing` is deliberately idempotent, which means without this every test
    after the first would silently reuse the first test's provider.
    """
    tracing._provider = None
    tracing._instrumented_apps.clear()
    yield
    tracing._provider = None
    tracing._instrumented_apps.clear()


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------
def test_disabled_by_flag_is_a_no_op() -> None:
    assert configure_tracing("disabled-service", enabled=False) is False
    assert tracing._provider is None


def test_disabled_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The opt-out has to work through configuration, not only the direct argument, because
    # that is how a deployment turns it off.
    from aiops_common.config import reload_settings

    monkeypatch.setenv("TRACING_ENABLED", "false")
    reload_settings()
    assert configure_tracing("disabled-service") is False


def test_configure_returns_true_when_enabled() -> None:
    assert configure_tracing("enabled-service", enabled=True) is True
    assert tracing._provider is not None


def test_configure_is_idempotent() -> None:
    configure_tracing("first", enabled=True)
    first = tracing._provider
    configure_tracing("second", enabled=True)
    # OpenTelemetry's global provider can only be set once; a second provider would be
    # created, warned about, and then ignored - so the module must not build one at all.
    assert tracing._provider is first


def test_shutdown_is_safe_to_call_twice() -> None:
    configure_tracing("shutdown-service", enabled=True)
    shutdown_tracing()
    assert tracing._provider is None
    shutdown_tracing()  # must not raise


# ---------------------------------------------------------------------------
# Resource attributes: the join keys between traces, metrics and logs
# ---------------------------------------------------------------------------
def test_resource_carries_the_join_attributes() -> None:
    attributes = tracing._resource("central-ledger").attributes
    # Tempo's metrics generator turns service.name into the `service` label, which is how a
    # span-derived metric lines up with a dpi_* metric. If this name drifts, the join in
    # module 7 returns nothing and looks like missing data rather than a broken contract.
    assert attributes["service.name"] == "central-ledger"
    assert attributes["deployment.environment"] == "test"
    assert attributes["dpi.platform"] == "mojaloop"


def test_sample_ratio_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiops_common.config import reload_settings

    monkeypatch.setenv("TRACE_SAMPLE_RATIO", "1.5")
    with pytest.raises(ValueError, match="TRACE_SAMPLE_RATIO"):
        reload_settings()


# ---------------------------------------------------------------------------
# Trace context -> log lines
# ---------------------------------------------------------------------------
def test_trace_context_is_empty_outside_a_span() -> None:
    assert current_trace_context() == {}


def test_trace_context_is_hex_padded_inside_a_span() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("unit-test-span"):
        context = current_trace_context()

    # Tempo's API takes the trace id from the URL path. An unpadded hex id is a different
    # string and returns 404, so the width is part of the contract, not cosmetic.
    assert len(context["trace_id"]) == 32
    assert len(context["span_id"]) == 16
    assert int(context["trace_id"], 16) > 0


def test_log_processor_stamps_the_active_trace() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("logged-span"):
        stamped = dict(_add_trace_context(None, "info", {"event": "txn.processed"}))

    # This is the whole mechanism behind log-to-trace navigation in Grafana: Alloy lifts
    # these two fields into structured metadata and the Loki datasource links on them.
    assert len(stamped["trace_id"]) == 32
    assert len(stamped["span_id"]) == 16


def test_log_processor_adds_nothing_outside_a_span() -> None:
    plain = dict(_add_trace_context(None, "info", {"event": "txn.processed"}))
    assert "trace_id" not in plain
    assert "span_id" not in plain


def test_log_processor_does_not_overwrite_an_explicit_trace_id() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("logged-span"):
        stamped = dict(
            _add_trace_context(None, "info", {"event": "x", "trace_id": "caller-supplied"})
        )
    # A service correlating work on behalf of another trace must be able to say so.
    assert stamped["trace_id"] == "caller-supplied"


# ---------------------------------------------------------------------------
# FastAPI instrumentation
# ---------------------------------------------------------------------------
def test_app_is_instrumented_once() -> None:
    app = FastAPI()
    configure_tracing("app-service", app=app, enabled=True)
    assert id(app) in tracing._instrumented_apps

    # Instrumenting twice raises inside OpenTelemetry's FastAPI integration, so the guard
    # is load-bearing rather than an optimisation.
    configure_tracing("app-service", app=app, enabled=True)
    assert len(tracing._instrumented_apps) == 1


def test_operational_endpoints_are_excluded() -> None:
    # Prometheus scrapes /metrics every 15s and Docker probes /health every 15s across five
    # simulated services. Tracing those outnumbers real transaction spans by orders of
    # magnitude and makes Tempo's span metrics report self-monitoring as DPI traffic.
    for path in ("health", "ready", "metrics"):
        assert path in EXCLUDED_URLS.split(",")


def test_service_specific_exclusions_are_appended() -> None:
    patterns = excluded_url_patterns(r"/sim$,/sim/")
    assert patterns.startswith(EXCLUDED_URLS)
    assert r"/sim$" in patterns.split(",")


def test_no_extra_exclusions_leaves_the_defaults_alone() -> None:
    assert excluded_url_patterns() == EXCLUDED_URLS
    assert excluded_url_patterns("") == EXCLUDED_URLS


def test_simulator_exclusions_do_not_catch_the_traced_endpoint() -> None:
    """The anchoring in SIM_CONTROL_URLS is load-bearing.

    Exclusions are `re.search` substring patterns. A bare `sim` would silently exclude
    /simulate/request - the only endpoint that must produce a trace - and the symptom would
    be an empty Tempo with every component reporting healthy.
    """
    from opentelemetry.util.http import ExcludeList

    from mojaloop_sim.asgi import SIM_CONTROL_URLS

    excluded = ExcludeList(excluded_url_patterns(SIM_CONTROL_URLS).split(","))

    assert excluded.url_disabled("/sim")
    assert excluded.url_disabled("/sim/faults")
    assert excluded.url_disabled("/sim/scale")
    assert excluded.url_disabled("/health")
    assert excluded.url_disabled("/metrics")
    assert not excluded.url_disabled("/simulate/request")
    assert not excluded.url_disabled("/simulate/request?endpoint=/transfers")


def test_tracer_provider_is_registered_globally() -> None:
    configure_tracing("global-service", enabled=True)
    # Services call trace.get_tracer(__name__) directly (routes.py does), so what this
    # module holds and what the process actually uses must be the same object.
    assert trace.get_tracer_provider() is tracing._provider
    assert isinstance(tracing._provider, TracerProvider)


def test_existing_global_provider_is_adopted_not_shadowed() -> None:
    """The regression that caught a real bug.

    OpenTelemetry permits `set_tracer_provider` once per process; later calls warn and keep
    the original. Before the fix, `configure_tracing` built a second provider, believed it
    had installed it, and every span emitted through `trace.get_tracer(...)` went to the
    first one instead - with no error anywhere to explain the empty traces.
    """
    configure_tracing("first-service", enabled=True)
    installed = trace.get_tracer_provider()

    # Simulate a second component configuring tracing later in the same process.
    tracing._provider = None
    configure_tracing("second-service", enabled=True)

    assert tracing._provider is installed
