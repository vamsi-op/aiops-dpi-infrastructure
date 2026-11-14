# Setup

How to install the prerequisites, run the platform on Docker Compose, load training data and
open the dashboard. For Kubernetes see [KUBERNETES.md](KUBERNETES.md); for a cloud VM see
[DEPLOY_VM.md](DEPLOY_VM.md).

---

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | recent | WSL 2 backend on Windows. Give it **6–8 GB memory** and 4 CPUs (*Settings → Resources*). |
| Python | 3.11 or 3.12 | `python --version` |
| Node.js | 20 or later | Only for the dashboard dev server |
| Git | any | |

Hardware: 16 GB RAM recommended (the full stack is about 20 containers), ~15 GB free disk.

On Windows, allow the helper scripts to run once per user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

> On Linux, use `scripts/k8s.sh` for Kubernetes, and run `docker compose` directly in place of
> `stack.ps1` (the Compose files are the same).

---

## 2. Bootstrap

```powershell
.\scripts\bootstrap.ps1
```

This creates `.venv`, installs the shared library (`aiops_common`) and development tooling,
copies `.env.example` to `.env`, and checks that Docker is reachable. Activate the environment in
each new terminal:

```powershell
.\.venv\Scripts\Activate.ps1
```

Review `.env` before the first start. The defaults work locally; see
[CONFIGURATION.md](CONFIGURATION.md) for every setting. Two to note:

- `HEALING_ENABLED=false` — automated remediation is off until you turn it on.
- `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` — local defaults; change them for any shared
  deployment.

---

## 3. Start the stack

```powershell
.\scripts\stack.ps1 up -Layer all
```

`-Layer` selects how much to start: `base` (TimescaleDB, Redis), `observability` (adds
Prometheus, Grafana, Loki, Alloy, Tempo, Alertmanager and the five simulated services), or `all`
(adds the AIOps services). The first start pulls images and initialises the database schema from
`infra/timescaledb/init/`.

Check that everything is up:

```powershell
.\scripts\stack.ps1 ps -Layer all
.\scripts\healthcheck.ps1
```

| Service | URL |
|---|---|
| API gateway | http://localhost:8080 (`/docs` for the OpenAPI UI) |
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| Metrics agent · anomaly detector · traffic predictor · RCA engine · healing engine | http://localhost:8020 – 8024 |
| Simulated services | http://localhost:8011 – 8015 |
| TimescaleDB · Redis | localhost:5432 · localhost:6379 |

---

## 4. Load history and train

The models need history. The repository ships a 28-day dataset (see [DATA.md](DATA.md)); load it
and train the anomaly models:

```powershell
.\.venv\Scripts\python.exe scripts\replay_history.py --from-csv ml\datasets\mojaloop_replay_28d.csv
Invoke-RestMethod -Method Post http://localhost:8021/train -ContentType application/json -Body '{}'
```

The replay shifts the dataset by whole weeks so it ends at the present, keeping weekday
seasonality aligned. The traffic predictor picks the history up on its next cycle (every
10 minutes), or immediately with `Invoke-RestMethod -Method Post http://localhost:8022/run`.

The metrics agent writes its first live sample once each service has a full 5-minute rate window,
so detection starts about five minutes after the stack comes up.

---

## 5. Dashboard

```powershell
cd dashboard
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api` and `/ws` to the gateway on port 8080.
On Kubernetes the dashboard runs in the cluster instead, at http://localhost:8081.

---

## 6. Enable self-healing and run the demo

Set `HEALING_ENABLED=true` in `.env`, then apply it. `up` recreates only the containers whose
configuration changed (a plain `restart` would not re-read `.env`):

```powershell
.\scripts\stack.ps1 up -Layer all
```

Run the chaos scorecard (three scenarios, about 25 minutes including settle time between them):

```powershell
.\.venv\Scripts\python.exe scripts\chaos_demo.py
```

See [OPERATIONS.md](OPERATIONS.md) for what each scenario does and how to read the results.

---

## 7. Tests and checks

```powershell
pytest tests -q                         # unit tests, no Docker needed
$env:AIOPS_RUN_INTEGRATION = '1'
pytest tests -q                         # plus integration tests against the running stack
ruff format --check . ; ruff check . ; mypy libs services
```

Verification scripts check individual components against the running stack:
`scripts/check_observability.py`, `check_metrics_agent.py`, `check_anomaly_detector.py`,
`check_traffic_predictor.py`, `check_api_contract.py` and `ws_smoke.py`.

---

## 8. Everyday commands

```powershell
.\scripts\stack.ps1 up -Layer all                      # start
.\scripts\stack.ps1 ps -Layer all                       # status
.\scripts\stack.ps1 logs -Layer all -Service rca-engine -Follow   # follow one service
.\scripts\stack.ps1 down                               # stop, keep data
.\scripts\stack.ps1 reset                              # delete all data and re-initialise
```

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `running scripts is disabled on this system` | Run the `Set-ExecutionPolicy` command in section 1. |
| `error during connect` from Docker | Docker Desktop is not running. Start it and wait for it to settle. |
| `Ports are not available` | Something already uses 5432 or 6379. Change `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` in `.env`. |
| Tables missing after a schema change | Init scripts run only on an empty volume. Apply the file in `infra/timescaledb/migrations/`, or `stack.ps1 reset`. |
| Dashboard panels empty | The gateway is not reachable on :8080, or no samples yet — the metrics agent needs a 5-minute warm-up. |
| No forecasts | History not loaded, or the predictor has not run since. `POST http://localhost:8022/run`. |
| Detector reports `no_model` | Models not trained. `POST http://localhost:8021/train`. |
| WSL 2 uses too much memory | Limit it in `%UserProfile%\.wslconfig` (`[wsl2]` `memory=8GB`), then `wsl --shutdown`. |
