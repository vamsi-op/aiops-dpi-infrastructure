# AIOps for DPI Infrastructure

**Predictive scaling and self-healing for Digital Public Infrastructure.**

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)

Digital Public Infrastructure — payment switches, identity platforms, open commerce networks —
serves millions of people and cannot afford downtime. Traditional monitoring reports a failure
after it has happened. This platform forecasts load before the peak, detects behaviour that is
unusual for the time of day, explains the root cause, and remediates automatically behind
explicit safety rails.

It is a **common platform for any DPI system**: the monitored services and their dependencies
come from a topology file, and every model is learned from each service's own telemetry. The
repository ships a simulated [Mojaloop](https://mojaloop.io/) payment switch as the reference
workload.

---

## Capabilities

| Capability | How it works |
|---|---|
| **Monitoring** | Prometheus (metrics), Loki + Alloy (logs), Tempo (traces), Alertmanager and Grafana. DPI-specific signals — transaction rate, error ratio, latency percentiles, queue depth, settlement lag — alongside CPU, memory and connections. |
| **Anomaly detection** | Per-service Isolation Forest over a nine-signal feature vector, scored against a weekday × hour seasonal baseline, with per-feature attribution. Two-sample persistence and an evidence gate keep ordinary load variation from becoming alerts. |
| **Traffic forecasting** | Seasonal profile blended with a seasonal-naïve forecast at 1, 6 and 24 hour horizons. Backtested MAPE ≈ 1 %; the measured error sets the headroom of each replica recommendation. |
| **Root cause analysis** | Correlates anomalies by time, along the dependency graph (the deepest failing dependency is the root), against recent deployments, and against external events — confirmed with log and trace evidence. Many alarms become one incident with one named cause. |
| **Self-healing** | Playbooks selected from the diagnosed cause: restart on resource exhaustion, circuit breakers on the callers of a failing dependency, rollback of a bad deployment, pre-emptive scale-out. Every action passes five safety rails and is verified against post-action telemetry. |
| **Operations dashboard** | React dashboard with live service health, anomalies, incidents, forecasts, the remediation log, and one-click chaos experiments. |
| **Kubernetes-native** | Runs on Kubernetes (kind locally, a single VM for a deployment). Healing uses the Kubernetes API: rolling restarts, scaling that respects an HPA, and a real `rollout undo`. |

## Verified end to end

`scripts/chaos_demo.py` injects each fault through the API gateway and measures the platform's
response. All three scenarios pass on Docker Compose and on Kubernetes, with no action taken on
any service that was not faulted.

| Scenario | Fault | Detect | Remediate | Diagnosed root cause | Action |
|---|---|---|---|---|---|
| Resource exhaustion | memory leak in `account-lookup-service` | 64 s | 76 s | the leaking service | rolling restart |
| Dependency failure | latency in `central-ledger` | 52 s | 64 s | `central-ledger`, not its callers | restart; circuit breakers on callers |
| Bad deployment | defective release of `ml-api-adapter` | 35 s | 48 s | the deployment (CHANGE, 0.94) | `rollout undo` to the previous revision |

*Kubernetes (kind) timings. Detection time includes the metrics sampling interval (15 s) and the
two-sample persistence requirement.*

---

## Architecture

```
 DPI workload (any system described by config/topology.*.json)
        │  metrics · logs · traces
        ▼
 Observability    Prometheus · Loki + Alloy · Tempo · Alertmanager · Grafana
        │  PromQL
        ▼
 metrics-agent ──► TimescaleDB (raw samples + 1 m / 1 h rollups)
        │
        │  Redis Streams: aiops.metrics → aiops.anomalies → aiops.incidents → aiops.actions
        ▼
 anomaly-detector    traffic-predictor    rca-engine    healing-engine
        │
        ▼
 api-gateway (REST + WebSocket) ──► dashboard (React)
```

Services communicate only through the event bus and their own tables, so each can be restarted,
scaled or replaced independently. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start

Requirements: Windows 10/11 with Docker Desktop (WSL 2), Python 3.11 or 3.12, Node.js 20, and
16 GB RAM. Full instructions, including Linux, are in [docs/SETUP.md](docs/SETUP.md).

```powershell
.\scripts\bootstrap.ps1                    # Python environment and .env
.\scripts\stack.ps1 up -Layer all          # the full stack on Docker Compose
.\.venv\Scripts\python.exe scripts\replay_history.py --from-csv ml\datasets\mojaloop_replay_28d.csv
Invoke-RestMethod -Method Post http://localhost:8021/train -ContentType application/json -Body '{}'
```

Then start the dashboard:

```powershell
cd dashboard; npm install; npm run dev     # http://localhost:5173
```

To enable automated remediation, set `HEALING_ENABLED=true` in `.env` and restart the healing
engine. Then run the chaos scorecard:

```powershell
.\.venv\Scripts\python.exe scripts\chaos_demo.py
```

To run on Kubernetes instead: `.\scripts\k8s.ps1 up` — see [docs/KUBERNETES.md](docs/KUBERNETES.md).

| Endpoint | URL |
|---|---|
| Dashboard | http://localhost:5173 (Compose) · http://localhost:8081 (Kubernetes) |
| API gateway | http://localhost:8080 — interactive API docs at `/docs` |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

---

## Documentation

| Document | Contents |
|---|---|
| [Setup](docs/SETUP.md) | Installing prerequisites, running the stack, troubleshooting |
| [Architecture](docs/ARCHITECTURE.md) | Layers, data flow, storage, event bus, design decisions |
| [Components](docs/COMPONENTS.md) | Each service: responsibility, inputs and outputs, API |
| [Configuration](docs/CONFIGURATION.md) | Environment variables and the DPI topology file |
| [Operations](docs/OPERATIONS.md) | Self-healing, safety rails, chaos experiments, incident lifecycle |
| [Data](docs/DATA.md) | Where the training data comes from and how it is generated |
| [Kubernetes](docs/KUBERNETES.md) | Running on kind; Kubernetes-native healing |
| [VM deployment](docs/DEPLOY_VM.md) | Deploying to a single cloud VM |
| [Contributing](CONTRIBUTING.md) | Development workflow and conventions |

## Repository layout

```
├── config/        Topology of the monitored DPI system
├── dashboard/     React + TypeScript operations dashboard
├── deploy/        Kubernetes manifests (kind) and single-VM setup
├── docs/          Documentation
├── infra/         Configuration for Prometheus, Grafana, Loki, Alloy, Tempo, Alertmanager, TimescaleDB
├── libs/          aiops_common: configuration, schemas, event bus, database, topology
├── ml/            Training dataset (28-day CSV) and model artifacts
├── scripts/       Stack control, data replay, verification, chaos scorecard
├── services/      The AIOps services and the DPI workload simulator
└── tests/         Unit and integration tests
```

## Technology

Python 3.11+ (FastAPI, scikit-learn, Pydantic) · TimescaleDB · Redis Streams · Prometheus ·
Grafana · Loki · Alloy · Tempo · OpenTelemetry · React 18 + TypeScript + Recharts · Docker Compose ·
Kubernetes

## License

[Apache 2.0](LICENSE)
