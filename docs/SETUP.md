# Setup guide

Start-to-finish instructions for a Windows machine. Nothing is assumed to be installed.
Follow the sections in order; each ends with a check you can run.

Estimated time: 30–45 minutes, mostly waiting on downloads.

---

## 0. What you are installing and why

| Tool | Why the project needs it | Section |
|------|--------------------------|---------|
| Git | Version control | 1 |
| Python 3.11 or 3.12 | Every AIOps service is Python | 2 |
| WSL 2 | Docker Desktop's engine on Windows | 3 |
| Docker Desktop | Runs TimescaleDB, Redis, Prometheus, Grafana, and later the services themselves | 3 |
| VS Code (optional) | Editing, plus a nice Docker/Python experience | 4 |
| kind or minikube | Local Kubernetes — **not needed yet**, only for module 10 | 8 |

Hardware: 8 GB RAM is the practical minimum, 16 GB is comfortable once the full stack
runs. Roughly 15 GB of free disk.

---

## 1. Git

Check first — it may already be there:

```powershell
git --version
```

If it prints a version, skip ahead. Otherwise download the installer from
[git-scm.com/download/win](https://git-scm.com/download/win) and accept the defaults.
Close and reopen your terminal afterwards so `PATH` refreshes.

---

## 2. Python

```powershell
python --version
```

**Target version: 3.11 or 3.12.**

Python 3.13 will work for modules 1–4, but some scientific wheels used later
(`statsmodels`, and `prophet` if we choose it) still lag behind on 3.13. If you already
have 3.13, you can proceed now and install 3.12 alongside it later, or install 3.12 now
and avoid the detour.

Install from [python.org/downloads](https://www.python.org/downloads/). During install:

- ✅ tick **Add python.exe to PATH**
- ✅ tick **Install pip**

If you have several Python versions, `py -0` lists them and `py -3.12 -m venv .venv`
picks a specific one.

**Check:**

```powershell
python --version    # 3.11.x or 3.12.x
python -m pip --version
```

---

## 3. Docker Desktop (with WSL 2)

Docker runs every piece of infrastructure in this project, so this is the section that
matters most.

### 3.1 Enable WSL 2

Open PowerShell **as Administrator** and run:

```powershell
wsl --install
```

This enables the required Windows features and installs a default Ubuntu distribution.
**Reboot when it asks.**

If WSL was already present, make sure it is up to date and on version 2:

```powershell
wsl --update
wsl --set-default-version 2
wsl --status
```

> On a work laptop this may be blocked by policy. If `wsl --install` fails with a
> permissions error, you need IT to enable virtualisation and the WSL feature. Docker
> Desktop can also run on Hyper-V instead of WSL 2, but WSL 2 is faster and is the
> supported default.


### 3.2 Install Docker Desktop

Download from
[docker.com/products/docker-desktop](https://awww.docker.com/products/docker-desktop/)
and run the installer with **Use WSL 2 instead of Hyper-V** ticked.

Start Docker Desktop and wait for the whale icon in the system tray to stop animating.

### 3.3 Give it enough resources

Docker Desktop → **Settings** → **Resources**:

| Setting | Recommended |
|---------|-------------|
| CPUs | 4 (or half your cores) |
| Memory | 6–8 GB |
| Swap | 2 GB |
| Disk image size | 40 GB+ |

Then **Settings → General**: make sure **Start Docker Desktop when you log in** is on,
so the stack is available without thinking about it.

**Check:**

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

The last command should download a tiny image and print a greeting. If it does, Docker
is working. If you get `error during connect`, Docker Desktop is not running yet — open
it and wait for the tray icon to settle.

---

## 4. VS Code (optional but recommended)

Install from [code.visualstudio.com](https://code.visualstudio.com/), then add these
extensions:

- **Python** (ms-python.python)
- **Pylance** (ms-python.vscode-pylance)
- **Ruff** (charliermarsh.ruff)
- **Docker** (ms-azuretools.vscode-docker)
- **PostgreSQL** (ms-ossdata.vscode-pgsql) — for poking at TimescaleDB

---

## 5. Project bootstrap

Open PowerShell in the project root (`D:\Project`).

### 5.1 Allow scripts to run

The helper scripts are PowerShell. If this is a fresh machine, PowerShell will refuse to
run them until you relax the execution policy for your user:

```powershell
Get-ExecutionPolicy -Scope CurrentUser
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`RemoteSigned` allows local scripts while still blocking unsigned scripts downloaded from
the internet.

### 5.2 Run the bootstrap script

```powershell
.\scripts\bootstrap.ps1
```

It will:

1. verify your Python version
2. create `.venv` in the project root
3. install the shared library (`aiops_common`) in editable mode plus dev tooling
4. copy `.env.example` to `.env` if you do not have one
5. create `data/`, `logs/`, `ml/artifacts/`, `ml/datasets/`
6. import `aiops_common` to prove the install worked
7. tell you whether Docker is reachable

### 5.3 Activate the environment

```powershell
.\.venv\Scripts\Activate.ps1
```

Your prompt gains a `(.venv)` prefix. Do this in every new terminal you use for the
project.

### 5.4 Review `.env`

Open `.env`. For local work the defaults are fine, but note:

- `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` are throwaway local values. Change
  them before any deployment anyone else can reach.
- `HEALING_ENABLED=false` — automated remediation is **off**. That is intentional. We
  turn it on deliberately in module 6 once the safety rails are tested.
- If port `5432` or `6379` is already used on your machine (a local Postgres or Redis),
  change `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` rather than fighting the conflict.
- Hostnames are `localhost` on purpose. This file is read by code running on **your
  machine**, which reaches the containers through their published ports. Services that
  run **inside** Compose get these overridden to the Compose service names
  (`timescaledb`, `redis`, ...) in the Compose file, because container-to-container
  traffic uses Docker DNS. Don't "fix" them to the service names.

---

## 6. Start the data layer

```powershell
.\scripts\stack.ps1 up
```

This starts two containers:

| Container | Image | Port | Role |
|-----------|-------|------|------|
| `aiops-timescaledb` | `timescale/timescaledb:2.17.2-pg16` | 5432 | metric history, incidents, actions |
| `aiops-redis` | `redis:7.4-alpine` | 6379 | event bus + circuit-breaker state |

The first run pulls images (a few hundred MB) and then TimescaleDB executes the SQL in
`infra/timescaledb/init/`, creating tables, the `service_metrics` hypertable, continuous
aggregates, compression and retention policies. Give it about 30 seconds.

> **Important:** those init scripts run **only once**, when the data volume is empty. If
> you change the schema later, either write a migration or wipe and re-create with
> `.\scripts\stack.ps1 reset`.

---

## 7. Verify

```powershell
.\scripts\healthcheck.ps1
```

Expected output: every line `[PASS]`, ending with `All checks passed. Module 1 OK.`

It checks the Docker daemon, container health, the TimescaleDB extension, all eight
tables, the hypertable, the continuous aggregates, Redis `PING`, and runs the Python unit
tests.

### Poking around by hand

```powershell
# Open a SQL prompt inside the database container
docker exec -it aiops-timescaledb psql -U aiops -d aiops

# then, at the psql prompt:
\dt                                    -- list tables
\d+ service_metrics                    -- inspect the hypertable
SELECT * FROM timescaledb_information.hypertables;
SELECT view_name FROM timescaledb_information.continuous_aggregates;
\q
```

```powershell
# Redis
docker exec -it aiops-redis redis-cli
> PING
> INFO server
> exit
```

```powershell
# Unit tests only (no Docker needed)
.\.venv\Scripts\python.exe -m pytest tests -v

# Include the integration tests that hit the real database and event bus
$env:AIOPS_RUN_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest tests -v
```

> A note on the PowerShell scripts: they are deliberately ASCII-only. Windows
> PowerShell 5.1 decodes BOM-less UTF-8 as Windows-1252, which turns an em dash into
> characters it treats as string delimiters, and the script fails to parse. If you edit
> them, keep them ASCII.

---

## 8. Kubernetes — later, not now

Module 2 through 9 run on Docker Compose. That is a deliberate choice: Compose gives a
fast feedback loop while we build the intelligence, and the AIOps logic does not care
what runs the containers.

Once the platform works end to end, module 10 ports it to Kubernetes with
[kind](https://kind.sigs.k8s.io/) (Kubernetes in Docker) so the Kubernetes-native parts
of the project — pod restarts, HPA-driven scaling, deployment rollbacks — run against a
real API server. Nothing to install for that yet.

---

## 9. Everyday commands

```powershell
# Stack
.\scripts\stack.ps1 up                          # start
.\scripts\stack.ps1 ps                          # status
.\scripts\stack.ps1 logs -Service timescaledb -Follow
.\scripts\stack.ps1 down                        # stop, keep data
.\scripts\stack.ps1 reset                       # DESTROY data, re-init schema

# Python
.\.venv\Scripts\Activate.ps1
pytest tests -v
ruff check .
ruff format .
mypy libs services
```

---

## 10. Troubleshooting

**`.\scripts\bootstrap.ps1 : File cannot be loaded because running scripts is disabled`**
Run the `Set-ExecutionPolicy` command in section 5.1.

**`docker : error during connect ... The system cannot find the file specified`**
Docker Desktop is not running. Start it and wait for the tray icon to stop animating.

**`Ports are not available: bind: address already in use`**
Something already listens on 5432 or 6379. Find it with
`Get-NetTCPConnection -LocalPort 5432 | Select-Object OwningProcess`, then either stop it
or change `POSTGRES_HOST_PORT` in `.env` and run `.\scripts\stack.ps1 up` again.

**Healthcheck says tables are missing**
The init scripts only run against an empty volume. `.\scripts\stack.ps1 reset` wipes and
re-initialises.

**`aiops-timescaledb` keeps restarting**
Read the logs: `docker logs aiops-timescaledb --tail 100`. A syntax error in one of the
`infra/timescaledb/init/*.sql` files is the usual cause, and the message names the file
and line.

**`pip install` fails building a wheel**
Usually a Python version mismatch. Check `python --version` is 3.11 or 3.12, and re-run
`.\scripts\bootstrap.ps1 -Recreate`.

**WSL 2 uses a lot of memory**
Create `%UserProfile%\.wslconfig` with:

```ini
[wsl2]
memory=8GB
processors=4
swap=2GB
```

Then `wsl --shutdown` and restart Docker Desktop.

---

## Next

Module 1 is done once `healthcheck.ps1` is green. Module 2 adds the observability plane
(Prometheus, Grafana, Loki, Tempo) and a Mojaloop simulator that produces realistic
payment traffic with a daily rhythm — the raw material every model in this project learns
from.
