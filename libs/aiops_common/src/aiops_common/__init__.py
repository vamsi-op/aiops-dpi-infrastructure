"""Shared foundation for the DPI AIOps platform.

Import the pieces you need directly from this package:

    from aiops_common import get_settings, configure_logging, get_logger
    from aiops_common import EventBus, Database, PrometheusClient
    from aiops_common.schemas import MetricSample, AnomalyEvent
    from aiops_common.constants import Stream, Severity
"""

from __future__ import annotations

__version__ = "0.1.0"

from .bus import Event, EventBus
from .config import Settings, get_settings, reload_settings
from .db import Database, get_database
from .errors import (
    AiopsError,
    ConfigurationError,
    EventBusError,
    InsufficientDataError,
    ModelNotReadyError,
    RemediationBlockedError,
    RemediationFailedError,
    StorageError,
    UpstreamQueryError,
)
from .log import configure_logging, get_logger
from .promql import PrometheusClient

__all__ = [
    "__version__",
    # config
    "Settings",
    "get_settings",
    "reload_settings",
    # logging
    "configure_logging",
    "get_logger",
    # infrastructure clients
    "Database",
    "get_database",
    "Event",
    "EventBus",
    "PrometheusClient",
    # errors
    "AiopsError",
    "ConfigurationError",
    "EventBusError",
    "InsufficientDataError",
    "ModelNotReadyError",
    "RemediationBlockedError",
    "RemediationFailedError",
    "StorageError",
    "UpstreamQueryError",
]
