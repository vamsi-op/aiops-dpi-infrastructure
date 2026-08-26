"""Minimal Prometheus HTTP API client.

The metrics agent uses this to pull the numbers it needs; the RCA engine uses range
queries to correlate signals around an incident. Only the endpoints we actually need
are implemented, kept deliberately small and dependency-light.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import ObservabilitySettings, get_settings
from .errors import UpstreamQueryError
from .log import get_logger
from .schemas import utc_now

log = get_logger(__name__)

_RETRY: dict[str, Any] = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=0.4, max=3),
    "retry": retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    "reraise": True,
}


class PrometheusClient:
    """Async client for the Prometheus query API."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        settings: ObservabilitySettings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings().observability
        self._base_url = (base_url or self._settings.prometheus_url).rstrip("/")
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._settings.query_timeout_seconds),
            )
        return self

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise UpstreamQueryError("PrometheusClient used before connect()")
        return self._client

    # -- queries -----------------------------------------------------------
    @retry(**_RETRY)  # type: ignore[arg-type]
    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UpstreamQueryError(
                "Prometheus returned an error status",
                status=exc.response.status_code,
                path=path,
            ) from exc
        except httpx.TransportError as exc:
            raise UpstreamQueryError(
                "Prometheus unreachable", url=self._base_url, path=path
            ) from exc

        body = response.json()
        if body.get("status") != "success":
            raise UpstreamQueryError(
                "Prometheus query failed",
                error=body.get("error", "unknown"),
                path=path,
            )
        return body.get("data", {})

    async def instant(self, query: str, at: datetime | None = None) -> list[dict[str, Any]]:
        """Evaluate `query` at a single point in time.

        Returns a list of `{"labels": {...}, "value": float, "timestamp": datetime}`.
        """
        params: dict[str, Any] = {"query": query}
        if at is not None:
            params["time"] = at.timestamp()
        data = await self._get("/api/v1/query", params)
        return [_parse_instant(item) for item in data.get("result", [])]

    async def scalar(self, query: str, default: float = 0.0) -> float:
        """Evaluate a query expected to return one series; falls back to `default`."""
        results = await self.instant(query)
        if not results:
            return default
        value = results[0]["value"]
        return default if value is None else float(value)

    async def scalar_by_label(
        self, query: str, label: str, *, default: float = 0.0
    ) -> dict[str, float]:
        """Evaluate a query and index the results by one label value.

        Handy for `sum(rate(...)) by (service)` style queries where we want a
        `{service: value}` dictionary in one round trip.
        """
        out: dict[str, float] = {}
        for item in await self.instant(query):
            key = item["labels"].get(label)
            if key is None:
                continue
            out[key] = default if item["value"] is None else float(item["value"])
        return out

    async def range(
        self,
        query: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        lookback: timedelta | None = None,
        step_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Evaluate `query` over a time range.

        Provide either `start`/`end`, or `lookback` (relative to now). Returns a list of
        `{"labels": {...}, "values": [(datetime, float), ...]}`.
        """
        end = end or utc_now()
        if start is None:
            start = end - (lookback or timedelta(hours=1))
        data = await self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": f"{step_seconds}s",
            },
        )
        return [_parse_range(item) for item in data.get("result", [])]

    async def label_values(self, label: str) -> list[str]:
        data = await self._get(f"/api/v1/label/{label}/values", {})
        return list(data) if isinstance(data, list) else []

    async def is_healthy(self) -> bool:
        try:
            await self.scalar("up")
            return True
        except UpstreamQueryError:
            return False


def _parse_instant(item: dict[str, Any]) -> dict[str, Any]:
    raw_ts, raw_value = item.get("value", (0, "NaN"))
    return {
        "labels": item.get("metric", {}),
        "value": _to_float(raw_value),
        "timestamp": datetime.fromtimestamp(float(raw_ts), tz=utc_now().tzinfo),
    }


def _parse_range(item: dict[str, Any]) -> dict[str, Any]:
    tz = utc_now().tzinfo
    return {
        "labels": item.get("metric", {}),
        "values": [
            (datetime.fromtimestamp(float(ts), tz=tz), _to_float(value))
            for ts, value in item.get("values", [])
        ],
    }


def _to_float(value: Any) -> float | None:
    """Prometheus sends numbers as strings and uses "NaN" for missing data."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # NaN check
