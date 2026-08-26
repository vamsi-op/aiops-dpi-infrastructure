"""Logging setup behaviour, especially the import-order trap."""

from __future__ import annotations

import structlog

from aiops_common import log as log_module
from aiops_common.log import (
    _add_service_context,
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)


def _render(event: str = "test.event") -> dict[str, object]:
    """Run the identity processor by hand and return the resulting event dict."""
    return dict(_add_service_context(None, "info", {"event": event}))


def test_identity_fields_are_attached() -> None:
    configure_logging("identity-test")
    rendered = _render()
    assert rendered["service"] == "identity-test"
    assert rendered["env"] == "test"
    assert rendered["dpi_platform"] == "mojaloop"


def test_later_explicit_name_wins() -> None:
    # The regression this guards: importing aiops_common creates module-level loggers,
    # which used to lock in the default service name before a service could set its own.
    configure_logging("first-name")
    configure_logging("second-name")
    assert _render()["service"] == "second-name"


def test_explicit_fields_are_not_overwritten() -> None:
    configure_logging("identity-test")
    rendered = dict(_add_service_context(None, "info", {"event": "x", "service": "explicit"}))
    assert rendered["service"] == "explicit"


def test_get_logger_returns_a_bound_logger() -> None:
    logger = get_logger("tests.example")
    assert hasattr(logger, "info")
    assert hasattr(logger, "bind")


def test_request_context_is_bound_and_cleared() -> None:
    clear_request_context()
    bind_request_context(request_id="req_abc", incident_id="inc_123")
    merged = structlog.contextvars.merge_contextvars(None, "info", {"event": "x"})
    assert merged["request_id"] == "req_abc"
    assert merged["incident_id"] == "inc_123"

    clear_request_context()
    assert "request_id" not in structlog.contextvars.merge_contextvars(
        None, "info", {"event": "x"}
    )


def test_configure_logging_is_idempotent() -> None:
    configure_logging("idempotent-test")
    assert log_module._CONFIGURED is True
    configure_logging()  # must not raise or reset anything
    assert log_module._CONFIGURED is True
