# Architecture

How the platform is put together, and why each choice was made.

---

## 1. The problem shape

Traditional monitoring answers *"is it broken?"* after the fact. DPI workloads need
answers to three harder questions:

1. **Will it break?** — forecast load, scale before the spike, not after the queue backs up.
2. **What is unusual?** — a service can be within every static threshold and still be
   behaving abnormally. Latency of 200 ms is fine at 3 a.m. and alarming at peak.
3. **Why?** — five services alarming at once is one problem, not five. Something caused it.

Those three questions map directly onto the three ML/analytics components: traffic
prediction, anomaly detection, root cause analysis. Self-healing then closes the loop by
acting on the answers.

---

## 2. Layered design

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ LAYER 4 · CONTROL                                                              │
│   api-gateway (FastAPI + WebSocket)   dashboard (React)   chaos-agent          │
│   Human-facing: see state, inspect an incident, run an experiment.             │
└───────────────────────▲───────────────────────────────────────────────────────┘
                        │ REST + WebSocket
┌───────────────────────┴───────────────────────────────────────────────────────┐
│ LAYER 3 · INTELLIGENCE & ACTION                                                │
│   anomaly-detector   traffic-predictor   rca-engine   healing-engine           │
│   Stateless consumers of the event bus; state lives in TimescaleDB/Redis.       │
└───────────────────────▲───────────────────────────────────────────────────────┘
                        │ Redis Streams (aiops.metrics, aiops.anomalies, ...)
┌───────────────────────┴───────────────────────────────────────────────────────┐
│ LAYER 2 · COLLECTION & STORAGE                                                 │
│   metrics-agent  →  TimescaleDB (raw + 1m/1h rollups)                          │
│   Normalises everything into one MetricSample shape.                            │
└───────────────────────▲───────────────────────────────────────────────────────┘
                        │ PromQL / LogQL / TraceQL
┌───────────────────────┴───────────────────────────────────────────────────────┐
│ LAYER 1 · OBSERVABILITY PLANE (off the shelf)                                  │
│   Prometheus   Loki + Promtail   Tempo   Alertmanager   Grafana                 │
└───────────────────────▲───────────────────────────────────────────────────────┘
                        │ /metrics, stdout logs, OTLP traces
