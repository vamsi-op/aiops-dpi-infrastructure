"""Prometheus exporter for a simulated Mojaloop service.

The metric names come straight from `aiops_common.constants.DpiMetric`. That is the
contract between this module and module 3's collection agent, so the names are imported
rather than retyped, and `tests/test_simulator.py` asserts the exposition actually contains
every one of them. A typo here would surface as a silently empty column in the feature
vector three modules downstream.

Two design notes:

* **Counters, not gauges, for traffic.** The simulator increments `dpi_transaction_total`
  by the current rate on every tick, so `rate(dpi_transaction_total[5m])` recovers the TPS.
  That is the idiomatic Prometheus shape and means module 3 uses ordinary PromQL rather
  than reading a bespoke gauge.
* **Histograms for latency, plus a p99 gauge.** The histogram is the real source, letting
  module 3 derive any quantile with `histogram_quantile`. The p99 gauge is redundant but
  declared in `DpiMetric`, and it keeps simple dashboard queries cheap.
"""

from __future__ import annotations

import random

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from aiops_common.constants import DpiMetric

from .profiles import ServiceProfile
from .traffic import ServiceState

# Buckets chosen around the latency range these services actually produce (20 ms healthy,
# multiple seconds when saturated). Default Prometheus buckets top out at 10s with poor
# resolution below 100 ms, which would make p50 useless here.
LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.15,
    0.25,
    0.4,
    0.6,
    1.0,
    1.5,
    2.5,
    5.0,
    10.0,
)

# Cap on histogram observations per tick. At peak a service handles ~40 TPS, and observing
# every simulated request would burn CPU for no extra statistical value.
MAX_OBSERVATIONS_PER_TICK = 40


