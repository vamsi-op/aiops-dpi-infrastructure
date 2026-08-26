# Module plan and progress

Each module is independently verifiable: when it is done, there is a command you can run
that proves it works. Nothing is marked complete on the strength of "the code is written".

---

## Module 1 — Project foundation ✅

**Delivered**

| Path | What it is |
|------|-----------|
| `libs/aiops_common/` | Shared Python package: typed config, structured logging, canonical names, data contracts, Redis Streams bus, TimescaleDB client, Prometheus client, FastAPI factory |
| `infra/timescaledb/init/` | Schema: 8 tables, `service_metrics` hypertable, 1m + 1h continuous aggregates, compression, retention |
| `docker-compose.yml` | Data layer: TimescaleDB 2.17 / PG16 + Redis 7.4, both health-checked |
| `scripts/bootstrap.ps1` | One-command developer setup |
| `scripts/stack.ps1` | Compose wrapper with layered profiles |
| `scripts/healthcheck.ps1` | 20-odd assertions across Docker, schema, Redis and Python |
| `tests/` | 65 unit tests (config, schemas, logging, event envelope, service factory) + 11 integration tests that hit the real database and event bus |
| `docs/SETUP.md` | Zero-to-running guide including Docker/WSL 2 install |
| `docs/ARCHITECTURE.md` | Design and the reasoning behind it |

**Verify** — confirmed working on Docker 29.7.2 / TimescaleDB 2.17.2 / Python 3.13.4

```powershell
.\scripts\bootstrap.ps1
.\scripts\stack.ps1 up
.\scripts\healthcheck.ps1        # 20 infra assertions + all 76 tests
```

**Design notes**

- `extra="forbid"` on every schema: a typo in an event payload fails at the boundary
  rather than silently dropping a field the model expected. A base validator strips
  computed fields on the way back in, so bus round trips still work.
- `ANOMALY_FEATURES` is an ordered tuple and part of the model contract. Append only.
  An integration test asserts every feature name maps to a real column.
- `HEALING_ENABLED=false` by default. Automated remediation is opt-in.

**Things that bit us, recorded so they don't bite again**

| Problem | Fix |
|---------|-----|
| Importing the library configured logging before a service could name itself | Service name resolved at log time, not captured at configure time |
| `model_dump()` output failed re-validation under `extra="forbid"` | Base validator drops known computed field names |
| `percentile_disc` is illegal inside a continuous aggregate | Removed; only parallelisable aggregates there |
| `create_hypertable` positional form is overload-ambiguous | Use `by_range()`, and no space dimension on a single node |
| Em dashes in `.ps1` files | Windows PowerShell 5.1 reads BOM-less UTF-8 as CP1252, turning them into smart quotes it treats as string delimiters. Scripts are ASCII-only. |
| `asyncpg` reports no row count for `executemany` | `execute_many()` returns the number of parameter sets submitted |
| `refresh_continuous_aggregate` cannot run in a transaction, and its window args are polymorphic | `call_procedure()` autocommit path + explicit `CAST(... AS timestamptz)` |

---

## Module 2 — Observability plane + Mojaloop simulator ⬜ next

**Will deliver**

- `docker-compose.observability.yml`: Prometheus, Grafana, Loki, Promtail, Tempo, Alertmanager
- Prometheus scrape config and recording rules for DPI signals
- Grafana provisioned with datasources and a starter dashboard (no clicking through UIs)
- `services/mojaloop_sim/`: five simulated Mojaloop microservices that
  - expose realistic `/metrics` (RED + saturation + settlement lag)
  - generate traffic with daily seasonality and a weekly pattern, so forecasting has
    genuine signal to learn instead of noise
  - accept fault-injection commands (latency, errors, memory growth, crash) — the hooks
    module 9 drives
- Structured JSON logs shipped to Loki, traces to Tempo

**Verify**: Prometheus targets all `UP`, Grafana shows live traffic with a visible daily
curve, `logcli`/Grafana Explore returns simulator logs.

---

## Module 3 — Metrics collection agent ⬜

- `services/metrics_agent/`: scheduled PromQL collection, normalisation into
  `MetricSample`, batch write to TimescaleDB, publish to `aiops.metrics`
- The DPI-specific exporter: transaction processing time, verification latency, API
  success ratio, settlement lag
- Backfill command so the models have history to train on immediately

**Verify**: `SELECT count(*) FROM service_metrics` grows every 15s; `service_metrics_1m`
populates; agent `/metrics` shows collection latency and error counters.

---

## Module 4 — Anomaly detection ⬜

- Per-service baseline statistics (rolling mean/σ, hour-of-day aware)
- Isolation Forest on the `ANOMALY_FEATURES` vector, with `StandardScaler`
- Score normalisation to 0–1 and severity mapping
- Feature attribution: which signals drove the score, and by how many σ
- Anomaly *kind* classification so playbooks can be matched
- Training job + model registry entry in `model_runs`

**Verify**: inject a fault with the simulator, see an anomaly appear on `aiops.anomalies`
within one scrape interval, with the right `kind` and sensible contributing features.

---

## Module 5 — Traffic prediction and pre-emptive scaling ⬜

- Forecasting on the 1-hour rollup for 1 / 6 / 24 hour horizons
- Backtesting with MAPE recorded per model version, so we can prove it beats naive
- Replica recommendation from predicted peak TPS and per-replica capacity
- Publishes `aiops.predictions`; the healing engine turns a recommendation into a scale

**Verify**: forecast tracks the simulator's known daily curve; recorded MAPE beats a
seasonal-naive baseline; a predicted peak produces a `scale_out` recommendation ahead of
the spike.

