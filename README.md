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

## Quick start

Prerequisites: **Docker Desktop**, **Python 3.11 or 3.12**, **Node 20+**.
First time on Windows? **[docs/SETUP.md](docs/SETUP.md)** covers Docker and WSL 2 from
scratch.

```powershell
git clone <repo-url>
cd Project

.\scripts\bootstrap.ps1        # venv, dependencies, .env
.\scripts\stack.ps1 up         # TimescaleDB + Redis
.\scripts\healthcheck.ps1      # must print "All checks passed"
```

### See the dashboard

Modules 2–7 are not built yet, so seed the database with representative data:

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --days 3
```

Then, in three terminals:

```powershell
# 1 - API
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000 --app-dir services\api_gateway

# 2 - dashboard
cd dashboard; npm install; npm run dev          # http://localhost:5173

# 3 - keep the health grid live
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --follow --skip-history
```

The seed produces a week of Mojaloop telemetry with a realistic daily and weekly rhythm,
plus five scripted failure scenarios: memory leak, bad deploy, traffic surge, a cascade
with a single upstream cause, and a chaos run with a detect/remediate scorecard. Those are
exactly the cases modules 4 and 7 have to handle.

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
