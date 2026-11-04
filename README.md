# AIOps for DPI Infrastructure

**Predictive scaling and self-healing for Digital Public Infrastructure.**

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)

DPI systems serve millions of people and cannot afford downtime. When a payment switch
goes down, commerce stops. When an identity service fails, people lose access to
services they depend on.

Traditional monitoring is reactive — it tells you something *already* broke. This platform
is proactive: it forecasts load before the spike, flags anomalies before the outage,
explains the likely cause, and runs a remediation playbook on its own.

Built against **Mojaloop** (payments). The design generalises to **MOSIP** (identity) and
**Beckn** (commerce) by swapping one collection adapter.

> Project AI-7 · Category: AI-for-DPI · Bracket 2

---

## What it does

| Capability | How |
|---|---|
| **Predicts traffic** | Forecasts per-service load at 1 / 6 / 24 hour horizons and recommends a replica count *before* the peak arrives |
| **Detects anomalies** | Isolation Forest over a multivariate feature vector, with per-feature attribution — not static thresholds |
| **Explains root cause** | Correlates anomalies temporally, across the service dependency graph, against deployments, and against external events. Five alarms become **one** incident with **one** named cause |
| **Heals itself** | Playbooks for OOM restart, circuit breaking, deploy rollback and pre-emptive scaling, behind four independent safety rails |
| **Proves it works** | Chaos experiments with a scorecard: time-to-detect, time-to-RCA, time-to-remediate |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  DPI WORKLOAD — Mojaloop microservices                                    │
│  ml-api-adapter · quoting-service · central-ledger · account-lookup       │
└───────────┬──────────────────────────────────────────────────────────────┘
            │ metrics · logs · traces
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  OBSERVABILITY PLANE ✅                                                   │
│  Prometheus · Loki + Alloy · Tempo · Grafana · Alertmanager              │
└───────────┬──────────────────────────────────────────────────────────────┘
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AIOPS PLANE ✅                                                           │
│  metrics-agent ──► TimescaleDB (history + feature store)                 │
│        ├──► anomaly-detector      (Isolation Forest, 5-15s) ✅           │
│        ├──► traffic-predictor     (1h / 6h / 24h) ✅                     │
│        ├──► rca-engine            (service · change · event) ✅          │
│        └──► healing-engine        (playbooks + safety rails) ✅          │
└───────────┬──────────────────────────────────────────────────────────────┘
            │ Redis Streams
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE ✅                                                         │
│  api-gateway (REST + WebSocket) · dashboard (React) · chaos-agent ⏸️     │
└──────────────────────────────────────────────────────────────────────────┘
```

**Current Status (November 2026):**
- ✅ Modules 1-5, 6, 7, 8 complete and integrated
- ⏸️ Modules 9-10 planned
- 17 containers running, full end-to-end pipeline operational
- **Self-healing workflow verified**: Fault → Anomaly (5-15s) → Incident → Remediation

Layers only query downward and communicate upward through the event bus. No service
reaches into another's database. Full reasoning in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Running the project

Commands are PowerShell, on Windows. First time on this machine? **[docs/SETUP.md](docs/SETUP.md)**
walks through installing Docker Desktop and WSL 2 from nothing.

### Prerequisites

| Tool | Version | Check |
|---|---|---|
| Docker Desktop | any recent | `docker compose version` |
| Python | **3.11 or 3.12** | `python --version` |
| Node.js | 20 or newer | `node --version` |
| Git | any | `git --version` |

Docker Desktop must be **running** — wait for the tray whale to stop animating. Python
3.13 works for what exists today, but some ML wheels needed in module 5 still lag on it.

### Step 1 — One-time setup

```powershell
git clone https://github.com/vamsi-op/aiops-dpi-infrastructure.git
cd aiops-dpi-infrastructure

# If PowerShell refuses to run the scripts:
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

