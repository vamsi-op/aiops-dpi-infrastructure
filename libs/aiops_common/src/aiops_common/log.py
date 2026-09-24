"""Structured logging setup shared by all services.

Two output modes, chosen with `AIOPS_LOG_FORMAT`:
  * `json`    — one JSON object per line. Use inside Docker so Promtail/Loki can parse it.
  * `console` — colourised, human friendly. Use when running a service in your terminal.

Every log line automatically carries `service`, `env` and `dpi_platform`, so a single
Loki query can slice across the whole platform. Lines emitted inside an active span also
carry `trace_id` and `span_id`, which is what connects a log to the trace that produced it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from .config import get_settings

_CONFIGURED = False
# Read at log time, not captured at configure time, so a service that names itself
# after the library has already initialised logging still gets the right label.
_SERVICE_NAME: str | None = None

# Loggers from third-party libraries that are chatty at INFO level.
_NOISY_LOGGERS = {
    "asyncio": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "sqlalchemy.engine.Engine": logging.WARNING,
    "watchfiles": logging.WARNING,
}


def _add_service_context(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Bind identity fields onto every event."""
    settings = get_settings()
    event_dict.setdefault("service", _SERVICE_NAME or settings.service_name)
    event_dict.setdefault("env", settings.env)
    event_dict.setdefault("dpi_platform", settings.dpi_platform)
    return event_dict


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Uvicorn duplicates its message in `color_message`; drop it to keep lines lean."""
    event_dict.pop("color_message", None)
    return event_dict


def _add_trace_context(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Stamp the active trace and span ids onto the line, when there is one.

    This is the entire mechanism behind "jump from this log line to the trace" in Grafana:
    Alloy lifts `trace_id` into structured metadata, and the Loki datasource turns it into
    a link. Without it, logs and traces are two collections that happen to cover the same
    period and have to be lined up by eye.

    Imported lazily and defensively. `log.py` is imported by every module in the library,
    including at times when OpenTelemetry is deliberately absent (unit tests, the seeder),
    and logging must never be the thing that fails to import.
    """
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - otel is a hard dependency, but be safe
        return event_dict

    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict.setdefault("trace_id", format(context.trace_id, "032x"))
        event_dict.setdefault("span_id", format(context.span_id, "016x"))
    return event_dict


def configure_logging(
    service_name: str | None = None,
    *,
    level: str | None = None,
    log_format: str | None = None,
    force: bool = False,
) -> None:
    """Configure structlog and route the stdlib logging module through it.

    Idempotent, and tolerant of import order. Library modules create their loggers at
    import time, which would otherwise lock in a default service name before the service
    has had a chance to declare its own. Calling this again with a `service_name` updates
    the label without rebuilding the whole pipeline.
    """
    global _CONFIGURED, _SERVICE_NAME

    if service_name:
        _SERVICE_NAME = service_name
    if _CONFIGURED and not force:
        if level:
            logging.getLogger().setLevel(level.upper())
        return

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    resolved_format = log_format or settings.log_format

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_service_context,
        _add_trace_context,
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if resolved_format == "json":
        renderer: Processor = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Not cached: loggers are created at module import, before the final
        # configuration is known, so they must resolve the pipeline on each call.
        cache_logger_on_first_use=False,
    )

    # Send stdlib log records (uvicorn, sqlalchemy, kubernetes client, ...) through
    # the same pipeline so output stays uniform.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(noisy_level, root.level))

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Call `configure_logging()` once at process start-up first."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.stdlib.get_logger(name)


def bind_request_context(**fields: Any) -> None:
    """Attach fields (e.g. `request_id`, `incident_id`) to all logs in this task/thread."""
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    """Remove all context bound by `bind_request_context`."""
    structlog.contextvars.clear_contextvars()
