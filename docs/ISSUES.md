# Backlog — ready to file as GitHub issues

One issue per module. Copy a block into a new issue, or run
`.\scripts\create_issues.ps1` to file them all at once with the GitHub CLI.

Modules 1 and 8 are already done, so they are not listed here.

## Suggested labels

Create these once, under **Issues → Labels**:

| Label | Colour | Meaning |
|-------|--------|---------|
| `module` | `#0e8a16` | A numbered build task from docs/MODULES.md |
| `infra` | `#1d76db` | Infrastructure, monitoring, collection |
| `ml` | `#5319e7` | Models, training, evaluation |
| `automation` | `#b60205` | Remediation, chaos, orchestration |
| `frontend` | `#fbca04` | Dashboard |
| `good first issue` | `#7057ff` | Small, self-contained, safe to learn on |
| `blocked` | `#d93f0b` | Waiting on another issue |

## Role split from the project brief

| Person | Focus | Owns |
|--------|-------|------|
| Intern 18 — Infrastructure & Monitoring | Metrics collection, Kubernetes, Prometheus/Grafana, log aggregation | Modules 2, 3, 10 |
| Intern 19 — ML & Automation | Anomaly detection, traffic prediction, self-healing | Modules 4, 5, 6, 7, 9 |

If the group is larger than two, modules 7 and 9 split off cleanly, and the
`good first issue` items at the bottom are independent of everything else.

## Dependency order

```
      [2] observability + simulator   <- start here, everything needs telemetry
               |
      [3] metrics agent               <- fills the database
           /        \
    [4] anomaly   [5] forecast        <- can be built in parallel
          |            |
      [7] RCA          |
          \           /
        [6] self-healing              <- consumes both
               |
        [9] chaos demo                <- proves the whole loop
               |
       [10] Kubernetes (stretch)
```

Modules 4 and 5 can start before 3 is finished by using
`scripts/seed_demo_data.py --days 7` for training data.

---

## Issue 1

**Title:** `[Module 2] Observability plane + Mojaloop simulator`
**Labels:** `module`, `infra`

### Scope

Stand up the off-the-shelf observability stack and a workload that produces realistic
telemetry for it to collect.

- `docker-compose.observability.yml`: Prometheus, Grafana, Loki, Promtail, Tempo, Alertmanager
- `infra/prometheus/`: scrape config and recording rules for the DPI signals
- `infra/grafana/provisioning/`: datasources and a starter dashboard, provisioned from
  files so nobody has to click through the UI
- `services/mojaloop_sim/`: five simulated Mojaloop microservices (`ml-api-adapter`,
  `quoting-service`, `central-ledger`, `account-lookup-service`,
  `transaction-request-service`) that
  - expose a realistic `/metrics` endpoint: RED signals, saturation, settlement lag
  - generate traffic with daily **and** weekly seasonality, so module 5 has real signal
    to learn instead of noise
  - accept fault-injection commands over HTTP: added latency, error rate, memory growth,
    crash — these are the hooks module 9 drives
- Structured JSON logs shipped to Loki, traces to Tempo

### Explicitly out of scope

- Reading those metrics into TimescaleDB — that is module 3
- Any interpretation of the numbers

### Acceptance criteria

- [ ] `.\scripts\stack.ps1 up -Layer observability` brings the stack up healthy
- [ ] Prometheus `/targets` shows every simulator instance `UP`
- [ ] Grafana loads at :3000 with the datasource and starter dashboard already present,
      no manual setup
- [ ] The traffic chart shows a visible daily curve over 24h, not flat noise
- [ ] Simulator logs are queryable in Grafana Explore via Loki
- [ ] `POST` to a fault-injection endpoint visibly changes the metrics within one scrape
- [ ] `.\scripts\healthcheck.ps1` still green

### Files you will likely touch

```
docker-compose.observability.yml     (new)
infra/prometheus/, infra/grafana/, infra/loki/, infra/promtail/, infra/tempo/, infra/alertmanager/   (new)
services/mojaloop_sim/               (new)
scripts/healthcheck.ps1              (extend with observability checks)
docs/MODULES.md
```

### Notes

The simulator is the foundation for everything downstream, so the traffic model matters
more than the number of features. A believable daily and weekly rhythm plus injectable
faults is enough. `MOJALOOP_SIM_BASE_TPS` and `MOJALOOP_SIM_SEASONALITY` already exist in
`.env.example`, and `SimulatorSettings` is already in `aiops_common/config.py`.

---

## Issue 2