.\scripts\bootstrap.ps1
```

`bootstrap.ps1` creates `.venv`, installs dependencies, copies `.env.example` to `.env`,
and verifies the shared library imports. Safe to re-run; add `-Recreate` to start clean.

### Step 2 — Start the data layer

```powershell
.\scripts\stack.ps1 up          # TimescaleDB + Redis
.\scripts\healthcheck.ps1       # must end with "All checks passed"
```

The first run pulls ~2.8 GB of images and then applies the schema — give it about 30
seconds. `healthcheck.ps1` asserts 16 things about Docker, the schema, the hypertable, the
continuous aggregates and Redis, then runs the full test suite.

**If the healthcheck is not green, stop here and fix it.** Everything below assumes it passes.

### Step 3 — Start the full AIOps stack

```powershell
.\scripts\stack.ps1 up -Layer all
```

Builds and starts 16 containers: the five simulated Mojaloop services, Prometheus, Grafana, 
the metrics agent, anomaly detector, and traffic predictor. The first run builds images, so 
allow a few minutes.

- **Grafana** — http://localhost:3000 — opens straight on the DPI dashboard, no setup
- **Prometheus** — http://localhost:9090 — all 5 Mojaloop targets should be `UP`

Give it about 60 seconds for the `rate()` windows to fill, then verify each module:

```powershell
.\.venv\Scripts\python.exe scripts\check_observability.py
.\.venv\Scripts\python.exe scripts\check_metrics_agent.py
.\.venv\Scripts\python.exe scripts\check_anomaly_detector.py
.\.venv\Scripts\python.exe scripts\replay_history.py --days 28  # needed for module 5
.\.venv\Scripts\python.exe scripts\check_traffic_predictor.py
```

These verify that: targets are scraped, recording rules work, metrics flow to TimescaleDB,
anomaly detection beats the baseline on real data, and traffic forecasting beats seasonal-naive 
at every horizon.

Traffic follows a realistic daily and weekly rhythm, so over 24 hours the Grafana traffic
panel shows a clear curve. That seasonality is what module 5's forecasting learns.

### Step 4 — Load historical data for the dashboard

The live modules write real-time data, but the dashboard looks better with history. Seed it:

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --days 3
```

This writes ~21,000 metric rows plus 11 anomalies, 5 incidents, 8 healing actions, 15
forecasts and a chaos run.

### Step 5 — Start the dashboard app

Three terminals. Activate the venv in each with `.\.venv\Scripts\Activate.ps1`, or use the
full `.\.venv\Scripts\python.exe` path as shown.

```powershell
# Terminal 1 - API gateway
.\.venv\Scripts\python.exe -m uvicorn api_gateway.asgi:app --port 8000 --app-dir services\api_gateway
```

```powershell
# Terminal 2 - dashboard  (npm install only needed the first time)
cd dashboard
npm install
npm run dev
```

```powershell
# Terminal 3 - live data tail, keeps the health grid green
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --follow --skip-history
```

Terminal 3 is not optional for a demo: services are marked **stale** two minutes after
their last sample, so without it the grid goes grey. That is correct behaviour — a service
that stops reporting must be visible as stale, not silently vanish.

### Step 7 — Run the self-healing demo

The system now demonstrates full end-to-end self-healing. This demo injects a fault, waits for automatic detection, incident creation, and remediation:

```powershell
.\.venv\Scripts\python.exe scripts\demo_quick_healing.py
```

**What happens:**
1. **T+0s**: 50% error rate injected into `central-ledger`
2. **T+5-15s**: Anomaly detected (Isolation Forest)
3. **T+15-20s**: Incident created with root cause analysis
4. **T+20-25s**: Healing playbook executes (container restart)
5. **Complete**: Fault cleared, system restored

**Performance metrics:**
- Anomaly detection: **5-15 seconds** (optimized from 40-60s)
- End-to-end recovery: **~25 seconds** from injection to remediation
- Zero consumer group backlog issues

The demo output shows the anomaly ID, incident ID, severity, and timeline. Check the dashboard **Incidents** page to see the full RCA with hypotheses and evidence.

### Step 8 — Verify healing actions

Check that healing actions executed:

```powershell
# View healing engine logs
docker logs aiops-healing-engine --tail 50

# Check RCA engine incident creation
docker logs aiops-rca-engine --tail 30 | Select-String "incident_created"

# Verify playbook execution (look for "Restarted" messages)
docker logs aiops-healing-engine | Select-String "Restarted|execute_start"
```

Healing actions execute when:
- Master switch `HEALING_ENABLED=true` (set in `.env`)
- Cooldown period expired (15 seconds between actions on same target)
- Hourly budget not exceeded (max 10 actions per hour)
- Confidence threshold met (>= 0.7)

Blocked actions are logged with `skipped_*` status codes proving the safety rails work.

### Step 9 — Open the dashboard

**http://localhost:5173**

| Page | What you should see |
|---|---|
| **Overview** | 5 Mojaloop services, all `healthy`, live TPS and error ratios |
| **Service detail** | Per-service metric history, its anomalies, forecasts and actions |
| **Anomalies** | Timeline with per-feature attribution and deviation in sigma |
| **Forecast** | Actual vs predicted traffic with the scaling decision overlaid |
| **Incidents** | 5 incidents, each with ranked root-cause hypotheses and evidence |
| **Automation** | Healing actions, including the `skipped_*` ones proving the rails work |
| **Chaos** | The seeded `cpu_hog` run with its detect/remediate scorecard |

