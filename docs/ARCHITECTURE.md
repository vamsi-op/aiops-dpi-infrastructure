# Architecture

How the platform is structured, how data moves through it, and the reasoning behind the main
design choices.

---

## 1. Problem

Monitoring that alerts on static thresholds answers "is it broken?" after the fact. A DPI
operator needs three harder answers:

1. **Will it break?** Forecast load and add capacity before the peak, not after a queue forms.
2. **Is this unusual?** 200 ms latency is normal at peak and alarming at 3 a.m. Normal depends on
   the service, the hour and the day of the week.
3. **Why?** Five services alarming together are usually one problem. Find its cause.

Traffic forecasting, anomaly detection and root cause analysis answer these. Self-healing closes
the loop by acting on the diagnosis.

---

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ CONTROL        api-gateway (REST + WebSocket) · dashboard (React)            │
└───────────────────────────────▲──────────────────────────────────────────────┘
                                │
┌───────────────────────────────┴──────────────────────────────────────────────┐
│ INTELLIGENCE   anomaly-detector · traffic-predictor · rca-engine             │
│ & ACTION       healing-engine                                                │
│                Stateless consumers of the event bus; state is in the database │
└───────────────────────────────▲──────────────────────────────────────────────┘
                                │ Redis Streams
┌───────────────────────────────┴──────────────────────────────────────────────┐
│ COLLECTION     metrics-agent → TimescaleDB (raw samples, 1 m and 1 h rollups) │
└───────────────────────────────▲──────────────────────────────────────────────┘
                                │ PromQL · LogQL · Tempo service graph
┌───────────────────────────────┴──────────────────────────────────────────────┐
│ OBSERVABILITY  Prometheus · Loki + Alloy · Tempo · Alertmanager · Grafana     │
└───────────────────────────────▲──────────────────────────────────────────────┘
                                │ /metrics · container logs · OTLP traces
