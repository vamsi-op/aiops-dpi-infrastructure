# Configuration

All services are configured through environment variables, validated at start-up by
`aiops_common.config.get_settings()`. Local defaults live in `.env.example`; `bootstrap.ps1` copies
it to `.env`. Docker Compose passes the shared settings to every service and overrides hostnames
with container names. On Kubernetes they come from the `aiops-env` ConfigMap and `aiops-db`
Secret.

Hostnames in `.env` are `localhost` because that file is also read by scripts running on the host.
Do not change them to container names.

---

## 1. Platform

| Variable | Default | Purpose |
|---|---|---|
| `AIOPS_ENV` | `local` | Environment label on logs and the dashboard |
| `AIOPS_DPI_PLATFORM` | `mojaloop` | Label of the monitored DPI system on every record |
| `AIOPS_TOPOLOGY` | `topology.mojaloop.json` | Topology file in `config/` (Docker Compose) — see section 8 |
| `AIOPS_TOPOLOGY_FILE` | *(unset)* | Absolute path to a topology file; unset means the built-in Mojaloop graph |
| `AIOPS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AIOPS_LOG_FORMAT` | `json` | `json` (required for Loki label parsing) or `console` |

## 2. Storage and event bus

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `localhost` / `5432` / `aiops` / `aiops` / local value | TimescaleDB connection. **Change the password for any shared deployment.** |
| `POSTGRES_HOST_PORT` | `5432` | Host port Compose publishes TimescaleDB on |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | `localhost` / `6379` / `0` / none | Redis connection |
| `REDIS_HOST_PORT` | `6379` | Host port Compose publishes Redis on |

## 3. Observability

| Variable | Default | Purpose |
|---|---|---|
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL`, `ALERTMANAGER_URL` | `http://localhost:9090` etc. | Query endpoints |
| `OTLP_ENDPOINT` | `http://localhost:4318` | Where services send traces |
| `TRACING_ENABLED` / `TRACE_SAMPLE_RATIO` | `true` / `1.0` | OpenTelemetry tracing |
| `SCRAPE_INTERVAL_SECONDS` | `15` | Metrics agent cadence; matches the Prometheus scrape interval |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | `admin` / `admin` | **Change for any shared deployment.** |

## 4. Models

| Variable | Default | Purpose |
|---|---|---|
| `ANOMALY_SCORE_THRESHOLD` | `0.65` | Score at which an anomaly is actionable (0.5 is the model's decision boundary) |
| `ANOMALY_CONTAMINATION` | `0.02` | Isolation Forest expected outlier fraction |
| `ANOMALY_MIN_TRAINING_ROWS` | `2000` | Minimum history per service before a model trains |
| `MODEL_ARTIFACT_DIR` | `ml/artifacts` | Where trained models are stored |
| `FORECAST_HORIZONS` | `1,6,24` | Forecast horizons in hours |
| `FORECAST_HISTORY_DAYS` | `28` | Hourly history the forecaster trains on (four weekly cycles) |
| `FORECAST_INTERVAL_SECONDS` | `600` | How often forecasts are refreshed |
| `SCALING_MIN_HEADROOM` | `0.25` | Minimum capacity headroom on replica recommendations; the measured forecast error raises it |

## 5. Self-healing

| Variable | Default | Purpose |
|---|---|---|
| `HEALING_ENABLED` | `false` | Master switch. Off: actions are proposed and logged, never executed. |
| `HEALING_MIN_CONFIDENCE` | `0.7` | Minimum root-cause confidence to act |
| `HEALING_COOLDOWN_SECONDS` | `300` | Per target, after a successful action |
| `HEALING_FAILURE_COOLDOWN_SECONDS` | `120` | Per target, after a failed action |
| `HEALING_MAX_ACTIONS_PER_HOUR` | `10` | Platform-wide budget of executed actions |
| `HEALING_MAX_ATTEMPTS_PER_INCIDENT` | `2` | Then the incident is escalated to a human |
| `HEALING_MAX_EVENT_AGE_SECONDS` | `120` | Anomalies older than this are not acted on (e.g. a backlog after a restart) |
| `HEALING_EXECUTOR` | `auto` | `docker`, `kubernetes`, or `auto` (Kubernetes when running in a pod) |
| `K8S_NAMESPACE` / `K8S_ROLLOUT_TIMEOUT_SECONDS` | `aiops` / `90` | Namespace the executor may act in; rollout wait |
| `HEALING_MIN_REPLICAS` / `HEALING_MAX_REPLICAS` | `1` / `3` | Scaling bounds |
| `HEALING_GLOBAL_MAX_CONTAINERS` | `20` | Compose only: refuse to scale beyond this many containers on the host |
| `SCALE_MIN_AVAILABLE_RAM_MB` / `SCALE_MAX_HOST_CPU_PERCENT` | `256` / `85` | Compose only: host headroom required before scaling out |
| `REPLICA_MEMORY_LIMIT_MB` / `REPLICA_CPU_LIMIT` | `256` / `0.40` | Compose only: limits applied to added replicas |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Compose only: Docker API socket |

## 6. Simulator

| Variable | Default | Purpose |
|---|---|---|
| `MOJALOOP_SIM_BASE_TPS` | `25` | Baseline transactions per second across the simulated services |
| `MOJALOOP_SIM_SEASONALITY` | `true` | Daily and weekly traffic shape |
| `SIM_*_PORT` | `8011`–`8015` | Host ports of the five simulated services |

## 7. Dashboard

The dashboard reaches the gateway through the Vite dev proxy (`dashboard/vite.config.ts`, default
`http://127.0.0.1:8080`) or, when deployed, through nginx on the same origin. Set
`VITE_API_BASE_URL` / `VITE_WS_URL` only to call a gateway on another host.

---

## 8. The DPI topology file

The platform is not tied to one DPI system. Anomaly detection and forecasting learn each service
from its own metrics; root cause analysis and healing need only the **service graph**: which
services exist and which services each one calls.

```json
{
  "name": "my-dpi-system",
  "services": {
    "api-gateway": { "calls": ["registry", "ledger"] },
    "registry":    { "calls": [] },
    "ledger":      { "calls": [] }
  }
}
```

- `calls` lists the services a service depends on. Failure propagates the other way: when
  `ledger` degrades, `api-gateway` waits and fails too. RCA blames the deepest failing
  dependency, and the circuit-breaker playbook opens breakers on its callers.
- Every name in `calls` must be a service in the file. Self-calls and cycles are rejected at
  start-up, because a cycle has no deepest service to blame.
- Service names must match the `service` label on the `dpi_*` metrics Prometheus scrapes.

`config/topology.mojaloop.json` describes the bundled simulator and is the default.

| Environment | How to use another topology |
|---|---|
| Docker Compose | Put the file in `config/` and set `AIOPS_TOPOLOGY=<file name>` in `.env` |
| Kubernetes | `scripts/k8s.ps1` / `k8s.sh` build the `aiops-topology` ConfigMap from `config/topology.mojaloop.json`; point that line at your file |
| Scripts on the host | `AIOPS_TOPOLOGY_FILE=<path>` |

To monitor another DPI system: expose the `dpi_*` metrics with a `service` label, add its
topology file, load or collect history, and train.