**Title:** `[Module 3] Metrics collection agent + DPI-specific exporter`
**Labels:** `module`, `infra`
**Depends on:** Module 2

### Scope

- `services/metrics_agent/`: on a timer, run a fixed set of PromQL queries, normalise the
  results into `MetricSample`, batch-write to TimescaleDB, publish to `aiops.metrics`
- The DPI-specific exporter: transaction processing time, verification latency, API success
  ratio, settlement lag — the business signals generic infra monitoring misses
- A backfill command, so modules 4 and 5 have history to train on immediately

### Explicitly out of scope

- Detection thresholds or scoring (module 4)
- Deciding anything based on a metric

### Acceptance criteria

- [ ] `SELECT count(*) FROM service_metrics` grows every scrape interval
- [ ] `service_metrics_1m` populates (respect the continuous-aggregate bucket rule — see
      the comment in `tests/test_integration_stack.py`)
- [ ] One row per service per instance per window; `error_ratio` is computed and stored
- [ ] Agent `/metrics` exposes collection latency, rows written, and an error counter
- [ ] Prometheus being briefly unreachable does not kill the agent
- [ ] Backfill command produces at least 7 days of plausible history
- [ ] Unit tests for the normalisation logic; integration test for the write path

### Files you will likely touch

```
services/metrics_agent/              (new)
docker-compose.aiops.yml             (new or extend)
docs/MODULES.md
```

### Notes

`PrometheusClient` in `aiops_common/promql.py` already does instant, range and
`scalar_by_label` queries with retries. `Database.execute_many()` is the batch write path.
This service is the **only** place that knows where metrics come from — everything
downstream must see a uniform `MetricSample`. That boundary is what makes the platform
portable to MOSIP and Beckn.

---

## Issue 3

**Title:** `[Module 4] Anomaly detection with Isolation Forest`
**Labels:** `module`, `ml`
**Depends on:** Module 3 (or seed data)

### Scope

- Per-service baselines: rolling mean and standard deviation, aware of hour-of-day
- `IsolationForest` over the `ANOMALY_FEATURES` vector, with a `StandardScaler`
- Normalise the model's native score to 0–1 and map to a `Severity`
- Feature attribution: which signals drove the score, and by how many standard deviations
- Classify the anomaly `kind` (`memory_pressure`, `latency_spike`, `error_burst`, ...) so
  module 6 can match a playbook
- Training job writing a row to `model_runs`, and a saved artifact under `ml/artifacts/`

### Explicitly out of scope

- Deciding what to *do* about an anomaly (module 6)
- Grouping anomalies into incidents (module 7)

### Acceptance criteria

- [ ] Consumes `aiops.metrics`, publishes to `aiops.anomalies`, persists to `anomalies`
- [ ] Injecting a fault via the simulator produces an anomaly within one scrape interval
- [ ] `kind` is correct for each of: memory leak, latency spike, error burst, traffic surge
- [ ] `contributing_features` names the signals a human would have pointed at
- [ ] `score` is in 0–1 and `is_actionable` respects the configured threshold
- [ ] Normal traffic produces a false-positive rate near `ANOMALY_CONTAMINATION`, measured
      and written up in the PR
- [ ] Training run recorded in `model_runs` with row count, duration and metrics
- [ ] Cold start is handled: no model yet must not crash the service

### Files you will likely touch

```
services/anomaly_detector/           (new)
ml/training/                         (training entry point)
docs/MODULES.md
```

### Notes

`ANOMALY_FEATURES` in `constants.py` is an **ordered, append-only** tuple and part of the
model contract — reordering it silently invalidates saved models. `MetricSample.feature_vector()`
projects a sample onto it. `AnomalyEvent` already carries `feature_snapshot`,
`feature_deviations` and `contributing_features`; the dashboard renders all three, so
populating them properly is visible work.

---

## Issue 4

**Title:** `[Module 5] Traffic prediction and pre-emptive scaling`
**Labels:** `module`, `ml`
**Depends on:** Module 3 (or seed data)

### Scope

- Forecast per-service TPS at 1, 6 and 24 hour horizons, training on `service_metrics_1h`
- Backtesting with MAPE recorded per model version, so improvements are provable
- Turn predicted peak TPS into a replica recommendation using per-replica capacity
- Publish `aiops.predictions`, persist to `traffic_predictions`

### Explicitly out of scope

- Executing a scale action (module 6 owns that)

### Acceptance criteria