┌───────────────────────────────┴──────────────────────────────────────────────┐
│ WORKLOAD       DPI services (simulated Mojaloop in this repository)           │
└──────────────────────────────────────────────────────────────────────────────┘
```

Services query downward (the database, Prometheus) and publish upward through the event bus. No
service reads another service's private state.

---

## 3. Data flow

A memory leak in `account-lookup-service`, as measured in the chaos scorecard:

| Time | Component | What happens |
|---|---|---|
| 0 s | workload | Memory starts climbing. |
| every 15 s | metrics-agent | Evaluates the `dpi:*` recording rules in Prometheus, builds one `MetricSample` per service, stores it, publishes it to `aiops.metrics`. |
| ~50 s | anomaly-detector | Scores the sample against the service's model. Memory is several σ above the baseline for this weekday and hour. Two consecutive anomalous samples are required, then it publishes a `memory_pressure` anomaly to `aiops.anomalies`. |
| ~64 s | rca-engine | Correlates the anomaly with others in a 5-minute window, along the dependency graph, against recent deployments and events. Opens an incident naming `account-lookup-service` as the root cause and publishes it to `aiops.incidents`. |
| ~64 s | healing-engine | Waits for the incident, selects `restart_on_oom` from the diagnosis, passes the safety rails, records a before-snapshot and restarts the service. |
| ~76 s | healing-engine | Verifies recovery on post-action samples and publishes the result to `aiops.actions`. |
| continuous | api-gateway | Relays every event to the dashboard over WebSocket. |
| ~5 min | rca-engine | Resolves the incident once all affected services meet their SLOs for a continuous 120 s. |

---

## 4. Event bus

Redis Streams with consumer groups give persistence and at-least-once delivery: a consumer that
restarts resumes where it stopped.

| Stream | Producer | Consumers | Payload |
|---|---|---|---|
| `aiops.metrics` | metrics-agent | anomaly-detector, api-gateway | `MetricSample` |
| `aiops.anomalies` | anomaly-detector | rca-engine, healing-engine, api-gateway | `AnomalyEvent` |
| `aiops.incidents` | rca-engine | healing-engine, api-gateway | `Incident` with ranked hypotheses |
| `aiops.actions` | healing-engine | api-gateway | `HealingAction` |
| `aiops.predictions` | traffic-predictor | healing-engine (pre-emptive scale-out), api-gateway | `PredictionEvent` |

Payload schemas live in `libs/aiops_common/src/aiops_common/schemas.py` and are shared by every
service. The api-gateway tails streams without a consumer group (it observes, it does not
consume) and relays each event to dashboard clients over `/ws`. Deployments and chaos runs are
written to their tables, where the RCA engine and the dashboard read them.

---

## 5. Storage

TimescaleDB holds all history. Schema: `infra/timescaledb/init/`.

| Table | Contents |
|---|---|
| `service_metrics` | Hypertable of 15-second samples, with continuous aggregates `service_metrics_1m` and `service_metrics_1h`. Compressed after 2 days, raw rows kept 30 days (the hourly rollup is kept indefinitely for forecasting). |
| `anomalies` | Every anomaly with its score, contributing features, observed values and σ deviations. |
| `incidents` | Correlated incidents, ranked root-cause hypotheses, lifecycle status. |
| `healing_actions` | Every proposed action — executed, failed, or blocked by a safety rail — with before and after snapshots. |
| `traffic_predictions` | Forecasts per service and horizon, with replica recommendations. |
| `deployments` | Releases, the evidence for CHANGE hypotheses. |
| `chaos_runs` | Experiments and their measured time to detect and remediate. |
| `model_runs` | Every training run: data window, row count, metrics, artifact path. |

Redis also holds short-lived coordination state: safety-rail cooldowns, the hourly action budget
and incident attempt counters.

---

## 6. Design decisions

**One topology file, no platform-specific code.** Services and dependencies come from
`config/topology.*.json` ([CONFIGURATION.md](CONFIGURATION.md)). Root cause analysis and the
circuit-breaker playbook read the graph; models are learned from each service's own history.
Monitoring another DPI system means a new topology file and the `dpi_*` metrics, not a code
change.

**One sample shape.** Only the metrics agent knows where a number came from. Everything
downstream sees a validated `MetricSample` with a fixed feature order (`ANOMALY_FEATURES`, which
is append-only, because reordering it would invalidate trained models).

**Training data shaped like live data.** The history the models learn from is built the way
Prometheus builds a live sample: 5-minute window rates and histogram quantiles from a finite
number of observations. A model trained on smoother data flags ordinary live traffic.
See [DATA.md](DATA.md).

**Rates and gauges are treated differently after a restart.** For five minutes after a service
(or one of its dependencies) restarts, its rate signals still include the pre-restart window.
Those samples are scored on their gauges only, with rates held at baseline. That keeps a
remediated service from being flagged for its old fault, while a bad release on Kubernetes —
always a new pod — is still caught in seconds.

**Diagnosis before action.** The healing engine waits for the RCA incident before acting, so a
cascade's victims are never restarted, and a bad deployment is rolled back rather than
restarted.

**Automation is opt-in and audited.** `HEALING_ENABLED=false` by default. Every action passes the
same safety rails, and a blocked action is recorded with the rail that stopped it. See
[OPERATIONS.md](OPERATIONS.md).

**Everything is UTC and timezone-aware.** `utc_now()` is the only clock; naive datetimes fail
lint (`ruff` rule `DTZ`).

**Typed, centralised configuration.** `get_settings()` returns a validated object, so a bad value
fails at start-up with a clear message.

**The platform monitors itself.** Every service exposes `/health`, `/ready` and `/metrics`
through one shared application factory.

---

## 7. Technology choices

| Choice | Reason |
|---|---|
| TimescaleDB | Time-series partitioning, continuous aggregates, compression and retention in plain SQL. The forecaster reads the materialised hourly rollup. |
| Redis Streams | Persistence and consumer groups with one small container. Pub/sub drops messages when a consumer is down; Kafka is heavier than this scale needs. |
| Prometheus recording rules | Rates, histogram quantiles and service discovery done once, in one place; the metrics agent reads finished `dpi:*` series. |
| Isolation Forest + seasonal baseline | Unsupervised, so no labelled incidents are needed. The baseline supplies the "for this hour" context and explains each anomaly per feature. |
| Seasonal profile + seasonal-naïve forecast | Beats both components alone in backtests at every horizon, and is transparent enough to explain. |
| Grafana Alloy | The supported log shipper for Loki (Promtail reached end of life in 2026). |
