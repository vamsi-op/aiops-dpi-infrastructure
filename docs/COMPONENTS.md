# Components

Reference for each service: what it is responsible for, what it reads and writes, and its HTTP
API. Every service also exposes `/health`, `/ready`, `/metrics` and interactive API docs at
`/docs`. Host ports are for Docker Compose and kind; inside the cluster or Compose network every
service listens on port 8000.

| Service | Port | Code |
|---|---|---|
| [Shared library](#shared-library-aiops_common) | — | `libs/aiops_common` |
| [DPI workload simulator](#dpi-workload-simulator) | 8011 – 8015 | `services/mojaloop_sim` |
| [Metrics agent](#metrics-agent) | 8020 | `services/metrics_agent` |
| [Anomaly detector](#anomaly-detector) | 8021 | `services/anomaly_detector` |
| [Traffic predictor](#traffic-predictor) | 8022 | `services/traffic_predictor` |
| [RCA engine](#rca-engine) | 8023 | `services/rca_engine` |
| [Healing engine](#healing-engine) | 8024 | `services/healing_engine` |
| [API gateway](#api-gateway) | 8080 | `services/api_gateway` |
| [Dashboard](#dashboard) | 5173 / 8081 | `dashboard` |

---

## Shared library (`aiops_common`)

Everything two services must agree on.

| Module | Provides |
|---|---|
| `config` | `get_settings()`: typed, validated settings from the environment |
| `schemas` | Pydantic models for every event and record (`MetricSample`, `AnomalyEvent`, `Incident`, `HealingAction`, `PredictionEvent`, …) |
| `constants` | Stream names, table names, `ANOMALY_FEATURES` (append-only feature order), enums |
| `topology` | The monitored system's service graph, loaded from the topology file |
| `bus` | Redis Streams client: publish, consumer-group subscribe, atomic flags and counters |
| `db` | Async SQLAlchemy access to TimescaleDB |
| `promql` | Prometheus query client |
| `service` | `build_app()`: the FastAPI factory that adds health, readiness, metrics and logging |
| `log`, `tracing` | Structured JSON logging and OpenTelemetry tracing |

---

## DPI workload simulator

Five simulated Mojaloop services (`ml-api-adapter`, `central-ledger`, `quoting-service`,
`account-lookup-service`, `transaction-request-service`), one container each. They stand in for a
real DPI deployment so the platform has realistic, controllable traffic.

- **Traffic** with daily and weekly seasonality and a random walk; every derived signal
  (latency, queue, CPU, memory, connections) follows load against modelled capacity.
- **DPI metrics** (`dpi_transaction_total`, `dpi_transaction_duration_seconds` histogram,
  `dpi_settlement_lag_seconds`, …) on `/metrics`, container logs, and OpenTelemetry traces.
- **Dependencies and circuit breakers.** Each service polls the services it calls through a
  traced `/dependency/probe`. Their failures propagate to callers, and each caller has a
  closed / open / half-open breaker per dependency.
- **Fault injection** for chaos experiments.
- **Load sharing.** Each process models the whole service; when scaled out, every pod takes
  `1/peers` of the traffic, the pod count coming from DNS (`SIM_PEERS_HOST`: a headless
  `<service>-peers` Service on Kubernetes, the service name under Compose). Prometheus scrapes
  every pod and the recording rules sum across them.

| Method | Path | Purpose |
|---|---|---|
| GET | `/sim` | Current state: load, capacity, active faults, dependencies |
| GET / POST / DELETE | `/sim/faults`, `/sim/faults/{id}` | List, inject or clear faults: `network_latency`, `error_injection`, `memory_hog`, `cpu_hog`, `traffic_spike`, `bad_deploy`, `kill_service`, `queue_backlog`, `settlement_delay` |
| POST | `/sim/scale` | Change the modelled replica count |
| GET | `/sim/circuits` | Breaker state per dependency |
| POST | `/sim/circuits/{dependency}` | Open a breaker (used by the circuit-breaker playbook) |

---

## Metrics agent

Turns Prometheus series into one normalised `MetricSample` per service every 15 seconds.

- **Reads** the `dpi:*` recording rules (`infra/prometheus/rules/`) and discovers services from
  `up`.
- **Writes** `service_metrics` and publishes to `aiops.metrics`.
- Skips a service until its 5-minute rate window is at least 95 % covered, so a just-started
  service never reports a misleading partial rate.
- Tags samples whose rate window spans a restart of the service or of one of its dependencies
  (`restart_in_rate_window`, `dependency_restart_in_rate_window`): a counter reset, or a process
  younger than 7 minutes (`dpi_process_start_time_seconds`), which also covers restarts
  Prometheus did not witness and Kubernetes rolling restarts.

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Last cycle, rows written, services skipped |
| GET | `/queries` | The PromQL the agent runs |
| POST | `/collect` | Run one collection cycle now |
| POST | `/backfill` | Rebuild history from Prometheus for a past window |

---

## Anomaly detector

Scores every sample and publishes anomalies with an explanation.

- **Model per service.** An Isolation Forest over the nine-signal feature vector, whose inputs
  are σ deviations from a weekday × hour baseline. One-sided features are clipped in the harmless
  direction (low latency is not an anomaly). p99 latency is weighted by how many tail
  observations support it.
- **Decision.** The larger of the forest score and a deviation score, calibrated so 0.5 is the
  decision boundary; anomalies at or above `ANOMALY_SCORE_THRESHOLD` (0.65) are actionable.
- **Guards against false alarms.** Two consecutive anomalous samples are required (unless the
  score is ≥ 0.95 with a ≥ 4σ signal). Load gauges alone must move ≥ 3σ, otherwise a
  user-facing symptom must be involved. Samples in a restart window are scored on gauges only.
- **Classification** into a kind (`memory_pressure`, `cpu_saturation`, `latency_spike`,
  `error_burst`, `queue_backlog`, `settlement_delay`, `traffic_surge`, `traffic_drop`,
  `multivariate`) and a severity, with the contributing features named.
- **Crash reporting.** When the restart tag first appears for a service and no healing action or
  deployment explains it, a `service_restart` anomaly is raised: the orchestrator already
  restarted the process, but the crash is still something an operator must see.
- **Reads** `aiops.metrics`; **writes** `anomalies` and `aiops.anomalies`; models are stored in
  `ml/artifacts/`, and each training run is recorded in `model_runs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Models loaded, samples scored, anomalies emitted |
| GET | `/config` | Threshold, contamination, feature list |
| POST | `/train` | Train every service's model from TimescaleDB history (body: `{"days": 28}`) |
| POST | `/score` | Score an arbitrary sample (for testing) |

---

## Traffic predictor

Forecasts each service's load and recommends replica counts.

- **Model.** A seasonal weekday × hour profile, level-adjusted to recent traffic, blended with a
  seasonal-naïve forecast; the blend weight depends on lead time. Prediction intervals widen with
  the horizon.
- **Horizons** 1, 6 and 24 hours, re-forecast every 10 minutes from the hourly rollup.
- **Backtested** at rolling origins against the seasonal-naïve baseline. The measured MAPE sets
  the headroom of the replica recommendation (never below `SCALING_MIN_HEADROOM`).
- **Writes** `traffic_predictions` and `aiops.predictions`. The healing engine acts on scale-out
  recommendations before the peak arrives.

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Last cycle, predictions written, per-horizon accuracy |
| GET | `/accuracy` | Model vs. baseline MAPE per service and horizon |
| GET | `/forecast/{service}` | Latest stored forecast |
| POST | `/run` | Forecast now |

---

## RCA engine

Correlates anomalies into incidents and ranks root-cause hypotheses.

| Dimension | Method |
|---|---|
| Temporal | Anomalies within a 5-minute window form one candidate incident. |
| Topological | The root is the deepest affected dependency in the service graph; its affected callers are victims. |
| Change | A deployment of an affected service shortly before the anomalies (CHANGE). It outranks the generic "this service is unhealthy" hypothesis. |
| Event | Traffic surges and running chaos experiments, listed as context below the diagnosis. |
| Telemetry | Loki error counts and Tempo service-graph failed edges confirm or strengthen hypotheses. |

- Merges new anomalies into an open incident on the same or a dependent service.
- **Auto-resolves** an incident when every affected service meets its SLOs (error ≤ 1 %, p99 ≤
  250 ms, queue ≤ 5, CPU and memory ≤ 85 %) for a continuous 120 s and the last executed action
  did not fail.
- **Reads** `aiops.anomalies`, `deployments`, `chaos_runs`, `healing_actions`, `service_metrics`;
  **writes** `incidents` and `aiops.incidents`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/status`, `/config` | Correlation window, open incidents |
| GET | `/incidents`, `/incidents/{id}` | Incidents with hypotheses and evidence |
| POST | `/incidents/{id}/resolve` | Resolve manually |
| POST | `/correlate` | Correlate a supplied set of anomalies (for testing) |

---

## Healing engine

Selects and executes remediation, behind safety rails. See [OPERATIONS.md](OPERATIONS.md) for
the playbooks and rails.

- **Reads** `aiops.anomalies`, `aiops.incidents` and `aiops.predictions`; waits for the RCA
  incident before acting on an anomaly.
- **Executors.** Docker SDK under Compose; the Kubernetes API in a cluster (rolling restart,
  scaling that respects an HPA, `rollout undo` to the previous ReplicaSet). Selected by
  `HEALING_EXECUTOR` (`auto` picks Kubernetes inside a pod).
- **Verifies** every action against post-action samples and records before and after
  snapshots.
- **Writes** `healing_actions` and `aiops.actions`, and moves the incident to `mitigating`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Enabled flag, actions taken and blocked |
| GET | `/config` | Safety-rail settings and playbook list |
| GET | `/actions`, `/actions/{id}` | Action history with snapshots and verification |

---

## API gateway

The only backend the dashboard talks to. It reads TimescaleDB, relays the event streams over
WebSocket, and starts chaos experiments.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/overview` | Totals, counts and per-service health for the landing page |
| GET | `/api/services`, `/api/services/{service}` | Health tiles; one service's history, anomalies and forecasts |
| GET | `/api/topology` | The service graph |
| GET | `/api/anomalies`, `/api/anomalies/timeline`, `/api/anomalies/{id}` | Anomaly list, counts per time bucket, one anomaly |
| GET | `/api/incidents`, `/api/incidents/{id}` | Incidents; one incident with its anomalies, actions and deployments |
| GET | `/api/predictions` | Latest forecasts |
| GET | `/api/automation`, `/api/actions` | Healing summary and action log |
| GET / POST | `/api/chaos` | Experiment history; start an experiment. On Kubernetes `bad_deploy` is a real rollout of a defective revision. |
| GET | `/api/deployments`, `/api/config` | Release history; thresholds for the UI |
| WS | `/ws` | Live events from every stream |

Health tiles use the platform SLOs: amber at p99 > 250 ms, error ratio > 1 %, queue > 5; red at
p99 > 1 s, error ratio > 5 %, queue > 50.

---

## Dashboard

React 18 + TypeScript + Recharts. Pages: **Overview** (platform health, incidents, live
events), **Services**, **Anomalies** (timeline and per-feature explanation), **Incidents**
(hypotheses, evidence, actions), **Forecast** (actual vs. forecast with confidence band, replica
recommendations), **Automation** (safety rails, action log) and **Chaos** (run experiments,
scorecard).

Development: `npm run dev` on port 5173, proxying to the gateway on 8080. Production: the
`dashboard/Dockerfile` image serves the built app with nginx and proxies `/api` and `/ws`.
