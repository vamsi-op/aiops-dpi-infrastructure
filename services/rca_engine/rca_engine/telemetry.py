"""Logs and traces as root-cause evidence (module 7).

The correlator reasons over anomalies, which come from metrics. The project brief asks for
metrics, logs *and* traces, and the other two answer questions metrics cannot:

* **Logs (Loki):** did the suspected service itself log warnings and errors in the window?
  A root cause usually complains first; a victim of a cascade often does not.
* **Traces (Tempo's service graph, remote-written to Prometheus):** which caller -> callee
  calls actually failed? `traces_service_graph_request_failed_total` is derived from real
  spans, so a failing edge *into* a service is direct evidence that its callers are being
  hurt by it - the cascade direction, observed rather than inferred from the static
  dependency map.

Both are best-effort. Loki or Prometheus being down must never block an incident, so every
query returns empty evidence on failure and the hypothesis stands on its metrics alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from aiops_common.config import get_settings
from aiops_common.log import get_logger

log = get_logger(__name__)

QUERY_TIMEOUT_SECONDS = 3.0

LOG_QUERY = (
    'sum by (service) (count_over_time({{service_group="dpi", level=~"warning|error"}}[{window}s]))'
)
FAILED_EDGE_QUERY = (
    "sum by (client, server) (increase(traces_service_graph_request_failed_total[{window}s]))"
)
TOTAL_EDGE_QUERY = "sum by (client, server) (increase(traces_service_graph_request_total[{window}s]))"


@dataclass
class TelemetryEvidence:
    """Warning/error log counts per service and failed-call counts per edge in a window."""

    log_problems: dict[str, int] = field(default_factory=dict)
    failed_calls: dict[tuple[str, str], float] = field(default_factory=dict)
    total_calls: dict[tuple[str, str], float] = field(default_factory=dict)

    def failing_callers_of(self, service: str) -> list[tuple[str, float, float]]:
        """(caller, failed, total) for every edge into `service` with failed calls."""
        out = []
        for (client, server), failed in self.failed_calls.items():
            if server == service and failed >= 1:
                out.append((client, failed, self.total_calls.get((client, server), 0.0)))
        return sorted(out, key=lambda item: -item[1])

    def statements(self, service: str) -> list[str]:
        """Human-readable evidence lines about `service`, for the hypothesis."""
        lines = []
        problems = self.log_problems.get(service, 0)
        if problems:
            lines.append(f"Logs: {problems} warning/error line(s) from {service} in the window")
        for caller, failed, total in self.failing_callers_of(service):
            share = f" of {total:.0f}" if total else ""
            lines.append(f"Traces: {failed:.0f}{share} call(s) {caller} -> {service} failed")
        return lines


class TelemetryClient:
    """Reads log and trace evidence. Injectable `fetch` for tests."""

    def __init__(self, fetch: Any = None) -> None:
        settings = get_settings().observability
        self._loki = settings.loki_url.rstrip("/")
        self._prometheus = settings.prometheus_url.rstrip("/")
        self._fetch = fetch or self._http_get

    async def _http_get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def _vector(self, base: str, path: str, query: str) -> list[dict[str, Any]]:
        try:
            body = await self._fetch(f"{base}{path}", {"query": query})
            return list(body.get("data", {}).get("result", []))
        except Exception as exc:  # noqa: BLE001 - evidence is optional, never fatal
            log.warning("telemetry.query_failed", url=base, error=str(exc))
            return []

    async def collect(self, window_seconds: int) -> TelemetryEvidence:
        window = max(60, int(window_seconds))
        evidence = TelemetryEvidence()

        for item in await self._vector(
            self._loki, "/loki/api/v1/query", LOG_QUERY.format(window=window)
        ):
            service = item.get("metric", {}).get("service")
            if service:
                evidence.log_problems[service] = int(float(item["value"][1]))

        for target, query in (
            (evidence.failed_calls, FAILED_EDGE_QUERY),
            (evidence.total_calls, TOTAL_EDGE_QUERY),
        ):
            for item in await self._vector(
                self._prometheus, "/api/v1/query", query.format(window=window)
            ):
                metric = item.get("metric", {})
                client, server = metric.get("client"), metric.get("server")
                if client and server:
                    target[(client, server)] = float(item["value"][1])

        return evidence