### Ports

| Service | URL | Notes |
|---|---|---|
| Dashboard | http://localhost:5173 | Vite dev server, proxies `/api` to :8000 |
| API gateway | http://localhost:8000 | `/docs` for interactive OpenAPI |
| Grafana | http://localhost:3000 | Provisioned DPI dashboard, admin/admin |
| Prometheus | http://localhost:9090 | `/targets` and `/rules` |
| TimescaleDB | localhost:5432 | user/db `aiops` |
| Redis | localhost:6379 | event bus |
| Metrics agent | http://localhost:8020 | Collection and rollup |
| Anomaly detector | http://localhost:8021 | Isolation Forest scoring |
| Traffic predictor | http://localhost:8022 | 1h / 6h / 24h forecasts |
| Simulated services | localhost:8011–8015 | `/sim`, `/metrics`, `/docs` per service |

Simulated service ports: `ml-api-adapter` 8011, `central-ledger` 8012, `quoting-service`
8013, `account-lookup-service` 8014, `transaction-request-service` 8015.

Change `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env` if those clash with something
you already run.

### Break something on purpose

The simulator accepts fault injection, which is how modules 4, 6 and 9 are developed and
demonstrated. Every fault **expires automatically** (30-minute hard cap), so nothing can be
left broken.

```powershell
# Add 500 ms of latency to central-ledger for 60 seconds
curl.exe -X POST http://localhost:8012/sim/faults -H "Content-Type: application/json" `
  -d '{\"kind\":\"network_latency\",\"magnitude\":0.5,\"duration_seconds\":60}'

# Watch the effect
curl.exe http://localhost:8012/sim

# Clear everything early
curl.exe -X DELETE http://localhost:8012/sim/faults
```

| Fault kind | What it simulates |
|---|---|
| `network_latency` | Slow downstream dependency |
| `error_injection` | Failing requests |
| `memory_hog` | Memory leak — a monotonic climb, not a spike |
| `cpu_hog` | CPU saturation |
| `traffic_spike` | Demand surge with a smooth rise and fall |
| `queue_backlog` | Work piling up |
| `settlement_delay` | Payments-specific: settlement falling behind |
| `bad_deploy` | Composite: errors + latency + queue together |
| `kill_service` | Terminates the process; the container restarts |

Scaling is also live, and it genuinely relieves pressure — capacity is
`tps_per_replica x replicas`, and every signal derives from `tps / capacity`:

```powershell
curl.exe -X POST http://localhost:8012/sim/scale -H "Content-Type: application/json" `
  -d '{\"replicas\":8,\"reason\":\"manual test\"}'
```

On `central-ledger`, 1 replica gives load 1.26 / p99 56 s / 100% errors; 8 replicas gives
load 0.16 / p99 0.21 s / 0.2% errors. That is what lets a remediation be *shown* to have
worked rather than merely logged.

### Verify it end to end

```powershell
.\.venv\Scripts\python.exe scripts\check_observability.py  # targets, rules, dashboards
.\.venv\Scripts\python.exe scripts\ws_smoke.py             # Redis -> gateway -> browser socket
.\.venv\Scripts\python.exe scripts\check_api_contract.py   # UI types vs live API responses
```

The first proves live fan-out works; every REST endpoint reads from the database, so they
would all pass even with WebSockets broken. The second catches `types.ts` drifting from the
Python models, which `tsc` and `pytest` both miss and which renders as an empty card.

### Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q        # unit only, no Docker needed

$env:AIOPS_RUN_INTEGRATION = '1'                     # + real DB and Redis
.\.venv\Scripts\python.exe -m pytest tests -q
```

### Stop

```powershell
Ctrl+C in each terminal

.\scripts\stack.ps1 down        # stop containers, keep data
.\scripts\stack.ps1 reset       # DESTROY data and re-apply the schema
```

Use `reset` after changing anything in `infra/timescaledb/init/` — those scripts only run
against an empty volume.

### If something breaks

