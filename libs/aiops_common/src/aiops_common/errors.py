"""Shared exception hierarchy.

Services raise these instead of bare `Exception` so the API layer can map failures to
sensible HTTP status codes and the healing engine can distinguish "the action is not
allowed" from "the action broke".
"""

from __future__ import annotations


class AiopsError(Exception):
    """Base class for every error raised by platform code."""

    status_code: int = 500

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        details = ", ".join(f"{key}={value!r}" for key, value in self.context.items())
        return f"{self.message} ({details})"


class ConfigurationError(AiopsError):
    """A required setting is missing, malformed, or mutually inconsistent."""

    status_code = 500


class StorageError(AiopsError):
    """TimescaleDB is unreachable or a query failed."""

    status_code = 503


class EventBusError(AiopsError):
    """Redis is unreachable or a stream operation failed."""

    status_code = 503


class UpstreamQueryError(AiopsError):
    """Prometheus / Loki / Tempo returned an error or timed out."""

    status_code = 502


class ModelNotReadyError(AiopsError):
    """Inference was requested before a model was trained or loaded."""

    status_code = 409


class InsufficientDataError(AiopsError):
    """Not enough history to train or to make a trustworthy prediction."""

    status_code = 422


class RemediationBlockedError(AiopsError):
    """A safety rail (dry-run, cooldown, budget, confidence) refused the action."""

    status_code = 409


class RemediationFailedError(AiopsError):
    """The remediation ran but did not succeed."""

    status_code = 500