class SimulatorMetrics:
    """Owns the Prometheus collectors for one simulated service.

    A dedicated registry rather than the global default: the process also runs an
    `aiops_common` FastAPI app with its own `aiops_*` metrics, and keeping them separate
    means the DPI exposition is exactly what a real Mojaloop service would emit.
    """

    def __init__(self, profile: ServiceProfile, *, rng: random.Random | None = None) -> None:
        self._profile = profile
        self._service = profile.name
        self._rng = rng or random.Random()
        self.registry = CollectorRegistry()

        common = ["service"]

        # -- traffic ------------------------------------------------------
        # `status` is the outcome class, matching how a real service would label results.
        self.api_requests = Counter(
            DpiMetric.API_REQUEST_TOTAL,
            "Total API requests handled",
            [*common, "endpoint", "method", "status"],
            registry=self.registry,
        )
        self.transactions = Counter(
            DpiMetric.TRANSACTION_TOTAL,
            "Total DPI transactions processed",
            [*common, "type", "status"],
            registry=self.registry,
        )
        self.transactions_failed = Counter(
            DpiMetric.TRANSACTION_FAILED_TOTAL,
            "DPI transactions that failed",
            [*common, "type", "reason"],
            registry=self.registry,
        )

        # -- latency ------------------------------------------------------
        # The problem statement names "transaction processing time" as a required
        # DPI-specific metric; this histogram is it.
        self.transaction_duration = Histogram(
            DpiMetric.TRANSACTION_DURATION_SECONDS,
            "End-to-end transaction processing time",
            [*common, "endpoint"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.api_latency_p99 = Gauge(
            DpiMetric.API_LATENCY_P99_SECONDS,
            "99th percentile API latency over the current window",
            common,
            registry=self.registry,
        )
        self.api_success_ratio = Gauge(
            DpiMetric.API_SUCCESS_RATIO,
            "Fraction of API requests succeeding, 0.0 to 1.0",
            common,
            registry=self.registry,
        )

        # -- DPI business signals ----------------------------------------
        # Only created when the profile emits them. Publishing constant zeros for a
        # service that has no settlement would teach module 4 that the signal is dead.
        self.settlement_lag: Gauge | None = None
        if profile.emits_settlement_lag:
            self.settlement_lag = Gauge(
                DpiMetric.SETTLEMENT_LAG_SECONDS,
                "How far settlement is running behind real time",
                common,
                registry=self.registry,
            )

        self.quote_latency: Histogram | None = None
        if profile.emits_quote_latency:
            self.quote_latency = Histogram(
                DpiMetric.QUOTE_LATENCY_SECONDS,
                "Time to produce a quote",
                common,
                buckets=LATENCY_BUCKETS,
                registry=self.registry,
            )

        self.verification_latency: Histogram | None = None
        if profile.emits_verification_latency:
            self.verification_latency = Histogram(
                DpiMetric.VERIFICATION_LATENCY_SECONDS,
                "Party or identity verification latency",
                common,
                buckets=LATENCY_BUCKETS,
                registry=self.registry,
            )

        # -- saturation ---------------------------------------------------
        self.cpu = Gauge(
            DpiMetric.CPU_UTILISATION_RATIO,
            "CPU utilisation as a ratio of the container limit",
            common,
            registry=self.registry,
        )
        self.memory = Gauge(
            DpiMetric.MEMORY_UTILISATION_RATIO,
            "Memory utilisation as a ratio of the container limit",
            common,
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            DpiMetric.QUEUE_DEPTH,
            "Items waiting to be processed",
            common,
            registry=self.registry,
        )
        self.active_connections = Gauge(
            DpiMetric.ACTIVE_CONNECTIONS,
            "Currently open connections",
            common,
            registry=self.registry,
        )

        # -- topology -----------------------------------------------------
        self.replicas = Gauge(
            DpiMetric.REPLICAS,
            "Replicas currently serving this service",
            common,
            registry=self.registry,
        )
        self.capacity_tps = Gauge(
            DpiMetric.CAPACITY_TPS,
            "Transactions per second this service can serve at its current replica count",
            common,
            registry=self.registry,
        )

        # -- simulator introspection --------------------------------------
        # Namespaced separately: this is a property of the simulator, not something a real
        # Mojaloop service would report. Module 7 uses it to distinguish a genuine
        # incident from an active chaos experiment.
        self.fault_active = Gauge(
            "mojaloop_sim_fault_active",
            "1 while a fault of this kind is injected into this service",
            [*common, "kind"],
            registry=self.registry,
        )
        self.load_factor = Gauge(
            "mojaloop_sim_load_factor",
            "Offered load divided by capacity. Ground truth for validating detection.",
            common,
            registry=self.registry,
        )

    # -- per-tick update ---------------------------------------------------
    def record(
        self, state: ServiceState, *, tick_seconds: float, active_kinds: set[str]
    ) -> None:
        """Advance all collectors to reflect `state` over one tick."""
        service = self._service
        endpoints = self._profile.endpoints or ("/",)

        # --- counters ---
        # Prometheus counters accept floats, so a fractional per-tick increment needs no
        # carry bookkeeping and rate() still returns the exact offered load.
        total_requests = state.tps * tick_seconds
        failed = total_requests * state.error_ratio
        succeeded = total_requests - failed

        # Spread across endpoints so label cardinality resembles a real service.
        per_endpoint_ok = succeeded / len(endpoints)
        per_endpoint_fail = failed / len(endpoints)
        for endpoint in endpoints:
            self.api_requests.labels(service, endpoint, "POST", "2xx").inc(per_endpoint_ok)
            if per_endpoint_fail > 0:
                self.api_requests.labels(service, endpoint, "POST", "5xx").inc(
                    per_endpoint_fail
                )

        self.transactions.labels(service, "transfer", "committed").inc(succeeded)
        if failed > 0:
            self.transactions.labels(service, "transfer", "aborted").inc(failed)
            self.transactions_failed.labels(service, "transfer", "timeout").inc(failed)

        # --- latency histogram ---
        observations = int(min(MAX_OBSERVATIONS_PER_TICK, max(1.0, total_requests)))
        sigma = self._profile.latency_sigma
        for _ in range(observations):
            endpoint = self._rng.choice(endpoints)
            # Lognormal around the current median gives a right-skewed distribution, which
            # is what request latency looks like in practice.
            sample = state.latency_p50_seconds * self._rng.lognormvariate(0.0, sigma)
            self.transaction_duration.labels(service, endpoint).observe(sample)

            if self.quote_latency is not None:
                self.quote_latency.labels(service).observe(
                    state.quote_latency_seconds * self._rng.lognormvariate(0.0, sigma * 0.8)
                )
            if self.verification_latency is not None:
                self.verification_latency.labels(service).observe(
                    state.verification_latency_seconds
                    * self._rng.lognormvariate(0.0, sigma * 0.8)
                )

        # --- gauges ---
        self.api_latency_p99.labels(service).set(state.latency_p99_seconds)
        self.api_success_ratio.labels(service).set(max(0.0, 1.0 - state.error_ratio))
        self.cpu.labels(service).set(state.cpu_ratio)
        self.memory.labels(service).set(state.memory_ratio)
        self.queue_depth.labels(service).set(state.queue_depth)
        self.active_connections.labels(service).set(state.active_connections)
        self.replicas.labels(service).set(state.replicas)
        self.capacity_tps.labels(service).set(state.capacity_tps)
        self.load_factor.labels(service).set(state.load_factor)

        if self.settlement_lag is not None:
            self.settlement_lag.labels(service).set(state.settlement_lag_seconds)

        # --- fault flags ---
        # Every known kind is published, 1 or 0, so a PromQL query does not have to cope
        # with a series appearing and disappearing.
        from .faults import FaultKind  # local import avoids a circular dependency

        for kind in FaultKind:
            self.fault_active.labels(service, str(kind)).set(
                1 if str(kind) in active_kinds else 0
            )