| Symptom | Fix |
|---|---|
| `running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `error during connect` from docker | Docker Desktop is not running |
| `port is already allocated` | Change `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env` |
| Healthcheck says tables are missing | `.\scripts\stack.ps1 reset` |
| Dashboard loads but every panel is empty | API gateway not running, or no seed data |
| Services all show `stale` | Start the terminal 3 tail |

More detail in **[docs/SETUP.md](docs/SETUP.md)** section 10.

### About the seeded data

`seed_demo_data.py` generates a week of Mojaloop telemetry with a realistic daily and
weekly rhythm, plus five scripted failure scenarios: memory leak, bad deploy, traffic
surge, a cascade with a single upstream cause, and a chaos run with a scorecard.

It is a stand-in, not a fake: the traffic shape matches what the module 2 simulator will
produce, and the five scenarios are exactly the cases modules 4 and 7 must handle. The
anomalies and incidents it writes were **not** produced by real detection — that is what
modules 4 and 7 build. Once they exist, they write to the same tables and streams and the
dashboard needs no changes.

To clear it: `.\.venv\Scripts\python.exe scripts\seed_demo_data.py --clear`

---

## Repository layout

```
├── libs/aiops_common/       Shared foundation: config, logging, schemas, event bus, DB
├── infra/                   Config for every off-the-shelf component
│   └── timescaledb/init/    Schema: tables, hypertable, continuous aggregates
├── services/                One directory per AIOps microservice
│   └── api_gateway/         REST + WebSocket backing the dashboard
├── dashboard/               React + TypeScript operations dashboard
├── ml/                      Training scripts, notebooks, model artifacts
├── scripts/                 Bootstrap, stack control, healthcheck, seed, verification
├── tests/                   Unit + integration tests
└── docs/                    SETUP · ARCHITECTURE · MODULES · ISSUES
```

---

## Module status

The project is built in independently verifiable modules. Each one ends with a command
that proves it works.

| # | Module | Focus | Status |
|---|--------|-------|--------|
| 1 | Project foundation — config, logging, schemas, event bus, storage | infra | ✅ done |
| 2 | Observability plane + Mojaloop simulator | infra | ✅ done |
| 3 | Metrics collection agent + DPI exporter | infra | ✅ done |
| 4 | Anomaly detection (Isolation Forest) | ml | ✅ done |
| 5 | Traffic prediction + pre-emptive scaling | ml | ✅ done |
| 6 | Self-healing playbook engine | automation | ✅ done |
| 7 | Root cause analysis engine | ml | ✅ done |
| 8 | Operations dashboard (React + FastAPI) | frontend | ✅ done |
| 9 | Chaos engineering demo | automation | ⬜ |
| 10 | Kubernetes migration | infra | ⬜ stretch |

Module 8 was built ahead of 2–7 deliberately, against the real schema and event bus. When
the upstream modules land they write to the same tables and streams, so the dashboard
needs no changes.

Detail per module — what it owns, what it does *not* own, and how it is verified — is in
**[docs/MODULES.md](docs/MODULES.md)**.

---

## Contributing

Read **[CONTRIBUTING.md](CONTRIBUTING.md)** first. The short version:

1. Claim an open issue labelled `module`.
2. Branch: `module-N-short-description`.
3. Build it. Cross module boundaries only through `schemas.py`, `constants.py`, and the
   database — never by importing another service.
4. Run the checks, open a PR.

```powershell
ruff format .; ruff check . --fix; mypy libs services; pytest tests -q
```

**[docs/ISSUES.md](docs/ISSUES.md)** holds the ready-to-file backlog. To create all of it
on GitHub at once:

```powershell
.\scripts\create_issues.ps1 -DryRun   # preview
.\scripts\create_issues.ps1           # create labels + 13 issues
```

### Team split

| Role | Modules |
|---|---|
| Infrastructure & Monitoring | 2, 3, 10 |
| ML & Automation | 4, 5, 6, 7, 9 |

Modules 7 and 9 split off cleanly if the group is larger, and `docs/ISSUES.md` lists
independent `good first issue` tasks.

---

## Design decisions worth knowing

- **Automation is off by default.** `HEALING_ENABLED=false`. Every playbook passes four
  rails — master switch, per-target cooldown, hourly budget, minimum confidence — and a
  blocked action is recorded with a `skipped_*` status. Being able to show the brakes work
  is what makes auto-remediation defensible.
- **One `MetricSample` shape for everything.** Only the collection agent knows where a
  number came from. That boundary is what makes the platform portable across Mojaloop,
  MOSIP and Beckn.
- **`ANOMALY_FEATURES` is append-only.** Reordering it silently invalidates every trained
  model.
- **The platform monitors itself.** Every service exposes `/health`, `/ready` and
  `/metrics` through one shared factory.
- **Timezone-aware UTC everywhere**, enforced by lint.

---

## Licence

[Apache 2.0](LICENSE) — matching the Mojaloop, MOSIP and Beckn ecosystem.
