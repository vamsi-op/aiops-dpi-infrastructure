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

## Module 2 — Observability plane + Mojaloop simulator ✅

### Part A ✅ — simulator, Prometheus, Grafana

| Path | What it is |
|------|-----------|
| `services/mojaloop_sim/` | Five simulated Mojaloop services. One image, `SIM_SERVICE` selects the profile, so each runs as a **real container** that module 6 can restart and module 9 can kill |
| `mojaloop_sim/profiles.py` | Per-service characteristics. `traffic_share` is derived from the dependency graph, so fan-in is real: central-ledger carries 1.78x the platform base |
| `mojaloop_sim/traffic.py` | Two-peak daily curve x weekday weighting, ~2.7x trough-to-peak, noise capped at 12%. Latency, CPU, memory, errors and queue depth are **all derived from one load factor** |
| `mojaloop_sim/faults.py` | 9 injectable faults, all auto-expiring, 30-minute hard cap |
| `mojaloop_sim/metrics.py` | Prometheus exporter using `DpiMetric` names imported from the shared library |
| `infra/prometheus/` | Scrape config + 20 recording rules for the DPI signals |
| `infra/grafana/` | Provisioned datasource + a 13-panel dashboard, no UI clicking |
| `scripts/check_observability.py` | Targets, recording rules, exporter-vs-simulator agreement, and **every dashboard panel returns data** |
| `tests/test_simulator.py` | 37 tests |

**Verify**

```powershell
.\scripts\stack.ps1 up -Layer observability
.\.venv\Scripts\python.exe scripts\check_observability.py
```

Grafana: http://localhost:3000 (lands on the DPI dashboard). Prometheus: http://localhost:9090.

**Verified working:** 9 containers healthy · 5/5 targets scraped · all 20 recording rules
return data · exporter within 3% of the simulator · all 13 panels populated.

### The two properties everything downstream depends on

**1. Scaling genuinely relieves pressure.** Capacity is `tps_per_replica * replicas`, and
every signal derives from `tps / capacity`. Measured on central-ledger:

| Replicas | Load | p99 | Errors | Queue | Settlement lag |
|---|---|---|---|---|---|
| 1 | 1.26 | 56.6 s | 100% | 1258 | 116 s |
| 8 | 0.16 | 0.21 s | 0.2% | 0 | 1.2 s |

Without this, module 5's forecasts and module 6's scaling playbooks would be numbers on a
dashboard with no observable consequence, and no remediation could ever be *shown* to have
worked.

**2. Signals are correlated, not independent.** An Isolation Forest over independent random
columns learns nothing. Because everything keys off one load factor, module 4 can detect
"latency is high *for this traffic level*" - which is the actual job.

### Fault injection, verified end to end

```
POST /sim/faults  {"kind": "network_latency", "magnitude": 0.5, "duration_seconds": 60}
```

| Fault | Measured effect |
|---|---|
| `network_latency` | p99 0.22 s -> 2.02 s |
| `error_injection` | error ratio 0.002 -> 0.202 |
| `memory_hog` | monotonic climb 0.60 -> 0.70 over 30 s (a trend, not a spike) |
| `cpu_hog` (8 s) | auto-expired after 11 s, other faults untouched |
| `kill_service` | container RestartCount 0 -> 1, back to healthy |

`bad_deploy` is deliberately a **composite** - errors *and* latency *and* queue depth
starting together, which is what module 7 must attribute to a `change` rather than to load.

### Design notes worth knowing

- **Five containers, not one process.** Module 6's `restart_on_oom` needs a real container
  to restart; module 9's `kill_service` needs a real one to kill.
- **No Docker `HEALTHCHECK` restart on the simulators.** An auto-restart would silently
  repair exactly the failures module 9 injects and module 6 is meant to remediate.
- **`honor_labels: true` on the Mojaloop scrape job.** Every `dpi_*` series carries its own
  `service` label; without this Prometheus renames it to `exported_service` and you end up
  with two label names for one concept. With it on, `service` means the same thing
  everywhere, including `up{service="central-ledger"}`.
- **Recording rules, not ad-hoc PromQL.** Module 3 asks for `dpi:tps:rate5m` so the agent,
  the dashboards and any alerts all read the identical computation.
- **Faults expire on read, not on a timer.** There is no background task that could die and
  leave a fault stuck on.
- **Counters increment by the fractional rate each tick**, so `rate()` recovers the exact
  offered load. Verified: scraped rate is within 3% of the simulator's own figure.

### Part B ✅ — logs, traces, alerting