- [ ] Forecast visibly tracks the simulator's known daily curve
- [ ] Recorded MAPE beats a seasonal-naive baseline, with both numbers in the PR
- [ ] A predicted peak yields `scale_out` **before** the spike, not during it
- [ ] Confidence bounds populated (`lower_bound` / `upper_bound` on `ForecastPoint`)
- [ ] Insufficient history raises `InsufficientDataError` rather than emitting a bad forecast
- [ ] Backtest is reproducible from a command, and recorded in `model_runs`

### Files you will likely touch

```
services/traffic_predictor/          (new)
ml/training/, ml/notebooks/          (model selection write-up)
docs/MODULES.md
```

### Notes

**Check library support for your Python version before committing to one.** Prophet and
some `statsmodels` builds lag on Python 3.13. Options that work well here: `statsmodels`
SARIMAX, a gradient-boosted regressor over lag and calendar features, or a
seasonal-naive-plus-trend baseline. A simple model with an honest backtest beats a complex
one you cannot evaluate — start with the baseline so you have something to beat.

`FORECAST_HORIZONS` is already parsed into `settings.ml.forecast_horizons_hours`.

---

## Issue 5

**Title:** `[Module 6] Self-healing playbook engine`
**Labels:** `module`, `automation`
**Depends on:** Modules 4 and 7

### Scope

- Playbooks: `restart_on_oom`, `circuit_break_cascade`, `rollback_bad_deploy`,
  `scale_out_preemptive`, `scale_in_idle`, `clear_queue_backlog`
- Four safety rails, each recording a distinct `skipped_*` status: master switch,
  per-target cooldown, hourly budget, minimum confidence
- An executor interface with a Docker implementation now and Kubernetes later
- Before/after metric snapshots on every action, so effectiveness is measurable

### Explicitly out of scope

- Determining the cause (module 7)
- Detecting the problem (module 4)

### Acceptance criteria

- [x] With `HEALING_ENABLED=false`, actions are recorded as `skipped_dry_run` and
      **nothing executes**
- [x] With it enabled, a memory-pressure anomaly restarts the container and
      `after_snapshot` shows recovery
- [x] Each rail has a unit test proving it blocks: cooldown, hourly budget,
      below-confidence, master switch
- [x] Every action is persisted to `healing_actions`, including skipped ones
- [x] Actions are idempotent — a duplicate anomaly does not double-restart
- [x] A failing playbook records `failed` with a useful `result_message`, and does not
      take the service down

### Files you will likely touch

```
services/healing_engine/             (new)
docs/MODULES.md
```

### Notes

**The safety rails are the deliverable, as much as the playbooks are.** A blocked action is
a *correct* outcome — that is why `ActionStatus` has four separate `skipped_*` values and
why they are persisted. Being able to show the brakes work is what makes automated
remediation defensible.

Keep the executor behind an interface. Module 10 swaps Docker for the Kubernetes API
(delete a pod, patch replicas, `rollout undo`) and nothing else should have to change.
`HealingSettings` and the Redis helpers `incr_with_ttl` / `set_flag` already exist for the
budget and cooldown state.

---

## Issue 6

**Title:** `[Module 7] Root cause analysis engine`
**Labels:** `module`, `ml`
**Depends on:** Module 4

### Scope

Correlate anomalies four ways and produce ranked, evidence-backed hypotheses:

- **Temporal** — group anomalies within a sliding window
- **Topological** — walk `MOJALOOP_DEPENDENCIES` upstream to find the origin
- **Change** — match against the `deployments` table
- **Event** — traffic surges, active chaos experiments
- Incident lifecycle: open, mitigate, resolve
- A plain-English summary for the dashboard (`ai_summary`)

### Explicitly out of scope

- Remediation (module 6)

### Acceptance criteria

- [x] A fault in `central-ledger` that cascades to `quoting-service` and `ml-api-adapter`
      produces **one** incident naming `central-ledger`, not three separate incidents
- [x] A deploy 10 minutes before an error burst yields a `change` hypothesis with the
      deployment as `target`
- [x] A traffic surge with no service fault yields an `event` hypothesis, not `service`
- [x] Hypotheses are ordered by confidence and every one carries concrete `evidence` strings
- [x] Anomalies are stamped with their `incident_id`
- [x] Incidents resolve automatically when the underlying anomalies clear

### Files you will likely touch

```
services/rca_engine/                 (new)
docs/MODULES.md
```

### Notes

The dependency graph in `constants.py` is already there and tested acyclic. Correlating
five simultaneous alarms into one incident with one named cause is the single most
demo-visible piece of intelligence in this project — the dashboard's incident page is
already built to display ranked hypotheses with their evidence, so good output shows up
immediately.

---

## Issue 7

