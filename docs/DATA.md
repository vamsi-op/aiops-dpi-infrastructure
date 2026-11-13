# Data

**In short:** no public dataset carries Mojaloop service telemetry, so all traffic on this
platform comes from a simulator. The one dataset stored as a file is the 28-day history the
traffic forecaster trains on, committed to the repo. Every
anomaly, incident, healing action, forecast and chaos result is produced by the running
services. None of them is ever loaded from a file.

## Where each kind of data comes from

| Data | Source | Stored as |
|---|---|---|
| Live service telemetry: traffic, latency, errors, CPU/memory, queue depth, settlement lag | Mojaloop simulator ([services/mojaloop_sim](../services/mojaloop_sim)) → Prometheus → metrics agent | `service_metrics` (TimescaleDB) |
| **Training history (28 days)** | The simulator's own traffic model, replayed over past timestamps | **[ml/datasets/mojaloop_replay_28d.csv](../ml/datasets/mojaloop_replay_28d.csv)**, loaded into `service_metrics` with `source=replay` |
| Faults | Chaos experiments injected into the simulator (dashboard → Chaos, or `scripts/run_chaos_experiment.py`) | `chaos_runs` |
| Anomalies | Anomaly detector (Isolation Forest) scoring live telemetry | `anomalies` |
| Incidents and root causes | RCA engine correlating anomalies | `incidents` |
| Healing actions | Healing engine executing playbooks | `healing_actions` |
| Forecasts | Traffic predictor (1 / 6 / 24 h) | `traffic_predictions` |
| Deployments | Real rollouts (the Kubernetes `bad_deploy` experiment) and chaos runs | `deployments` |

## Why the training data is simulated

- **No public source fits.** Mojaloop, MOSIP and Beckn operators don't publish per-service
  operational telemetry. Generic AIOps datasets (GAIA, SMD, LO2) don't have this
  platform's features: none of them carries `settlement_lag_seconds`, the payments-specific
  signal the detector relies on. A scaler fitted to another system's magnitudes would also
  flag every normal sample from ours as an outlier.
- **Waiting would take a month.** The forecaster needs four weeks of history to learn the
  weekday-versus-weekend rhythm and still hold one week back for testing. Collecting that
  live means running the stack for 28 days.
- **The history comes from the same model as the live traffic.**
  [replay_history.py](../scripts/replay_history.py) doesn't use a separate random generator.
  It replays the simulator's own `TrafficModel` with the same service profiles, so the
  models train on exactly the distribution they later see live.
- **It's fault-free on purpose.** The detector learns what normal looks like. Anomalies
  come only from live fault injection, so detection is always tested against faults it
  never saw in training.

## The dataset file

| | |
|---|---|
| File | `ml/datasets/mojaloop_replay_28d.csv` (5.3 MB) |
| Rows | 26,880 (5,376 per service × 5 services) |
| Time range | 2026-10-16 12:00 → 2026-11-13 11:59 UTC (28 days) |
| Resolution | 15-minute samples for the first 26 days, 1-minute samples for the last 2 |
| Services | ml-api-adapter, central-ledger, quoting-service, account-lookup-service, transaction-request-service |
| Columns | timestamp, service, instance, platform, tps, request/error counts, error_ratio, latency p50/p95/p99, cpu and memory utilisation, queue_depth, active_connections, settlement_lag, quote and verification latency, replicas, source, collector, step_seconds |

Traffic per service (tps, from the file): central-ledger 7.5–42.8 (median 24.3),
quoting-service 5.2–31.4, account-lookup-service 4.3–26.6, ml-api-adapter 4.3–24.9,
transaction-request-service 1.7–9.6. The highest error ratio anywhere in the file is 0.56%.

### Regenerate the file

The generator uses a fixed seed per service, so this rebuilds the same file:

```bash
python scripts/replay_history.py --export-csv ml/datasets/mojaloop_replay_28d.csv --days 28 --end 2026-11-13T12:00:00+00:00
```

### Load the file into the database

```bash
python scripts/replay_history.py --from-csv ml/datasets/mojaloop_replay_28d.csv
```

The rows are shifted forward by **whole weeks**, so the newest one lands just before the
present. Whole weeks keep every row on the same weekday and hour. A Monday-morning peak
stays on a Monday morning, which is what the forecaster learns and what the live simulator
keeps producing. Rows are tagged `source=replay`, so they're always distinguishable from
data the live pipeline collected. `replay_history.py --clear` removes them.

## How the models are evaluated

- **Traffic forecaster:** the most recent week is held out. The forecast's error on it
  (MAPE) is compared with a seasonal-naive baseline ("same hour last week") and recorded
  in `model_runs`.
- **Anomaly detector and the whole loop:** every chaos experiment is a labelled fault. The
  platform knows exactly when and where the fault was injected, so each run measures
  detection rate, time to detect, time to root cause and time to remediate against ground
  truth. The results are in `chaos_runs` and on the dashboard's Chaos page.

## Removed: scripts that wrote made-up results

Two earlier scripts wrote results that the platform never produced:

- **`seed_demo_data.py`:** written so the dashboard could be built before the backend
  existed. It inserted scripted anomalies, incidents, healing actions, forecasts, chaos
  runs and model-training records.
- **`execute_all_playbooks.py`:** saved healing actions with hard-coded before/after
  numbers.

Both are deleted. CI's API contract check now uses
[tests/fixtures/contract_fixture.sql](../tests/fixtures/contract_fixture.sql) instead: one
row per table, every id prefixed `contract-fixture-`, loaded only into CI's throwaway
database.

**A database that was ever seeded still holds those rows,** and they can't be told apart
from real ones. Reset it before any demo or deployment. This deletes all its data:

```powershell
.\scripts\stack.ps1 reset
.\.venv\Scripts\python.exe scripts\replay_history.py --from-csv ml/datasets/mojaloop_replay_28d.csv
```