┌───────────────────────┴───────────────────────────────────────────────────────┐
│ LAYER 0 · WORKLOAD                                                             │
│   Mojaloop microservices (simulated first, real on Kubernetes later)            │
└───────────────────────────────────────────────────────────────────────────────┘
```

The rule that keeps this maintainable: **layers only talk downward for queries and
upward through the event bus.** No service reaches sideways into another's database.

---

## 3. Why these components

### TimescaleDB rather than plain PostgreSQL
Metrics are append-only time series. Timescale gives us hypertable partitioning,
continuous aggregates (pre-computed 1-minute and 1-hour rollups), 10x compression on old
chunks, and automatic retention — all in SQL we already know. The forecasting model
trains on `service_metrics_1h`, which is cheap to read because it is materialised.

### Redis Streams rather than pub/sub or Kafka
Pub/sub drops messages when a consumer is down, which is unacceptable for anomaly events.
Kafka is the right answer at scale but is heavy for a laptop demo. Streams give us
persistence, consumer groups, and at-least-once delivery with one small container.

### Prometheus rather than scraping services directly
We could poll each service's `/metrics` ourselves, but then we would be rebuilding
Prometheus: service discovery, rate calculations, histogram quantiles, retention. Better
to let Prometheus do collection and use PromQL as our query language.

### One `MetricSample` shape for everything
The anomaly detector should not know whether a number came from Prometheus, a Kubernetes
API, or a business event. `metrics-agent` is the only component that knows about sources;
everything downstream sees a uniform, validated feature vector. This is what makes the
platform portable from Mojaloop to MOSIP to Beckn — swap the collection adapter, keep the
intelligence.

---

## 4. Data flow, end to end

Take a memory leak in `central-ledger` as the worked example.

| Step | Component | What happens |
|------|-----------|--------------|
| 1 | workload | Container memory climbs from 60% to 94% over ten minutes |
| 2 | Prometheus | Scrapes `/metrics` every 15s, stores the series |
| 3 | metrics-agent | Every 15s runs a fixed set of PromQL queries, builds a `MetricSample` per service, writes it to TimescaleDB and publishes to `aiops.metrics` |
| 4 | anomaly-detector | Scores the sample against its Isolation Forest baseline. `memory_utilisation_ratio` is 4.2σ high, queue depth is rising with it → anomaly score 0.87, kind `memory_pressure`. Publishes to `aiops.anomalies` |
| 5 | rca-engine | Looks for other anomalies in a 5-minute window, checks the dependency graph, checks `deployments` for a recent change. Finds a deploy 12 minutes earlier → hypothesis `category=change, confidence=0.84`. Opens an incident, publishes to `aiops.incidents` |
| 6 | healing-engine | Matches `memory_pressure` + high confidence to the `restart_on_oom` playbook. Checks safety rails: enabled? cooldown clear? budget left? confidence above threshold? Records a `before_snapshot`, restarts the container, records `after_snapshot` |
| 7 | api-gateway | Streams every one of those events to the dashboard over WebSocket as they happen |
| 8 | chaos-agent | If this run was an experiment, records time-to-detect and time-to-remediate on the `chaos_runs` row |

Steps 4–6 are independent processes reading a shared bus, so any one of them can be
restarted, scaled, or debugged without stopping the others.

---

## 5. Module boundaries

| Module | Owns | Does not own |
|--------|------|--------------|
| 1 foundation | config, logging, schemas, event bus client, DB client, storage schema | any business logic |
| 2 observability + simulator | Prometheus/Grafana/Loki/Tempo config, synthetic Mojaloop workload | how metrics are interpreted |
| 3 metrics-agent | PromQL queries, normalisation into `MetricSample`, persistence | detection thresholds |
| 4 anomaly-detector | baselines, Isolation Forest, score normalisation, feature attribution | what to do about an anomaly |
| 5 traffic-predictor | forecasts, replica recommendations | executing a scale action |
| 6 healing-engine | playbooks, safety rails, execution, audit trail | deciding the cause |
| 7 rca-engine | correlation, dependency walking, change attribution, incident lifecycle | remediation |
| 8 api-gateway + dashboard | read APIs, WebSocket fan-out, UI | producing any data of its own |
| 9 chaos-agent | fault injection, experiment scorecard | detection or healing |

---

## 6. Cross-cutting decisions

**Everything is UTC and timezone-aware.** `aiops_common.schemas.utc_now()` is the only
sanctioned way to get "now". Naive datetimes are a lint error (`ruff` rule `DTZ`).

**Configuration is typed and centralised.** `get_settings()` returns a validated object.
No `os.getenv` scattered through services; a bad value fails at start-up with a clear
message rather than at 3 a.m. with a `TypeError`.

**Automation is off by default.** `HEALING_ENABLED=false` ships as the default, and every
playbook runs through the same four rails: enabled, cooldown, hourly budget, minimum
confidence. Skipped actions are recorded with a `skipped_*` status — the audit trail
proves the brakes work, not just the engine.

**The platform monitors itself.** Every service exposes `/metrics`, `/health` and
`/ready` through the same `build_app()` factory. An AIOps tool that silently dies is
worse than no AIOps tool.

**Feature order is a contract.** `ANOMALY_FEATURES` is a tuple, and the docstring says
append-only. Reordering it would silently invalidate every saved model.

---

## 7. Portability to other DPI platforms

The `platform` field on every record and the `AIOPS_DPI_PLATFORM` setting exist so a
single deployment can be re-targeted:

| Platform | Services monitored | Distinctive metric |
|----------|-------------------|--------------------|
| Mojaloop | ml-api-adapter, quoting-service, central-ledger, account-lookup | `settlement_lag_seconds` |
| MOSIP | authentication, ID repository, credential issuance | `verification_latency_seconds` |
| Beckn | gateway, BAP, BPP registry | search-to-confirm funnel latency |

`MetricSample` already carries all three DPI-specific fields, so the schema does not
change when the target does — only the collection adapter in module 3 and the metric
profile it queries.

---

## 8. What Kubernetes changes (module 10)

Compose is the development substrate; Kubernetes is the deployment target. The parts that
change:

- `metrics-agent` gains `kube-state-metrics` and `cAdvisor` as sources, so `replicas`,
  restart counts and OOMKill events come from the real thing.
- `healing-engine` playbooks swap the Docker SDK for the Kubernetes API: delete a pod
  instead of restarting a container, patch a Deployment's replica count instead of a
  Compose scale, `rollout undo` instead of a simulated rollback.
- `chaos-agent` can use real pod eviction and network policies.

Everything in layers 3 and 4 — the models, correlation, incident lifecycle, dashboard —
is unchanged. That separation is the point of the adapter boundary.