**Title:** `[Module 9] Chaos engineering demo`
**Labels:** `module`, `automation`
**Depends on:** Modules 2, 4, 6, 7

### Scope

- `services/chaos_agent/`: inject `kill_service`, `memory_hog`, `cpu_hog`,
  `network_latency`, `error_injection`, `traffic_spike`, `bad_deploy`
- Wire it to the simulator's fault-injection endpoints and the Docker API
- Scorecard on each `chaos_runs` row: time-to-detect, time-to-RCA, time-to-remediate
- Scripted scenarios that run end to end unattended
- A written demo runbook

### Explicitly out of scope

- Detection or healing logic — chaos only breaks things and measures the response

### Acceptance criteria

- [ ] `.\scripts\demo.ps1` runs a full scenario unattended and prints the scorecard
- [ ] Each experiment is detected, attributed and remediated, with timings recorded
- [ ] Every experiment auto-reverts after `duration_seconds`, even if the agent crashes
- [ ] The dashboard's chaos page can start a run and shows it live
- [ ] `docs/DEMO.md` gives a runbook someone else can follow cold

### Files you will likely touch

```
services/chaos_agent/                (new)
scripts/demo.ps1                     (new)
docs/DEMO.md                         (new)
docs/MODULES.md
```

### Notes

This is the module that makes the project *demonstrable*, so budget time for it. The
`ChaosRun` schema, the `chaos_runs` table with its timing columns, and the dashboard's
chaos page with the scorecard all already exist and are exercised by the seed data — look
at what `seed_demo_data.py` produces for the `cpu_hog` run to see the target shape.

Auto-revert matters: an experiment that fails to clean up after itself leaves the platform
broken and wastes everyone's time.

---

## Issue 8

**Title:** `[Module 10] Kubernetes migration`
**Labels:** `module`, `infra`
**Depends on:** Modules 2–9
**Stretch goal**

### Scope

- A `kind` cluster definition for local Kubernetes
- Manifests or a Helm chart for the whole platform
- `metrics_agent` additionally reads `kube-state-metrics` and cAdvisor, so `replicas`,
  restart counts and OOMKill events come from the real thing
- Healing playbooks act through the Kubernetes API: pod deletion, HPA patching, `rollout undo`
- Real Mojaloop Helm deployment replaces the simulator

### Acceptance criteria

- [ ] One command creates the cluster and deploys the platform
- [ ] Healing playbooks work against real pods
- [ ] Predictive scaling drives a real HPA or replica patch
- [ ] Nothing in modules 4, 5, 7 or 8 needed changing — that is the point of the adapter
      boundary, and the PR should confirm it

### Notes

`docs/ARCHITECTURE.md` section 8 lists exactly what changes and what does not. If this
module needs edits to the models, correlation logic or dashboard, the abstraction leaked
and it is worth a discussion on the issue.

---

## Good first issues

Small, independent, and useful. Good for anyone joining mid-project or waiting on a
dependency.

### `Add a Makefile / task runner for the common commands`
**Labels:** `good first issue`
The check sequence in CONTRIBUTING.md section 5 is five commands. Wrap them as
`make check`, `make test`, `make up`, `make seed`. Keep the PowerShell scripts working —
Windows is the primary environment.

### `Dockerfiles for the API gateway and dashboard`
**Labels:** `good first issue`, `infra`
`services/api_gateway/` and `dashboard/` currently only run from source. Add multi-stage
Dockerfiles and a `docker-compose.aiops.yml` entry so the whole platform starts with one
command. Remember services inside Compose need `POSTGRES_HOST=timescaledb`, not
`localhost` — see the comment at the top of `.env.example`.

### `Frontend linting`
**Labels:** `good first issue`, `frontend`
`package.json` declares a `lint` script but ESLint is not configured. Add a flat config
for TypeScript and React, fix what it finds, and add the step to `.github/workflows/ci.yml`.

### `Loading and error states in the dashboard`
**Labels:** `good first issue`, `frontend`
Audit the seven pages for what renders while a request is in flight or after it fails.
An ops dashboard must never show a blank panel with no explanation — the same reasoning
as the stale-service bug recorded under module 8 in `docs/MODULES.md`.

### `Grafana dashboard for the AIOps services themselves`
**Labels:** `good first issue`, `infra`
Every service already exposes `aiops_http_requests_total`,
`aiops_http_request_duration_seconds`, `aiops_service_up` and `aiops_service_ready` via
`build_app()`. Nothing visualises them yet. Provision a dashboard so the platform can be
watched while it watches Mojaloop.