| Path | What it is |
|------|-----------|
| `infra/loki/` | Loki 3.7, single binary on the filesystem. 7-day retention, structured metadata enabled |
| `infra/alloy/config.alloy` | Grafana Alloy: Docker log discovery → JSON parse → Loki. **Replaces Promtail** |
| `infra/tempo/` | Tempo 2.10, OTLP on 4317/4318, plus the metrics generator that derives span metrics and a service graph and remote-writes them to Prometheus |
| `infra/alertmanager/` | Alertmanager 0.28 with 3 inhibit rules. Notifications deliberately go nowhere |
| `infra/prometheus/rules/dpi_alerts.yml` | 12 alert rules, every one reading a `dpi:*` recording rule rather than recomputing |
| `aiops_common/tracing.py` | Shared OpenTelemetry setup, so every service emits spans with identical resource attributes |
| `aiops_common/log.py` | `_add_trace_context` stamps `trace_id`/`span_id` on every line inside a span — this is what makes log→trace navigation work |
| `tests/test_tracing.py` | 19 tests |

**Verify** — confirmed working on Docker 29.7.2

```powershell
.\scripts\stack.ps1 up -Layer observability
.\.venv\Scripts\python.exe scripts\check_observability.py
```

**Verified working:** 13 containers · 10/10 Prometheus targets across 3 jobs · logs from all
5 services in Loki with `service`/`level` labels · a request traced end to end and fetched
back from Tempo **by id** with all 3 expected spans · 118 span-derived series in Prometheus
across 12 metric names · 1 service-graph edge (`user -> central-ledger`) · all 12 alert
rules loaded and Alertmanager registered.

That edge count is the useful number. Before `/sim` was excluded from tracing there were
five — one per service — created purely by `check_observability.py` reading their state, and
305 span-derived series instead of 118. The graph now reflects workload only.

Loki: http://localhost:3100 · Tempo: http://localhost:3200 · Alertmanager:
http://localhost:9093 · Alloy: http://localhost:12345

### Why Alloy instead of Promtail

