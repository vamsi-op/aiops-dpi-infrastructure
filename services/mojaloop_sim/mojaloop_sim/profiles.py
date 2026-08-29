"""Per-service characteristics for the simulated Mojaloop deployment.

Each simulated service runs as its own container (one image, `SIM_SERVICE` selects the
profile). Separate containers matter for later modules: module 6's `restart_on_oom`
playbook needs a real container to restart, and module 9's `kill_service` experiment needs
a real one to kill. A single process pretending to be five services could not support
either.

The `traffic_share` values are not arbitrary - they are derived from the dependency graph
in `aiops_common.constants.MOJALOOP_DEPENDENCIES`, so a service called by two others
carries proportionally more load:

    ml-api-adapter              1.00   external entry point
    transaction-request-service 0.40   external entry point, lower volume
    quoting-service             1.30   = ml-api-adapter(0.90) + transaction-request(0.40)
    account-lookup-service      1.05   = quoting(0.65)        + transaction-request(0.40)
    central-ledger              1.78   = ml-api-adapter(1.00) + quoting(0.78)

That fan-in is what makes root cause analysis in module 7 a real problem: when
central-ledger degrades, three upstream services show symptoms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiops_common.constants import MojaloopService


@dataclass(frozen=True)
class ServiceProfile:
    """Everything that makes one simulated service behave differently from the others."""

    name: str
    description: str

    # Share of platform-wide traffic. See the module docstring for the derivation.
    traffic_share: float

    # Requests per second one replica handles comfortably. Together with `replicas` this
    # gives a capacity, and load_factor = tps / capacity drives latency, CPU, memory,
    # error rate and queue depth. Scaling out therefore genuinely relieves pressure,
    # which is what makes modules 5 and 6 demonstrable rather than cosmetic.
    tps_per_replica: float
    replicas: int

    # Median latency at near-zero load, in seconds.
    base_latency_seconds: float
    # Spread of the latency distribution (sigma of the underlying lognormal).
    latency_sigma: float

    # Idle resource usage, as a ratio of the container limit.
    base_cpu_ratio: float
    base_memory_ratio: float

    # Baseline failure rate under healthy conditions.
    base_error_ratio: float

    # Which DPI-specific business metrics this service reports. Not every service has a
    # settlement lag or a quote latency, and emitting zeros everywhere would teach the
    # anomaly detector that those signals are meaningless.
    emits_settlement_lag: bool = False
    emits_quote_latency: bool = False
    emits_verification_latency: bool = False

    # HTTP endpoints this service exposes, used as a metric label so the data has the
    # same cardinality shape a real deployment would.
    endpoints: tuple[str, ...] = field(default_factory=tuple)


PROFILES: dict[str, ServiceProfile] = {
    MojaloopService.ML_API_ADAPTER: ServiceProfile(
        name=MojaloopService.ML_API_ADAPTER,
        description="Edge adapter: accepts transfers from DFSPs and fans out downstream",
        traffic_share=1.00,
        tps_per_replica=12.0,
        replicas=3,
        base_latency_seconds=0.035,
        latency_sigma=0.45,
        base_cpu_ratio=0.18,
        base_memory_ratio=0.42,
        base_error_ratio=0.0015,
        endpoints=("/transfers", "/transfers/{id}", "/participants"),
    ),
    MojaloopService.CENTRAL_LEDGER: ServiceProfile(
        name=MojaloopService.CENTRAL_LEDGER,
        description="Ledger and settlement: highest fan-in, the usual bottleneck",
        traffic_share=1.78,
        tps_per_replica=15.0,
        replicas=4,
        # Slowest of the five: it does the durable writes.
        base_latency_seconds=0.055,
        latency_sigma=0.55,
        base_cpu_ratio=0.22,
        base_memory_ratio=0.48,
        base_error_ratio=0.0020,
        emits_settlement_lag=True,
        endpoints=("/transfers", "/settlements", "/positions", "/participants"),
    ),
    MojaloopService.QUOTING_SERVICE: ServiceProfile(
        name=MojaloopService.QUOTING_SERVICE,
        description="Quote and fee calculation between DFSPs",
        traffic_share=1.30,
        tps_per_replica=15.0,
        replicas=3,
        base_latency_seconds=0.045,
        latency_sigma=0.50,
        base_cpu_ratio=0.20,
        base_memory_ratio=0.44,
        base_error_ratio=0.0025,
        emits_quote_latency=True,
        endpoints=("/quotes", "/quotes/{id}"),
    ),
    MojaloopService.ACCOUNT_LOOKUP: ServiceProfile(
        name=MojaloopService.ACCOUNT_LOOKUP,
        description="Resolves a party identifier to the DFSP that holds the account",
        traffic_share=1.05,
        tps_per_replica=20.0,
        replicas=2,
        # Fastest of the five: mostly cache lookups.
        base_latency_seconds=0.022,
        latency_sigma=0.40,
        base_cpu_ratio=0.14,
        base_memory_ratio=0.38,
        base_error_ratio=0.0012,
        # Party verification is the closest analogue to MOSIP's identity check, so this
        # is where the cross-platform metric is exercised.
        emits_verification_latency=True,
        endpoints=("/participants/{type}/{id}", "/parties/{type}/{id}"),
    ),
    MojaloopService.TRANSACTION_REQUEST: ServiceProfile(
        name=MojaloopService.TRANSACTION_REQUEST,
        description="Payee-initiated requests (request to pay)",
        traffic_share=0.40,
        tps_per_replica=8.0,
        replicas=2,
        base_latency_seconds=0.040,
        latency_sigma=0.48,
        base_cpu_ratio=0.16,
        base_memory_ratio=0.40,
        base_error_ratio=0.0030,
        endpoints=("/transactionRequests", "/transactionRequests/{id}"),
    ),
}


def get_profile(service: str) -> ServiceProfile:
    """Look up a profile, failing with a useful message rather than a KeyError."""
    try:
        return PROFILES[service]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"Unknown simulated service {service!r}. Set SIM_SERVICE to one of: {known}"
        ) from None
