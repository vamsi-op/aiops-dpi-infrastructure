# Operations

How self-healing decides and acts, what keeps it safe, how incidents move through their
lifecycle, and how to run and read chaos experiments.

---

## 1. Playbooks

| Playbook | Action on Docker Compose | Action on Kubernetes | Used for |
|---|---|---|---|
| `rollback_bad_deploy` | Restart the service (Compose has no revision history) | `rollout undo`: restore the previous ReplicaSet's pod template | A deployment diagnosed as the root cause |
| `circuit_break_cascade` | Open the breakers of every caller of the failing dependency, then restart the dependency | Same breakers, then a rolling restart | A failure spreading from a dependency to its callers |
| `restart_on_oom` | Restart the container | Rolling restart of the Deployment | Memory or CPU exhaustion, latency spikes and error bursts confined to one service |
| `clear_queue_backlog` | Restart to drain the queue; escalates to scale-out if a restart did not help | Same, via the Kubernetes API | Queue backlog, settlement delay |
| `scale_out_preemptive` | Add one replica (bounded by host headroom) | Scale the Deployment by one, raising an HPA's floor if one owns it | Traffic at ≥ 80 % of capacity, or a forecast peak above capacity |
| `scale_in_idle` | Remove one replica, never below the minimum | Same | Sustained low traffic |

### How a playbook is chosen

The healing engine waits up to 15 s for the RCA engine's incident, then selects from the
diagnosis before falling back to the symptom:

1. **CHANGE root cause** → `rollback_bad_deploy`.
2. **Another service is the root**, or a dependency of this service is part of the incident →
   `circuit_break_cascade` on the deepest affected dependency. The anomalous caller is a victim
   and is never restarted.
3. Otherwise, from the service's own telemetry: traffic near capacity → scale out; queue backlog →
   `clear_queue_backlog` (scale out if an earlier restart failed); CPU or memory exhaustion →
   `restart_on_oom`; errors or latency → restart.

Only actionable anomalies (score ≥ the threshold) that are less than
`HEALING_MAX_EVENT_AGE_SECONDS` old are considered.

---

## 2. Safety rails

Every action passes all of these, in order. A blocked action is still recorded, with a status
naming the rail that stopped it, so the audit trail shows the brakes working as well as the
engine.

| Rail | Setting | Recorded status when it blocks |
|---|---|---|
| Master switch | `HEALING_ENABLED` | `skipped_dry_run` |
| Minimum confidence | `HEALING_MIN_CONFIDENCE` (0.7) | `skipped_low_confidence` |
| Attempts per incident | `HEALING_MAX_ATTEMPTS_PER_INCIDENT` (2) | `skipped_max_attempts`, recorded once per incident; the incident needs a human |
| Per-target cooldown | `HEALING_COOLDOWN_SECONDS` (300), `HEALING_FAILURE_COOLDOWN_SECONDS` (120) | `skipped_cooldown` |
| Hourly budget | `HEALING_MAX_ACTIONS_PER_HOUR` (10) | `skipped_budget` |
| Capacity (scale-out) | replica bounds, host headroom | `skipped_max_replicas`, `skipped_insufficient_capacity`, `skipped_global_limit` |

The cooldown is claimed atomically in Redis, so two engines, or two anomalies for one fault,
cannot both act on the same target. Opening circuit breakers uses a separate cooldown key from
restarting the root, so a recent restart does not block the breakers.

### Verification

After an action the engine waits for fresh samples and judges recovery against the SLOs
(error ≤ 1 %, p99 ≤ 250 ms, queue ≤ 5, CPU and memory ≤ 85 %), using only samples taken after the
action finished. A failed first check is re-checked twice, 20 s apart, on the newest samples:
while a container restarts, Prometheus can keep serving its last gauge values for a few minutes.
Rate signals still inside the 5-minute window after a restart are left to SLO auto-resolution.
Before and after snapshots are stored on every action.

---

## 3. Incident lifecycle

```
open ──(healing action succeeds)──► mitigating ──(SLOs met for 120 s)──► resolved
  │
  └──(action fails, or attempt cap reached)──► stays open; requires a human
```

- **Opened** by the RCA engine when an actionable anomaly does not belong to an existing incident.
  Anomalies on the same service, or on a service sharing a dependency with it, are merged in;
  the stronger hypothesis is kept.
- **Resolved automatically** when every affected service meets its SLOs for a continuous 120 s and
  the last executed action did not fail. For callers of a restarted dependency, the rate signals
  that still hold the restart are excluded.
- **Resolved manually**: `POST http://localhost:8023/incidents/{id}/resolve`.
- A resolved incident is never reopened by an action that finishes after it closed.

---

## 4. Chaos experiments

Experiments are started through the gateway (`POST /api/chaos`) or from the dashboard's Chaos
page, and are recorded in `chaos_runs` with time to detect and time to remediate.

| Experiment | Effect |
|---|---|
| `memory_hog` | Memory climbs toward the limit |
| `cpu_hog` | CPU saturation, with a latency knock-on |
| `network_latency` | Added latency on every request; propagates to callers |
| `error_injection` | A fraction of requests fail |
| `traffic_spike` | Load several times normal |
| `queue_backlog`, `settlement_delay` | Backlog and settlement lag build up |
| `kill_service` | The service stops responding |
| `bad_deploy` | A defective release. On Kubernetes, a real rollout of a new revision with the defect. |

```powershell
Invoke-RestMethod -Method Post http://localhost:8080/api/chaos -ContentType application/json `
  -Body '{"experiment":"network_latency","target_service":"central-ledger","duration_seconds":300}'
```

### Scorecard

`scripts/chaos_demo.py` runs the three scenarios behind the problem statement's playbooks and
prints a scorecard. Before each scenario it waits until there are no recent actionable anomalies
and no open incidents, so one scenario's aftermath cannot leak into the next.

```powershell
python scripts\chaos_demo.py                          # all three (about 25 minutes)
python scripts\chaos_demo.py --scenario bad_deploy    # one: oom | cascade | bad_deploy
python scripts\chaos_demo.py --gateway http://<host>:8080 --json
```

| Column | Meaning |
|---|---|
| detect | Fault injection to the first anomaly on the target |
| RCA | Fault injection to the incident being opened |
| remediate | Fault injection to the first successful action on the target |
| root cause | The top hypothesis and its confidence |
| side effects | Actions on any service that was not faulted (breakers on callers excepted). Should be "none". |

The script exits non-zero if a scenario was not remediated or caused side effects. Allow about
five minutes between runs: a remediation restart leaves a 5-minute rate window to clear before the
next scenario starts on a quiet platform.

Measured results are in the [README](../README.md#verified-end-to-end) and
[KUBERNETES.md](KUBERNETES.md#verified-on-kind).

---

## 5. Day-to-day

| Task | How |
|---|---|
| Turn automation on or off | Compose: `HEALING_ENABLED` in `.env`, then `stack.ps1 up -Layer all`. Kubernetes: `.\scripts\k8s.ps1 healing` / `healing -Off`. |
| See why an action was taken | Dashboard → Automation, or `GET /api/actions`: trigger reason, confidence, snapshots, verification result |
| See why an action was *not* taken | The same list: the `skipped_*` status names the rail |
| Retrain the anomaly models | `POST http://localhost:8021/train` with body `{}` |
| Refresh forecasts now | `POST http://localhost:8022/run` |
| Inspect live events | Connect to `ws://localhost:8080/ws`, or `python scripts\ws_smoke.py` |