The original plan named Promtail. **Promtail reached end of life on 2 March 2026** — no
updates, no security fixes — and Grafana directs all further development to Alloy
([migration guide](https://grafana.com/docs/grafana-cloud/send-data/alloy/tasks/migrate/from-promtail/)).
Starting a new platform on an unmaintained collector would be choosing technical debt on
day one.

Alloy also reads the Docker daemon rather than tailing files under
`/var/lib/docker/containers`. On Docker Desktop that path lives inside the WSL 2 VM and is
not reliably bind-mountable from Windows, so file tailing works on Linux and silently
collects nothing here.

### The two properties Part B adds

**1. A log line knows which trace it belongs to.** `aiops_common.log` stamps `trace_id` on
every line emitted inside a span, Alloy promotes it to structured metadata, and the Loki
datasource turns it into a link. Without it, logs and traces are two collections covering
the same period that a human lines up by eye — exactly the manual work the platform exists
to remove.

**2. The topology is measured, not just declared.** Module 7 correlates anomalies by
walking `MOJALOOP_DEPENDENCIES`, a hand-maintained constant. Tempo's
`traces_service_graph_request_total` is the same graph observed on the wire, so the
declared topology becomes checkable rather than trusted. Today every edge is
`user -> <service>`; service-to-service edges appear once module 9 makes the simulator fan
out to its peers.

### Design notes worth knowing

- **Only `service` and `level` become Loki labels.** `event` is tempting — "give me every
  `txn.failure_rate_high`" is exactly the RCA query — but labels are Loki's index and every
  distinct value is a new stream. Simulator event names are bounded, but uvicorn and every
  library log through the same pipeline with the message text as `event`, so real
  cardinality is unbounded. `event` goes to structured metadata instead; `| json | event=...`
  costs slightly more at query time and cannot melt the index.
- **Alloy drops anything older than 1 hour.** `loki.source.docker` has no "start at the
  tail" option: with no stored position it reads each container's log from the beginning.
  Fixed at the collector rather than by raising Loki's cutoff, because that cutoff is a real
  guard against stale data being backdated into the store.
- **Operational endpoints are never traced.** `/health`, `/ready`, `/metrics` and the whole
  `/sim*` control surface are excluded. Prometheus scrapes and Docker probes every 15s
  across five services; tracing those buries real transaction spans and makes Tempo report
  the platform's self-monitoring as DPI traffic.
- **Alert thresholds are the baseline modules 4-7 must beat, not a replacement for them.**
  Static thresholds are fast and explainable. Module 4 detects "latency is high *for this
  traffic level*", which a threshold cannot judge. Keeping both makes the platform's own
  value measurable.
- **`DpiOverCapacity` is the important alert.** Every other signal — latency, errors, queue
  depth, settlement lag — is derived from `tps/capacity`, so it is the cause and the rest
  are symptoms. Alertmanager's inhibit rules suppress the symptoms when it fires.
- **Short `for` durations.** Simulator faults cap at 30 minutes and demo scenarios run in
  minutes, so a conventional 10-minute `for` means the fault expires before the alert fires.

### Things that bit us, recorded so they don't bite again

| Problem | Fix |
|---------|-----|
| Loki, Tempo and Alloy all reported permanently `unhealthy` while working fine | Loki and Tempo ship **distroless** images — no `/bin/sh` at all — and Alloy has a shell but no `wget` or `curl`, so a `CMD-SHELL` healthcheck cannot run. Healthchecks removed; Prometheus scrapes all three instead, which is the signal the alert rules already use |
| A wall of `timestamp too old` errors in Loki on first start | Alloy replayed weeks of backlog from long-running infra containers. Added `stage.drop { older_than = "1h" }` |
| Tempo warned `complete_block_timeout < blocklist_poll`, and traces 404'd despite existing | Default 5m block-list poll against a 1m flush leaves a 4-minute window where a flushed trace cannot be fetched by id. Set `blocklist_poll: 30s` |
| `check_observability.py` failed if run right after `stack.ps1 up` | Loki and Tempo serve traffic — and accept writes — for a couple of minutes before `/ready` turns 200. The script now polls instead of failing, and reports how long it waited |
| Tracing configured twice in one process silently sent spans nowhere | OpenTelemetry allows `set_tracer_provider` **once**; later calls warn and keep the original, so the module held a provider the process was not using. `configure_tracing` now adopts an already-installed provider. Covered by a regression test |
| Tempo's service graph showed a `user -> service` edge for all five services | `/sim` introspection calls were being traced, inflating `traces_spanmetrics_calls_total` with the platform's own polling. Excluded — with anchored patterns, because a bare `sim` would also have excluded `/simulate/request`, the one endpoint that must be traced |

---

## Module 2 original plan (for reference — superseded by the record above)

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

## Module 3 — Metrics collection agent ✅

The agent closes the loop the rest of the platform is built around. Prometheus holds 15 days
of metrics but is not a feature store; the models need a queryable table with months of
history, and the anomaly detector needs a push stream rather than a polling loop. This
service is the bridge: PromQL in, `service_metrics` rows and `aiops.metrics` events out.

**Delivered**

| Path | What it is |
|------|-----------|
| `services/metrics_agent/collector.py` | Reads the `dpi:*` recording rules and normalises them into `MetricSample`. Service discovery from `up{job="mojaloop"}`, so a new service is collected without a code change |
| `services/metrics_agent/writer.py` | Batch upsert into `service_metrics`, then one `aiops.metrics` event per service |
| `services/metrics_agent/agent.py` | The 15-second loop plus nine Prometheus series describing its own behaviour |
| `services/metrics_agent/backfill.py` | Replays Prometheus history through the *same* collector, so modules 4 and 5 can train immediately |
| `services/metrics_agent/routes.py` | `/status`, `/queries`, `POST /collect`, `POST /backfill` |
| `docker-compose.aiops.yml` | New compose layer for modules 3-9. `stack.ps1` already referenced it and skipped it while absent |
| `scripts/replay_history.py` | Generates 28 days of history by driving the simulator's **own** `TrafficModel` over past timestamps. This is what makes modules 4 and 5 buildable today instead of in a month |
| `scripts/check_metrics_agent.py` | Eleven-part verification |
| `tests/test_metrics_agent.py` | 39 tests |

**Verify**

```powershell
.\scripts\stack.ps1 up -Layer all
# The seeder and the agent must not both feed the same window - see below.
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --clear
# History for modules 4 and 5. Takes a few minutes; the agent owns the last hour.
.\.venv\Scripts\python.exe scripts\replay_history.py --days 28
.\.venv\Scripts\python.exe scripts\check_metrics_agent.py
```

**Verified working:** all 14 rules the agent depends on return data · row count grows on
demand via `POST /collect` · 5 services in the newest window with counts and stored ratio
agreeing · stored TPS matches live `dpi:tps:rate5m` to 2 decimal places · 1465 events on
`aiops.metrics` · `service_metrics_1m` and `service_metrics_1h` both populate · all 10
collection series on `/metrics`, and Prometheus scraping them.

Backfill, measured: a 20-hour request covered 1201 instants, collected the 129 that
Prometheus actually had data for, wrote 641 rows, and reported 10.7% coverage rather than
inventing the rest. `service_metrics_1h` went from empty to 20 buckets — which is module 5's
training set.

### Design notes worth knowing

- **The agent reads recording rules, never raw counters.** That is what the `dpi:*` rules
  are for. A `rate()` window written slightly differently in Python would give the agent a
  number that disagrees with the dashboard, with nothing in either to show which is wrong.
- **A service that is down gets no row, not a row of zeros.** This is the most important
  behaviour in the module. Zeros say "no traffic, no errors", which is what a quiet period
  looks like; module 4 would learn that an outage is indistinguishable from idleness. An
  absent row says "we do not know", which is true.
- **Counts and the stored `error_ratio` agree by construction.** `MetricSample.error_ratio`
  is computed from `error_count / request_count`, so `error_count` is derived from the
  recorded ratio rather than queried separately. `request_count` counts *transactions*, not
  API requests, even though `dpi:request_rate:rate5m` exists — pairing a transaction-derived
  error count with an API-request denominator would store a ratio that disagrees with
  `dpi:error_ratio:rate5m` for no reason.
- **One row per service, not per replica.** Every recording rule aggregates
  `by (service, platform)`, so the value is already summed across instances. Since
  `DashboardRepository.service_health` **sums** `tps` across a service's instances, writing
  one row per replica would multiply reported platform traffic by the replica count. With a
  single target the row carries the real instance address; with several it is marked
  `<service>:aggregate`.
- **`ON CONFLICT DO UPDATE`, where the seeder uses `DO NOTHING`.** The seeder generates
  history once. The agent re-collects on a schedule and may retry a cycle, and a retry
  carries *better* data because `rate()` windows fill in as scrapes land. `DO NOTHING` would
  permanently freeze whichever attempt happened to land first.
- **Backfill does not publish to the bus by default.** `REDIS_STREAM_MAXLEN` is 50k; a week
  of replay across five services is about that many events, which would evict exactly the
  recent samples the anomaly detector has not read yet. The stream is for what is happening
  now, the table for what happened.
- **The backfill is not a second implementation.** It steps a timestamp backwards and calls
  the same `collect(at=...)` the live loop uses. Every query passes an explicit evaluation
  time for this reason, so a backfilled row and a live row are produced by identical code.

### Where the training data comes from

Modules 4 and 5 are models, and models need history. Collected through the live pipeline that
is four weeks of wall-clock, because module 5 has to learn that weekends are quieter than
weekdays and a weekly rhythm cannot be learned from one week.

`scripts/replay_history.py` produces it in minutes by driving `mojaloop_sim.traffic.TrafficModel`
over past timestamps. That is the same generator the live simulator uses, and `traffic.py` was
written for it — `seasonal_multiplier` is a pure function of time, and its docstring says
"a backtest over historical timestamps gives the same curve the live simulator produced".

Two alternatives were rejected:

- **`seed_demo_data.py`** has its own independent `random`-based generator — it does not import
  `TrafficModel`. A model trained on it learns a distribution the live system never produces,
  and the resulting misbehaviour looks like a detector bug rather than a data mismatch.
- **Public AIOps datasets** (GAIA, SMD, LO2) cannot fit `ANOMALY_FEATURES`. None carry
  `settlement_lag_seconds`, the payments-specific signal this platform is built around, and
  `StandardScaler` fitted to another system's magnitudes makes every one of our normal samples
  read as an extreme outlier. Isolation Forest is distribution-specific; there is nothing to
  transfer.

Worth stating plainly: **this is not a real-versus-synthetic choice.** All traffic here comes
from `mojaloop_sim`. The honest distinction is *pipeline-observed* versus *directly computed* —
replayed rows skip Prometheus's `rate()` smoothing and scrape timing, not the traffic patterns.
`--smooth` (on by default) applies a trailing mean approximating the 5-minute rate window, so
the variance matches what the agent would have recorded. Rows are tagged `source=replay` so
provenance is never lost.

Measured: 26,880 rows over 28 days, and the weekly shape survives the round trip —

```
Mon 15.88  Tue 16.28  Wed 15.97  Thu 15.65  Fri 16.55  Sat 10.83  Sun 8.75
```

Friday/Sunday is 1.89x, against `WEEKDAY_WEIGHTS` of 1.10/0.58 = 1.90. That ratio is the thing
module 5 has to find.

Replay is deliberately **fault-free**. `ANOMALY_CONTAMINATION` is 0.02, so an Isolation Forest
wants a mostly-normal baseline; the anomalies should come from real fault injection at demo
time, not be pre-baked into the training set.

### Do not run the seeder and the agent together

`scripts/seed_demo_data.py` is the stand-in this module replaces. Both write
`service_metrics`, and the dashboard sums `tps` across a service's instances within a
120-second grace window — so seeded rows (`seed-<service>-0`) alongside agent rows inflate
reported traffic with **no error anywhere to explain it**. The agent checks for this once at
start-up and reports it on `/status` as `conflicting_producers`; the verification script
fails on it outright.

### Things that bit us, recorded so they don't bite again

| Problem | Fix |
|---------|-----|
| Every row landed with `labels = '{}'`, leaving agent rows indistinguishable from seeded ones and quietly disabling the conflicting-producer detection | The column list was copied from the seeder, whose own list stops at `replicas`. `labels` is now written — and jsonb needs `CAST(:labels AS jsonb)`, because asyncpg sends a dict as text and Postgres refuses to coerce it. Regression test added |
| After Prometheus restarts, the agent wrote `tps=0` for five minutes | Every `rate()` rule needs history *behind* the evaluation instant, so the gauges resolve while `dpi:tps:rate5m` does not. That stamped a five-minute fake outage into the table on every restart. `tps` is now treated as the one essential signal: without it the service is skipped and reported as `services_incomplete` — distinct from `services_down`, because the service is fine and only our view of it is not |
| `histogram_quantile` returning NaN was stored as a measured zero | Prometheus sends NaN as the string `"NaN"`, which `PrometheusClient` turns into `None`. Coercing that to 0 records "latency was zero" where the truth is "there were no observations". NaN is now dropped, so the rule is reported as returning nothing usable |
| Replaying 28 days left `service_metrics_1h` holding only 3 | Both continuous aggregate policies have a bounded `start_offset` — 2 hours for the 1-minute view, **3 days** for the 1-hour one — so the scheduled refresh never touches older buckets. Module 5 would have trained on a ninth of its data with every component reporting healthy. `replay_history.py` now refreshes both views across the full range, and `check_metrics_agent.py` asserts the rollup *starts* within one bucket of the raw data |
| The first version of that assertion failed on a correct system | It compared total span, but the newest bucket legitimately lags: both policies hold an `end_offset` back so a half-written bucket is never materialised. It reported "covers only 28.0d of 28.0d". Now it checks the oldest bucket — which is where the `start_offset` bug actually shows — and treats trailing lag as a warning with a generous allowance |
| `hash(name)` as a random seed made replays non-reproducible | Python randomises string hashing per process unless `PYTHONHASHSEED` is set. Harmless for the long-running simulator, but it meant a re-run generated a different series while `ON CONFLICT DO NOTHING` kept the first run's values — so the operator believed they had regenerated data they had not. Replay seeds with `zlib.crc32` instead |

---

## Module 4 — Anomaly detection ✅

The first service that makes a judgement rather than moving data. Consumes `aiops.metrics`,
scores each sample, and publishes `aiops.anomalies` with an explanation and a `kind` that maps
to a remediation playbook.

**Delivered**

| Path | What it is |
|------|-----------|
| `anomaly_detector/baseline.py` | Per-service, hour-of-day-aware mean and sigma per feature. Supplies the signed deviations that make an anomaly explainable |
| `anomaly_detector/model.py` | `StandardScaler` + `IsolationForest`, with calibrated 0-1 scoring anchored on the model's own decision boundary |
| `anomaly_detector/scorer.py` | Combines forest and baseline into one score. See below - this is the important design decision in the module |
| `anomaly_detector/classify.py` | Signed deviations to `AnomalyKind`, severity and a plain-English description |
| `anomaly_detector/trainer.py` | Per-service fits from TimescaleDB history, artifacts to disk, one `model_runs` row per service per run |
| `anomaly_detector/detector.py` | Redis consumer group on `aiops.metrics`; writes `anomalies` and publishes `aiops.anomalies` |
| `scripts/check_anomaly_detector.py` | Six-part verification, including two fault-injection round trips |
| `tests/test_anomaly_detector.py` | 36 tests |

**Verify**

```powershell
.\scripts\stack.ps1 up -Layer all
.\.venv\Scripts\python.exe scripts\replay_history.py --days 28   # if not already done
curl.exe -X POST http://localhost:8021/train -H "Content-Type: application/json" -d "{\"days\":28}"
.\.venv\Scripts\python.exe scripts\check_anomaly_detector.py
```

**Verified working.** Five models fitted from ~6300 rows each in 18 seconds, `train_outlier_ratio`
landing at 0.0200 against a contamination of 0.02, and 1.2-1.6% of training data above the
alerting threshold. The newest healthy sample scores 0.0014 — no alert on normal traffic. Two
different faults produce two different, correct kinds:

| Injected | Kind | Score | Attribution |
|---|---|---|---|
| `error_injection` on central-ledger | `error_burst` | 0.991 | `error_ratio` +5.5 sigma |
| `network_latency` on quoting-service | `latency_spike` | 0.988 | `latency_p99` +65.6 sigma, `latency_p50` +6.3 sigma |

### The Isolation Forest alone does not work, and finding out why mattered

A sample with `error_ratio` of 0.35 — **175x** the 0.002 baseline — scored **0.0023**. The forest
called a catastrophic error rate perfectly normal.

That is not a bad fit. An Isolation Forest is an *outlier* detector, not a *novelty* detector.
Its trees contain only split thresholds learned during training, so a point far outside that
range is not isolated quickly: it follows the same path as a point just outside, receives an
average path length, and scores as normal. It cannot extrapolate. Relying on it alone would have
shipped a detector that misses exactly the faults module 9 injects.

Standard-deviation distance from the hour-of-day baseline *does* extrapolate, without bound. So
the final score is the **maximum of both detectors**, and they are genuinely complementary:

- the **forest** catches unusual *combinations* of individually normal values — which no
  threshold rule can do, and the only reason it is here;
- the **baseline** catches magnitudes outside anything ever observed — which the forest
  structurally cannot.

The maximum rather than a weighted blend, so each verdict stays legible; an average would let a
confident baseline signal be diluted by an indifferent forest and the result would mean neither
thing. `/score` and the logs report both numbers separately plus which one decided, because a
forest score near zero beside a high deviation score is the *expected* shape for an out-of-range
fault, and hiding that behind one number makes a working detector look broken.

### Other design notes worth knowing

- **Calibration is anchored on the decision boundary, not on percentiles.** The obvious mapping
  — percentile of the training score distribution — puts `ANOMALY_SCORE_THRESHOLD = 0.65` at the
  65th percentile, so a third of perfectly normal samples would clear it and every service would
  alert continuously. Instead `score = 0.5 * (1 + tanh(raw / scale))`, so 0.5 is exactly where
  the forest flips from inlier to outlier and 0.65 sits meaningfully beyond it. `train_above_0_65`
  in `model_runs.metrics` is the figure that proves this holds before deployment.
- **The deviation curve is logarithmic.** `tanh` on raw sigma saturates to exactly 1.0 well
  before the thousands of sigma a near-constant signal reaches under fault, so every serious
  anomaly would score 1.0 and the RCA engine would have nothing to rank by. `log1p` first keeps
  it monotone: 2 sigma to 0.50, 10 to 0.91, 100 to 0.99, 3600 to 0.9998.
- **Direction matters, and `abs(deviation)` is the trap.** Latency five sigma *below* baseline is
  a service having a good day. Traffic is the only signal where both directions indicate a
  problem, hence `traffic_surge` and `traffic_drop` as separate kinds.
- **Three or more implicated signals is `multivariate`, whichever deviated most.** The
  simulator's `bad_deploy` raises errors, latency and queue depth together on purpose, because
  module 7 must attribute that composite to a *change* rather than to load. Calling it
  `error_burst` because errors happened to move most would discard that evidence.
- **Hour-of-day, but not weekday.** 28 days gives ~28 samples per (service, hour) bucket;
  splitting by weekday too would leave 4, and a sigma over 4 points is noise dressed as
  statistics. Module 5 models the weekly component, where it has the resolution.
- **Only emitted past the decision boundary.** Writing an `AnomalyEvent` per service per 15
  seconds is ~29k rows a day of "nothing happened", which buries the real ones and hands the RCA
  engine a haystack.
- **Per-service models.** central-ledger runs at 26 tps, transaction-request-service at 6; one
  shared scaler would normalise them onto a single distribution and every sample from the
  smaller service would sit far from the pooled mean.
- **Detection latency versus classification are separate questions.** The `dpi:*` rules are
  5-minute rates, so a fault *ramps*. The first anomaly often arrives while only the forest can
  see it, which correctly reads as `multivariate`; half a minute later the kind sharpens to
  something a playbook can match. The check script reports both rather than conflating them.

### Things that bit us, recorded so they don't bite again

| Problem | Fix |
|---------|-----|
| A 175x error rate scored 0.0023 and produced no anomaly | Isolation Forest cannot extrapolate beyond its training range — see above. Score is now `max(forest, baseline_deviation)`, with a regression test asserting the division of labour explicitly, because the forest scoring it low is correct behaviour rather than a fault to fix |
| Anomalies arrived with an empty `contributing_features` | When the forest fired but nothing crossed 2 sigma, no signals were named. An anomaly that explains nothing cannot be triaged and module 6 has nothing to match. The threshold now governs which *kind* is claimed, not whether an explanation is offered |
| The stored `description` contained a sigma symbol | U+03C3 raises `UnicodeEncodeError` on a cp1252 Windows console, which crashed the verification script twice. Descriptions are ASCII and spell "sigma" out. Same trap as the em dashes that broke the PowerShell scripts, one layer in: stored data rather than source |
| `/status` reported all five services as unmonitored immediately after a successful train | On first boot the detector legitimately sees every service before any model exists, and `unknown_services` was never pruned. It is a *current* complaint, not a history, so `set_models` now clears entries that have a model |
| Test scores were computed one row at a time | `fit()` called `model.score()` per training row to build the metrics, so sklearn's per-call overhead turned a sub-second fit into several seconds — very visible across a suite that fits a model per test. The tanh mapping is applied to the whole score column with numpy instead |
| `error_ratio` overrides in tests silently did nothing | It is a *computed* property over `request_count` and `error_count`, so the base validator strips it. Tests asserting on a 175x error burst were scoring a sample whose error ratio was zero. The helper now derives counts, exactly as `MetricsCollector._counts_for` does |
| mypy aborted before checking any of our code | numpy 2.x ships stubs using `type` statement syntax that mypy only accepts under `python_version >= 3.12`. Skipped via a `follow_imports` override rather than bumping the setting, because `python_version = "3.11"` is a real promise — `aiops_common` declares `requires-python = ">=3.11"` |

---

## Module 5 — Traffic prediction and pre-emptive scaling ✅

**Data prerequisite, settled in module 3.** Weekly seasonality has a period of 168 hourly
points, so seven days is *one* cycle — enough to fit, impossible to validate, and the
verification criterion below requires beating a seasonal-naive baseline that has no reference
for the holdout. Two cycles is the minimum; `replay_history.py --days 28` gives four, so a full
week can be held back while three remain for training. 28 days also sits inside the 30-day raw
retention policy, and the 1-hour rollup has no retention policy of its own — so once
materialised, that history survives the raw rows ageing out, exactly as the schema comment
intends.

Where module 4 reacts, this one anticipates.

**Delivered**

| Path | What it is |
|------|-----------|
| `traffic_predictor/forecaster.py` | Hour-of-week seasonal profile blended with seasonal-naive, weighted by lead time. Both the model form and the weights were chosen by measurement — see below |
| `traffic_predictor/backtest.py` | Rolling-origin backtesting with MAPE against the baseline, refitting per origin so the hold-out cannot leak |
| `traffic_predictor/capacity.py` | Measured `tps_per_replica` from Prometheus, turned into a replica recommendation with headroom derived from the backtest |
| `traffic_predictor/history.py` | Loads the 1-hour rollup, skipping partial buckets |
| `traffic_predictor/predictor.py` | 10-minute cycle: forecast, backtest, persist, publish, record in `model_runs` |
| `scripts/check_traffic_predictor.py` | Six-part verification covering all three stated criteria |
| `tests/test_traffic_predictor.py` | 35 tests |

**Verify**

```powershell
.\scripts\stack.ps1 up -Layer all
.\.venv\Scripts\python.exe scripts\replay_history.py --days 28   # if not already done
.\.venv\Scripts\python.exe scripts\check_traffic_predictor.py
```

**Verified working**, against all three criteria:

1. *Forecast tracks the known daily curve.* Peak predicted at 19:00 (40.9 tps), trough at 01:00
   (14.1 tps), a 2.90x swing — and the simulator's `daily_multiplier` is two Gaussians centred on
   11.0 and 19.5, the evening one larger, so 19:00 is exactly right. Correlation with the same
   hours one week earlier: **1.000**.
2. *Recorded MAPE beats a seasonal-naive baseline.* Aggregate MAPE, five services:

   | horizon | model | naive | improvement |
   |---|---|---|---|
   | 1h | 0.01641 | 0.01695 | +3.2% |
   | 6h | 0.03828 | 0.04265 | +10.2% |
   | 24h | 0.02694 | 0.03287 | +18.0% |

   13 of 15 individual service/horizon comparisons also win. The two exceptions are both at the
   1-hour horizon and are reported as warnings rather than hidden — see the honest limits below.
3. *A predicted peak produces `scale_out` ahead of the spike.* Driven deliberately by scaling
   central-ledger to one replica: 1h → `scale_out` 1→3 for a 24.4 tps peak, 6h and 24h →
   `scale_out` 1→4 for a 40.9 tps peak at 19:00. Persisted to `traffic_predictions` for module 6.

### The model was chosen by measurement, and the first design was wrong

The first implementation was a separable decomposition, `level * hour_factor * weekday_factor`, on
the reasoning that 24 + 7 parameters estimated from 28 and 96 observations each must beat 168
parameters estimated from 4 each. It lost to the naive baseline on **8 of 15** comparisons.

Backtested mean MAPE across all five services settled it:

```
variant                        1h       6h      24h    wins vs naive
separable, 48h level        0.0290   0.0502   0.0311    3/5  1/5  3/5
separable, no level         0.1264   0.1358   0.1401    0/5  0/5  0/5
hour-of-week profile        0.0250   0.0359   0.0267    2/5  4/5  5/5
blend at fixed weight 0.5   0.0233   0.0373   0.0279    3/5  5/5  5/5
seasonal-naive (baseline)   0.0262   0.0440   0.0329      -
```

Three things that table decided:

- **Separability does not hold well enough.** The 168-bucket hour-of-week profile beats the
  separable form at every horizon, so the daily shape genuinely differs by weekday rather than
  being one curve scaled by a weight. My data-efficiency argument was correct arithmetic and the
  wrong conclusion.
- **The `level` term is load-bearing, not a source of bias.** Removing it makes the model five
  times worse. The initial hypothesis — that re-anchoring to recent actuals was what cost accuracy
  — was exactly backwards.
- **Blending with the baseline beats either component alone.** Textbook bias-variance: naive is
  unbiased but carries the variance of a single observation, while the profile median is
  low-variance and smooths away real week-to-week drift.

A second measurement showed the optimal blend weight **rises with lead time**, so it is applied per
forecast step rather than as one constant:

```
weight      1h        6h       24h
  0.00   0.01695   0.04265   0.03287    <- pure naive
  0.15   0.01641   0.04057   0.03113    <- best at 1h
  0.75   0.01843   0.03567   0.02659    <- best at 6h
  0.90   0.02018   0.03605   0.02648    <- best at 24h
  1.00   0.02135   0.03667   0.02665
```

Monotone, with a clear reading: one hour out, last week's same hour is nearly unbeatable on
near-deterministic history — there is no noise left for a 4-week median to average away, so the
profile can only add its smoothing bias. Further out, the level drifts and the structural model
earns its weight.

### Honest limits

- **The model contains the baseline it is compared against.** Beating seasonal-naive is therefore
  partly structural. The profile half does real work — it beats naive alone at 6h and 24h — but the
  1-hour result should be read as "slightly better on average" rather than as having learned
  something naive has not.
- **Two services lose at the 1-hour horizon** (`ml-api-adapter` −1.3%, `transaction-request-service`
  −12.9%) and no blend weight fixes both. The check asserts **aggregate** MAPE per horizon, which is
  how a forecaster is judged; a per-service requirement would make the criterion hostage to
  whichever service happens to have the least noise. The individual losses are printed on every run.
- **The history is near-deterministic.** `replay_history.py` caps noise at 12% and hourly averaging
  removes most of what remains, which makes seasonal-naive an unusually strong benchmark. Against
  real Mojaloop traffic the profile's variance reduction should matter more and the gap should widen.

### Other design notes worth knowing

- **Headroom is derived, not chosen.** `recommend()` takes the larger of a configured floor (25%)
  and the measured backtest MAPE, so the safety margin scales with how wrong the model actually is.
  Module 2 measured what running out costs: at a load factor of 1.26, central-ledger served 100%
  errors with a 56-second p99. One replica too many is cheap; one too few is an outage — hence
  always rounding up.
- **Capacity is measured, not assumed.** `tps_per_replica` comes from `dpi:capacity_tps / dpi:replicas`
  in Prometheus. Hard-coding it would mean every scaling decision depended on a number nobody had
  checked since it was typed.
- **Trained on `avg_tps`, not `max_tps`.** Sizing to the peak *within* every hour on top of a peak
  *across* hours would compound two safety margins into one number nobody can reason about. Headroom
  is applied once, explicitly.
- **Backtest refits per origin.** Reusing a profile fitted on the whole series would leak the
  hold-out into the factors — the most common way a backtest flatters itself.
- **No statsmodels, no scikit-learn.** Holt-Winters with `seasonal_periods=168` needs two full
  cycles just to initialise and leaves little of 28 days for validation. The forecaster is medians
  and arithmetic from the standard library, so this image is ~100 MB smaller than the detector's.

### Things that bit us, recorded so they don't bite again

| Problem | Fix |
|---------|-----|
| The model lost to the naive baseline on 8 of 15 comparisons | The separable form was the wrong structure and the `level` term was wrongly suspected. Replaced with an hour-of-week profile blended with naive at a lead-time-dependent weight, all three decisions made from backtest tables rather than reasoning. 13/15 and aggregate wins at every horizon |
| The check script warned that a *correct* forecast had the wrong peak hour | It expected a morning peak. `mojaloop_sim.traffic.daily_multiplier` is two Gaussians — 11.0 weighted 0.45 and **19.5 weighted 0.55** — so the daily maximum is the evening one. The forecast was right and the expectation was wrong |
| `test_level_tracks_a_recent_shift` failed after a constant changed | It doubled the last 48 hours while `LEVEL_WINDOW_HOURS` had become 168, so the median barely moved. Tests that hard-code a window have to move with it |

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
.\.venv\Scripts\python.exe -m uvicorn api_gateway.asgi:app --port 8000 --app-dir services\api_gateway
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
