"""Turn Prometheus into `MetricSample` rows (module 3).

The agent reads the `dpi:*` **recording rules**, never the raw counters. That is the whole
reason those rules exist: `rate()` windows and `histogram_quantile()` expressions are easy
to write two slightly different ways, and when the agent's number disagrees with the
dashboard's there is nothing in either to show which is wrong. One definition, queried by
the agent, the dashboards and the alerts alike.

Consequence worth stating plainly: every recording rule aggregates `by (service, platform)`,
so what comes back is already service-level. The agent therefore writes one row per service
per window rather than one per replica. See `_instance_for` for how that is labelled and
why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aiops_common.log import get_logger
from aiops_common.promql import PrometheusClient
from aiops_common.schemas import MetricSample, utc_now
from aiops_common.topology import get_topology

log = get_logger(__name__)

# Marks rows this agent wrote, the way `seed-` marks the seeder's. Real deployments get a
# pod name; this is the honest equivalent for a service-level aggregate.
AGENT_SOURCE: Final[str] = "metrics-agent"

# Suffix used when a service has more than one scrape target. The recording rules have
# already summed across them, so attributing the row to any single instance would be a
# lie - and the API gateway sums `tps` across instances, so inventing two rows would
# double-count.
AGGREGATE_SUFFIX: Final[str] = ":aggregate"


# ---------------------------------------------------------------------------
# What to collect
#
# Maps a MetricSample field to the recording rule that supplies it. Declared as data
# rather than as a sequence of awaits so the set of queries is inspectable - the
# verification script asserts every rule here returns data, and a test asserts every
# field named here actually exists on MetricSample.
# ---------------------------------------------------------------------------
GAUGE_QUERIES: Final[dict[str, str]] = {
    "tps": "dpi:tps:rate5m",
    "latency_p50_seconds": "dpi:latency_p50:5m",
    "latency_p95_seconds": "dpi:latency_p95:5m",
    "latency_p99_seconds": "dpi:latency_p99:5m",
    "cpu_utilisation_ratio": "dpi:cpu:avg",
    "memory_utilisation_ratio": "dpi:memory:max",
    "queue_depth": "dpi:queue_depth:sum",
    "active_connections": "dpi:connections:sum",
    "settlement_lag_seconds": "dpi:settlement_lag:max",
    "quote_latency_seconds": "dpi:quote_latency_p95:5m",
    "verification_latency_seconds": "dpi:verification_latency_p95:5m",
}

# Queried alongside the gauges but not stored directly - see `_counts_for`.
ERROR_RATIO_QUERY: Final[str] = "dpi:error_ratio:rate5m"
REPLICAS_QUERY: Final[str] = "dpi:replicas"

# Service discovery. Using `up` rather than the MOJALOOP_SERVICES constant means a service
# added to the scrape config starts being collected without a code change, and a service
# that is genuinely gone stops producing rows instead of producing zeros.
DISCOVERY_QUERY: Final[str] = 'up{job="mojaloop"}'

# How much of the 5-minute rate window each service's counters actually cover: the number
# of raw samples Prometheus holds for the most complete transaction series.
#
# After Prometheus or a service (re)starts, `rate(...[5m])` exists but is computed over a
# window that is mostly empty, and it under-reads badly: measured after a full stack restart,
# central-ledger's tps climbed in a straight line from 0.9 to its real 9.7 over exactly five
# minutes. Stored and scored, that ramp reads as a 18-sigma traffic drop on every service at
# once, and the detector raised `traffic_drop` every 30 s until the window filled.
WINDOW_COVERAGE_QUERY: Final[str] = (
    "max by (service) (count_over_time(dpi_transaction_total[5m]))"
)
RATE_WINDOW_SECONDS: Final[int] = 300

# Whether a service's counters reset (the process restarted) inside the rate window. For
# the five minutes after a restart, rate() under-reads by ~15% - observed 7.45 -> 6.40 tps
# for exactly 300 s after each restart of quoting-service - which the detector correctly
# reads as a traffic drop on a perfectly healthy, just-remediated service. Rows are still
# stored (SLO checks and recovery verification need them) but tagged, and the detector does
# not score tagged rows.
# [6m], one minute wider than the rate window: at exactly five minutes Prometheus's resets()
# flickered 1 -> 0 -> 1 with scrape alignment, and the single untagged sample was flagged.
RESET_QUERY: Final[str] = "max by (service) (resets(dpi_transaction_total[6m]))"
RESTART_IN_WINDOW_LABEL: Final[str] = "restart_in_rate_window"
# Same, for a service whose dependency (direct or transitive, per the topology) restarted:
# its rate window still holds the calls that failed while the dependency was down. Observed
# after every remediation restart: the callers alarmed on errors and p99 for the full five
# minutes, opening noise anomalies long after the dependency had recovered.
DEPENDENCY_RESTART_LABEL: Final[str] = "dependency_restart_in_rate_window"

# Gauges every DPI service exports; a row is only written when all of them have a value.
CORE_FIELDS: Final[tuple[str, ...]] = (
    "cpu_utilisation_ratio",
    "memory_utilisation_ratio",
    "active_connections",
)
# Share of the window's expected samples required before a rate is trusted. 0.9 (18 of 20)
# still let through readings ~10% low at the tail of the ramp; 0.95 is 19 of 20.
MIN_WINDOW_COVERAGE: Final[float] = 0.95


@dataclass(frozen=True)
class CollectionResult:
    """What one collection cycle produced, including what it could not produce."""

    samples: list[MetricSample]
    collected_at: datetime
    # Rules that returned nothing. Reported rather than silently treated as zero: a
    # missing series and a series reading zero mean very different things, and conflating
    # them teaches the anomaly detector that an outage looks like idleness.
    missing_rules: list[str]
    # Services Prometheus knows about but that are currently down.
    services_down: list[str]
    # Services that are up but whose essential signal was unavailable, so no row was
    # written. Distinct from `services_down`: the service is fine, our view of it is not.
    services_incomplete: list[str]

    @property
    def service_count(self) -> int:
        return len(self.samples)


class MetricsCollector:
    """Queries Prometheus and normalises the answers into `MetricSample`."""

    def __init__(
        self,
        client: PrometheusClient,
        *,
        interval_seconds: int,
        platform: str = "mojaloop",
    ) -> None:
        self._client = client
        self._interval_seconds = interval_seconds
        self._platform = platform

    async def collect(self, at: datetime | None = None) -> CollectionResult:
        """Run one collection cycle.

        `at` pins the evaluation time so a backfill can replay history through exactly the
        same code path as a live cycle. Nothing here reads the clock other than through
        this argument, which is what makes the backfill trustworthy rather than a parallel
        implementation that might drift.
        """
        moment = at or utc_now()
        # Truncate to whole seconds. The primary key is (timestamp, service, instance), so
        # microsecond jitter would make ON CONFLICT never fire and a retried cycle would
        # insert a near-duplicate row a few hundred microseconds apart.
        moment = moment.replace(microsecond=0)

        up, down = await self._discover(moment)
        if not up:
            log.warning("collect.no_services_up", services_down=down)
            return CollectionResult([], moment, [], down, [])

        missing: list[str] = []
        values: dict[str, dict[str, float]] = {}
        for field, query in GAUGE_QUERIES.items():
            by_service = await self._by_service(query, moment)
            if not by_service:
                missing.append(query)
            values[field] = by_service

        ratios = await self._by_service(ERROR_RATIO_QUERY, moment)
        if not ratios:
            missing.append(ERROR_RATIO_QUERY)
        replicas = await self._by_service(REPLICAS_QUERY, moment)
        if not replicas:
            missing.append(REPLICAS_QUERY)
        coverage = await self._by_service(WINDOW_COVERAGE_QUERY, moment)
        resets = await self._by_service(RESET_QUERY, moment)
        topology = get_topology()
        needed = MIN_WINDOW_COVERAGE * RATE_WINDOW_SECONDS / max(1, self._interval_seconds)

        samples: list[MetricSample] = []
        incomplete: list[str] = []
        for service, instance in sorted(up.items()):
            # Skip the service rather than record a zero-traffic row.
            #
            # Every `rate()`-based rule needs history behind the evaluation instant, so for
            # the first five minutes after Prometheus restarts, `dpi:tps:rate5m` has no
            # value while the gauges already do. Writing tps=0 through that window would
            # stamp a five-minute fake outage into the table after every Prometheus restart,
            # and module 4 would train on it as a normal quiet period.
            #
            # Only `tps` is treated as essential. The other rules are legitimately absent
            # for some services - settlement lag is central-ledger only, quote latency is
            # quoting-service only - so requiring them would discard perfectly good rows.
            if service not in values["tps"]:
                incomplete.append(service)
                continue
            # A rate over a mostly-empty window (just after a restart) is not a measurement.
            # Unknown coverage (query returned nothing) is not treated as partial.
            if service in coverage and coverage[service] < needed:
                log.info(
                    "collect.rate_window_warming_up",
                    monitored_service=service,
                    samples_in_window=int(coverage[service]),
                    needed=round(needed),
                )
                incomplete.append(service)
                continue

            # Core gauges every service exports. If one has no value for this service (seen
            # right after a simulator restart, before its series were scraped), writing the
            # 0.0 default records CPU, memory and connections as zero - a -20 sigma reading
            # the detector duly flagged on every service. Skip the row instead, as for tps.
            # Service-specific signals (settlement lag, quote/verification latency) stay
            # optional.
            absent = [f for f in CORE_FIELDS if service not in values[f]]
            if absent:
                log.info("collect.core_gauges_absent", monitored_service=service, absent=absent)
                incomplete.append(service)
                continue

            fields = {field: values[field].get(service, 0.0) for field in GAUGE_QUERIES}
            error_ratio = max(0.0, min(ratios.get(service, 0.0), 1.0))
            request_count, error_count = self._counts_for(
                tps=fields["tps"], error_ratio=error_ratio
            )
            samples.append(
                MetricSample(
                    timestamp=moment,
                    service=service,
                    instance=instance,
                    platform=self._platform,
                    request_count=request_count,
                    error_count=error_count,
                    # The ratio Prometheus measured, not one recomputed from the rounded
                    # counts above - see MetricSample.measured_error_ratio.
                    measured_error_ratio=error_ratio,
                    replicas=int(round(replicas.get(service, 1.0))) or 1,
                    labels={
                        "source": AGENT_SOURCE,
                        "collector": "promql",
                        **({RESTART_IN_WINDOW_LABEL: "1"} if resets.get(service, 0) > 0 else {}),
                        **(
                            {DEPENDENCY_RESTART_LABEL: "1"}
                            if any(
                                resets.get(dependency, 0) > 0
                                for dependency in topology.transitive_dependencies(service)
                            )
                            else {}
                        ),
                    },
                    **fields,
                )
            )

        if missing:
            log.warning("collect.rules_returned_no_data", rules=missing)
        if incomplete:
            log.warning(
                "collect.services_skipped",
                services=incomplete,
                reason="dpi:tps:rate5m had no value; a zero row would invent an outage",
            )

        return CollectionResult(samples, moment, missing, down, incomplete)

    # -- internals ---------------------------------------------------------
    async def _by_service(self, query: str, moment: datetime) -> dict[str, float]:
        """Evaluate `query` at `moment` and index the result by the `service` label.

        Always passes an explicit evaluation time rather than letting Prometheus default to
        "now". A live cycle and a backfill cycle then run identical code, which is the only
        way the backfill can be trusted to produce rows comparable to the live ones.

        A NaN value is dropped rather than coerced to zero. `histogram_quantile` returns NaN
        when its buckets are empty, and Prometheus sends it as the string "NaN", which
        `PrometheusClient` already turns into None. Treating that as 0 would record "latency
        was zero" where the truth is "there were no observations to take a quantile of" -
        the same falsehood, one layer down, that `_discover` avoids for a service that is
        entirely absent.
        """
        return {
            item["labels"]["service"]: float(item["value"])
            for item in await self._client.instant(query, at=moment)
            if "service" in item["labels"] and item["value"] is not None
        }

    async def _discover(self, moment: datetime) -> tuple[dict[str, str], list[str]]:
        """Return `{service: instance}` for targets that are up, plus the ones that are not.

        A service that is down is deliberately *not* given a row of zeros. Writing zeros
        would record "this service handled no traffic and had no errors", which is exactly
        what the anomaly detector would learn to treat as a quiet period. An absent row is
        the truthful statement: we do not know what happened here.
        """
        results = await self._client.instant(DISCOVERY_QUERY, at=moment)
        instances: dict[str, list[str]] = {}
        down: list[str] = []

        for item in results:
            labels = item["labels"]
            service = labels.get("service")
            if not service:
                continue
            if (item["value"] or 0.0) < 1.0:
                down.append(service)
                continue
            instances.setdefault(service, []).append(labels.get("instance", "unknown"))

        up = {
            service: self._instance_for(service, found) for service, found in instances.items()
        }
        return up, sorted(set(down))

    @staticmethod
    def _instance_for(service: str, instances: list[str]) -> str:
        """Choose the `instance` value for a service-level row.

        With one scrape target - which is the case for the simulator, and will be for any
        single-replica deployment - the real target address is used, so a row is traceable
        back to the thing that produced it.

        With several, the recording rules have already summed across them and there is no
        single honest answer, so the row is marked as an aggregate. This matters because
        `DashboardRepository.service_health` **sums** `tps` across a service's instances:
        writing one row per replica while the value is already a sum would multiply the
        platform's reported traffic by the replica count.
        """
        if len(instances) == 1:
            return instances[0]
        return f"{service}{AGGREGATE_SUFFIX}"

    def _counts_for(self, *, tps: float, error_ratio: float) -> tuple[int, int]:
        """Derive the window's transaction and error counts.

        `request_count` counts **transactions**, not API requests, even though
        `dpi:request_rate:rate5m` exists. That choice is what keeps the row
        self-consistent: `MetricSample.error_ratio` is a computed property equal to
        `error_count / request_count`, and `error_count` here comes from the recorded
        transaction error ratio. Pairing a transaction-derived error count with an
        API-request denominator would produce a stored `error_ratio` that disagrees with
        `dpi:error_ratio:rate5m` for no good reason.

        The counts are whole numbers (the columns are `bigint`), so at low error rates they
        are coarse: 0.47 expected errors in a window rounds to 0. They are kept for volume
        reporting only. The stored and scored `error_ratio` is the measured ratio, passed
        separately as `MetricSample.measured_error_ratio` - deriving it from these counts
        made every low-error service read as error-free, which the anomaly detector then
        flagged on every sample.
        """
        requests = max(0, round(tps * self._interval_seconds))
        errors = max(0, round(requests * max(0.0, min(error_ratio, 1.0))))
        return requests, min(errors, requests)


def known_services() -> tuple[str, ...]:
    """The services we expect to find. Used only for reporting, never for collection."""
    return get_topology().services