---

## Module 6 — Self-healing playbook engine ⬜

- Playbooks: `restart_on_oom`, `circuit_break_cascade`, `rollback_bad_deploy`,
  `scale_out_preemptive`, `scale_in_idle`, `clear_queue_backlog`
- Safety rails: master switch, per-target cooldown, hourly budget, minimum confidence
- Docker adapter now, Kubernetes adapter later, behind one interface
- Before/after metric snapshots on every action, so effectiveness is measurable

**Verify**: with `HEALING_ENABLED=false` actions are recorded as `skipped_dry_run`; with it
true, a memory-pressure anomaly restarts the container and the after-snapshot shows
recovery. Rails are covered by unit tests.

---

## Module 7 — Root cause analysis ⬜

- Temporal correlation: group anomalies inside a sliding window
- Topological correlation: walk `MOJALOOP_DEPENDENCIES` upstream to find the origin
- Change correlation: match against the `deployments` table
- Event correlation: traffic surges and active chaos experiments
- Ranked hypotheses with explicit evidence strings, plus a plain-English summary

**Verify**: a fault in `central-ledger` that cascades to `quoting-service` and
`ml-api-adapter` produces **one** incident naming `central-ledger` as the cause, not three
separate incidents.

---

## Module 8 — Operations dashboard ✅ (built ahead of modules 2-7)

Built out of order, at request. The UI is written against the real schema and the real
event bus; `scripts/seed_demo_data.py` stands in for the services that will eventually
feed it. When modules 2-7 land they write to the same tables and streams, so the
dashboard needs no changes.

**Delivered**

| Path | What it is |
|------|-----------|
| `services/api_gateway/` | FastAPI: 17 REST endpoints + `/ws`. `repository.py` holds all SQL, `health_rules.py` the status thresholds, `live.py` the Redis-to-WebSocket relay with per-service metric batching |
| `dashboard/` | React 18 + TypeScript + Vite. 7 pages: Overview, Service detail, Anomalies, Forecast, Incidents, Automation, Chaos |
| `scripts/seed_demo_data.py` | A week of Mojaloop telemetry with daily/weekly rhythm, plus 5 scripted failure scenarios (memory leak, bad deploy, traffic surge, cascade, chaos run) |
| `scripts/ws_smoke.py` | Proves the live path: publishes to Redis, asserts it arrives at the WebSocket |
| `scripts/check_api_contract.py` | Parses `types.ts` and checks all 20 interfaces against live API responses |
| `tests/test_gateway.py` | 25 tests over health rules, repository shaping and route behaviour |

**Verify**

```powershell
.\scripts\stack.ps1 up
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --days 3

# terminal 1 - API
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000 --app-dir services\api_gateway
# terminal 2 - UI
cd dashboard; npm install; npm run dev            # http://localhost:5173
# terminal 3 - keep the health grid live
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --follow --skip-history

.\.venv\Scripts\python.exe scripts\ws_smoke.py            # live relay works
.\.venv\Scripts\python.exe scripts\check_api_contract.py  # UI/API contract holds
```

**Why the two extra verification scripts exist.** `tsc` checks the frontend against
`types.ts`, and pytest checks the backend against its Pydantic models, but nothing checked
that those two agree - a field renamed on one side passes both and renders an empty card
in the browser. Separately, every HTTP endpoint reads from the database, so they would all
pass even with WebSocket fan-out completely broken. Both blind spots are now covered.

**Two real bugs this found**

| Problem | Cause and fix |
|---------|---------------|
| Incidents and healing actions randomly never reached the browser | `XREAD`'s `$` is re-evaluated as "stream end" on *every* call, and `XREAD` returns as soon as *any* stream has data. A stream that had not produced yet kept its `$`, so anything published to it between two calls fell into the gap and was silently lost. `EventBus.tail()` now resolves every cursor to a concrete message id once, before the first read. Two regression tests cover it. |
| The dashboard went blank ~10 minutes after telemetry stopped | The 10-minute query window doubled as the service *discovery* window, so services vanished instead of being marked stale. For an ops tool that is the worst possible failure mode: a total outage renders identically to "nothing wrong". Discovery is now 24h, with a 120s instance grace so long-retired replicas cannot inflate summed traffic. |

---

## Module 9 — Chaos engineering demo ⬜

- Experiments: kill service, memory hog, CPU hog, network latency, error injection,
  traffic spike, bad deploy
- Scripted demo scenarios that run end to end unattended
- Scorecard: time-to-detect, time-to-RCA, time-to-remediate, per experiment
- A written demo runbook

**Verify**: `.\scripts\demo.ps1` runs a scenario start to finish and prints the scorecard.

---

## Module 10 — Kubernetes migration ⬜ (stretch)

- `kind` cluster definition
- Kubernetes manifests / Helm chart for the whole platform
- `metrics-agent` reads `kube-state-metrics` and cAdvisor
- Healing playbooks act through the Kubernetes API: pod deletion, HPA patching,
  `rollout undo`
- Real Mojaloop Helm deployment replaces the simulator

---

## Conventions we hold to

1. **Every module ends with a verification command.** No "trust me".
2. **Config through `get_settings()`.** No stray `os.getenv`.
3. **Timezone-aware UTC everywhere.** `utc_now()`, never `datetime.now()`.
4. **Contracts in `schemas.py`, names in `constants.py`.** If two services must agree on
   something, it lives in the shared library.
5. **Comments explain *why*.** The code already says what.
6. **Safety rails are tested like features.** A blocked action is a correct outcome.
