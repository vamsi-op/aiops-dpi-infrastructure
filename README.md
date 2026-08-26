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
│  OBSERVABILITY PLANE                                                      │
│  Prometheus · Loki + Promtail · Tempo · Grafana · Alertmanager             │
└───────────┬──────────────────────────────────────────────────────────────┘
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AIOPS PLANE                                                              │
│  metrics-agent ──► TimescaleDB (history + feature store)                   │
│        ├──► anomaly-detector      (Isolation Forest)                       │
│        ├──► traffic-predictor     (1h / 6h / 24h)                          │
│        ├──► rca-engine            (service · change · event)               │
│        └──► healing-engine        (playbooks + safety rails)               │
└───────────┬──────────────────────────────────────────────────────────────┘
            │ Redis Streams
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE                                                            │
│  api-gateway (REST + WebSocket) · dashboard (React) · chaos-agent          │
└──────────────────────────────────────────────────────────────────────────┘
```

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

### Step 3 — Load data

Modules 2–7 do not exist yet, so nothing is producing telemetry. Seed it:

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --days 3
```

This writes ~21,000 metric rows plus 11 anomalies, 5 incidents, 8 healing actions, 15
forecasts and a chaos run.

### Step 4 — Start the app

Three terminals. Activate the venv in each with `.\.venv\Scripts\Activate.ps1`, or use the
full `.\.venv\Scripts\python.exe` path as shown.

```powershell
# Terminal 1 - API gateway
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000 --app-dir services\api_gateway
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

### Step 5 — Open it

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
| TimescaleDB | localhost:5432 | user/db `aiops` |
| Redis | localhost:6379 | event bus |

Change `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env` if those clash with something
you already run.

### Verify it end to end

```powershell
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
| 2 | Observability plane + Mojaloop simulator | infra | ⬜ **start here** |
| 3 | Metrics collection agent + DPI exporter | infra | ⬜ |
| 4 | Anomaly detection (Isolation Forest) | ml | ⬜ |
| 5 | Traffic prediction + pre-emptive scaling | ml | ⬜ |
| 6 | Self-healing playbook engine | automation | ⬜ |
| 7 | Root cause analysis engine | ml | ⬜ |
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
