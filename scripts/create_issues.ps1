<#
.SYNOPSIS
    Creates the module labels and backlog issues on GitHub in one go.

.DESCRIPTION
    Files one issue per outstanding module, plus the "good first issue" items, using the
    bodies in docs/ISSUES.md as the source of truth for scope and acceptance criteria.

    Requires the GitHub CLI, authenticated:
        winget install GitHub.cli
        gh auth login

    Safe to re-run with -DryRun to preview. Without -DryRun it will create real issues,
    so check the output of a dry run first.

.EXAMPLE
    .\scripts\create_issues.ps1 -DryRun
.EXAMPLE
    .\scripts\create_issues.ps1
.EXAMPLE
    .\scripts\create_issues.ps1 -Repo myorg/aiops-dpi
#>
[CmdletBinding()]
param(
    [string]$Repo,
    [switch]$DryRun,
    [switch]$SkipLabels
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step { param([string]$m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "    !   $m" -ForegroundColor Yellow }

# -----------------------------------------------------------------------------
Write-Step 'Checking prerequisites'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI not found. Install with: winget install GitHub.cli   then: gh auth login'
}
& gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not authenticated. Run: gh auth login' }
Write-Ok 'gh is installed and authenticated'

if (-not $Repo) {
    # Capture stderr into the variable rather than letting gh print its own error, so a
    # missing remote produces the actionable message below instead of a wall of noise.
    $detected = (& gh repo view --json nameWithOwner -q .nameWithOwner 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw @'
Could not detect the GitHub repository from this directory.

Either create and push the repo first:
    git init -b main
    git add .
    git commit -m "Initial commit"
    gh repo create <name> --private --source=. --push

or pass it explicitly:
    .\scripts\create_issues.ps1 -Repo owner/name
'@
    }
    $Repo = ($detected | Select-Object -First 1).Trim()
}
Write-Ok "target repository: $Repo"

# -----------------------------------------------------------------------------
$labels = @(
    @{ Name = 'module';           Color = '0e8a16'; Description = 'A numbered build task from docs/MODULES.md' },
    @{ Name = 'infra';            Color = '1d76db'; Description = 'Infrastructure, monitoring, collection' },
    @{ Name = 'ml';               Color = '5319e7'; Description = 'Models, training, evaluation' },
    @{ Name = 'automation';       Color = 'b60205'; Description = 'Remediation, chaos, orchestration' },
    @{ Name = 'frontend';         Color = 'fbca04'; Description = 'Dashboard' },
    @{ Name = 'good first issue'; Color = '7057ff'; Description = 'Small, self-contained, safe to learn on' },
    @{ Name = 'blocked';          Color = 'd93f0b'; Description = 'Waiting on another issue' }
)

if (-not $SkipLabels) {
    Write-Step 'Creating labels'
    foreach ($label in $labels) {
        if ($DryRun) { Write-Host "    [dry run] label: $($label.Name)"; continue }
        # --force updates the colour/description if the label already exists.
        & gh label create $label.Name --repo $Repo --color $label.Color `
            --description $label.Description --force 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "label $($label.Name)" }
        else { Write-Warn "could not create label $($label.Name)" }
    }
}

# -----------------------------------------------------------------------------
# Issue bodies are kept short here and point at docs/ISSUES.md for the full scope and
# acceptance criteria, so the two never drift out of sync.
# -----------------------------------------------------------------------------
$docLink = "Full scope, out-of-scope boundary and acceptance criteria: [docs/ISSUES.md](docs/ISSUES.md)"

$issues = @(
    @{
        Title  = '[Module 2] Observability plane + Mojaloop simulator'
        Labels = 'module,infra'
        Body   = @"
Stand up the observability stack and a workload that produces realistic telemetry.

**Deliver**
- ``docker-compose.observability.yml``: Prometheus, Grafana, Loki, Promtail, Tempo, Alertmanager
- Prometheus scrape config + recording rules; Grafana datasources and a starter dashboard, provisioned from files
- ``services/mojaloop_sim/``: five simulated Mojaloop services exposing realistic ``/metrics``, generating traffic with daily **and** weekly seasonality, and accepting fault-injection commands (latency, errors, memory growth, crash)
- JSON logs to Loki, traces to Tempo

**Out of scope:** reading metrics into TimescaleDB (module 3), interpreting them.

**Done when**
- [ ] ``.\scripts\stack.ps1 up -Layer observability`` comes up healthy
- [ ] Prometheus ``/targets`` all ``UP``
- [ ] Grafana at :3000 has the datasource and dashboard already present, no clicking
- [ ] The 24h traffic chart shows a visible daily curve, not flat noise
- [ ] Simulator logs queryable via Loki in Grafana Explore
- [ ] Fault injection visibly changes metrics within one scrape
- [ ] ``.\scripts\healthcheck.ps1`` still green

**Start here** - every other module needs telemetry. ``SimulatorSettings`` and the
``MOJALOOP_SIM_*`` env vars already exist.

$docLink
"@
    },
    @{
        Title  = '[Module 3] Metrics collection agent + DPI-specific exporter'
        Labels = 'module,infra'
        Body   = @"
Collect from the observability plane, normalise, and persist.

**Deliver**
- ``services/metrics_agent/``: scheduled PromQL collection -> ``MetricSample`` -> batch write to TimescaleDB + publish to ``aiops.metrics``
- DPI-specific exporter: transaction processing time, verification latency, API success ratio, settlement lag
- Backfill command so modules 4 and 5 have training history immediately

**Out of scope:** detection thresholds and scoring (module 4).

**Done when**
- [ ] ``SELECT count(*) FROM service_metrics`` grows every scrape interval
- [ ] ``service_metrics_1m`` populates (mind the continuous-aggregate bucket rule - see the comment in ``tests/test_integration_stack.py``)
- [ ] Agent ``/metrics`` exposes collection latency, rows written, error counter
- [ ] Prometheus being briefly unreachable does not kill the agent
- [ ] Backfill produces >= 7 days of plausible history
- [ ] Unit tests for normalisation, integration test for the write path

``PrometheusClient`` and ``Database.execute_many()`` are ready to use. This service is the
**only** place that knows where metrics come from; everything downstream sees a uniform
``MetricSample``.

Depends on: Module 2

$docLink
"@
    },
    @{
        Title  = '[Module 4] Anomaly detection with Isolation Forest'
        Labels = 'module,ml'
        Body   = @"
Detect unusual behaviour before it becomes an outage.

**Deliver**
- Per-service baselines (rolling mean/sigma, hour-of-day aware)
- ``IsolationForest`` over ``ANOMALY_FEATURES`` with a ``StandardScaler``
- Score normalised to 0-1, mapped to ``Severity``
- Feature attribution: which signals drove it, and by how many sigma
- Anomaly ``kind`` classification so module 6 can match a playbook
- Training job writing to ``model_runs`` plus an artifact in ``ml/artifacts/``

**Out of scope:** what to do about it (module 6), grouping into incidents (module 7).

**Done when**
- [ ] Consumes ``aiops.metrics``, publishes ``aiops.anomalies``, persists to ``anomalies``
- [ ] An injected fault produces an anomaly within one scrape interval
- [ ] ``kind`` correct for memory leak, latency spike, error burst, traffic surge
- [ ] ``contributing_features`` names what a human would have pointed at
- [ ] False-positive rate on normal traffic near ``ANOMALY_CONTAMINATION``, measured and stated in the PR
- [ ] Cold start handled: no trained model yet must not crash the service

``ANOMALY_FEATURES`` is **append-only** - reordering silently invalidates saved models.
``AnomalyEvent`` already carries ``feature_snapshot``, ``feature_deviations`` and
``contributing_features``, and the dashboard renders all three.

Depends on: Module 3 (or use ``scripts/seed_demo_data.py --days 7``)

$docLink
"@
    },
    @{
        Title  = '[Module 5] Traffic prediction and pre-emptive scaling'
        Labels = 'module,ml'
        Body   = @"
Forecast load so scaling happens before the spike, not during it.

**Deliver**
- Per-service TPS forecasts at 1/6/24h, trained on ``service_metrics_1h``
- Backtesting with MAPE recorded per model version
- Predicted peak -> replica recommendation from per-replica capacity
- Publish ``aiops.predictions``, persist to ``traffic_predictions``

**Out of scope:** executing the scale action (module 6).

**Done when**
- [ ] Forecast visibly tracks the simulator's known daily curve
- [ ] Recorded MAPE beats a seasonal-naive baseline, both numbers in the PR
- [ ] A predicted peak yields ``scale_out`` **before** the spike
- [ ] Confidence bounds populated on ``ForecastPoint``
- [ ] Insufficient history raises ``InsufficientDataError`` rather than emitting a bad forecast
- [ ] Backtest reproducible from a command and recorded in ``model_runs``

**Check library support for your Python version first** - Prophet and some ``statsmodels``
builds lag on 3.13. SARIMAX, a gradient-boosted regressor over lag/calendar features, or
seasonal-naive-plus-trend all work. Start with the baseline so you have something to beat.

Depends on: Module 3 (or seed data)

$docLink
"@
    },
    @{
        Title  = '[Module 6] Self-healing playbook engine'
        Labels = 'module,automation'
        Body   = @"
Act on anomalies automatically, behind safety rails.

**Deliver**
- Playbooks: ``restart_on_oom``, ``circuit_break_cascade``, ``rollback_bad_deploy``, ``scale_out_preemptive``, ``scale_in_idle``, ``clear_queue_backlog``
- Four rails, each with its own ``skipped_*`` status: master switch, per-target cooldown, hourly budget, minimum confidence
- Executor behind an interface: Docker now, Kubernetes in module 10
- Before/after metric snapshots on every action

**Out of scope:** determining cause (module 7), detection (module 4).

**Done when**
- [ ] ``HEALING_ENABLED=false`` records ``skipped_dry_run`` and **executes nothing**
- [ ] Enabled, a memory-pressure anomaly restarts the container and ``after_snapshot`` shows recovery
- [ ] Each rail has a unit test proving it blocks
- [ ] Every action persisted to ``healing_actions``, including skipped ones
- [ ] Idempotent: a duplicate anomaly does not double-restart
- [ ] A failing playbook records ``failed`` with a useful message and does not take the service down

**The rails are as much the deliverable as the playbooks.** A blocked action is a *correct*
outcome - that is why there are four distinct ``skipped_*`` statuses and why they are
persisted. ``HealingSettings`` and the Redis ``incr_with_ttl`` / ``set_flag`` helpers exist
for budget and cooldown state.

Depends on: Modules 4, 7

$docLink
"@
    },
    @{
        Title  = '[Module 7] Root cause analysis engine'
        Labels = 'module,ml'
        Body   = @"
Turn many simultaneous alarms into one incident with one named cause.

**Deliver** - correlate four ways, produce ranked evidence-backed hypotheses:
- **Temporal**: group anomalies in a sliding window
- **Topological**: walk ``MOJALOOP_DEPENDENCIES`` upstream to the origin
- **Change**: match against the ``deployments`` table
- **Event**: traffic surges, active chaos runs
- Incident lifecycle (open / mitigating / resolved) and a plain-English ``ai_summary``

**Out of scope:** remediation (module 6).

**Done when**
- [ ] A ``central-ledger`` fault cascading to ``quoting-service`` and ``ml-api-adapter`` produces **one** incident naming ``central-ledger``, not three
- [ ] A deploy 10 min before an error burst yields a ``change`` hypothesis targeting that deployment
- [ ] A traffic surge with no service fault yields ``event``, not ``service``
- [ ] Hypotheses ordered by confidence, each with concrete ``evidence`` strings
- [ ] Anomalies stamped with their ``incident_id``
- [ ] Incidents resolve automatically when the anomalies clear

The dependency graph in ``constants.py`` exists and is tested acyclic. The dashboard's
incident page already renders ranked hypotheses with evidence, so good output shows
immediately.

Depends on: Module 4

$docLink
"@
    },
    @{
        Title  = '[Module 9] Chaos engineering demo'
        Labels = 'module,automation'
        Body   = @"
Break things deliberately and measure how well the platform responds.

**Deliver**
- ``services/chaos_agent/``: ``kill_service``, ``memory_hog``, ``cpu_hog``, ``network_latency``, ``error_injection``, ``traffic_spike``, ``bad_deploy``
- Wired to the simulator's fault-injection endpoints and the Docker API
- Scorecard per ``chaos_runs`` row: time-to-detect, time-to-RCA, time-to-remediate
- Scripted unattended scenarios plus a demo runbook

**Out of scope:** detection and healing logic - chaos only breaks and measures.

**Done when**
- [ ] ``.\scripts\demo.ps1`` runs a full scenario unattended and prints the scorecard
- [ ] Each experiment is detected, attributed and remediated, with timings recorded
- [ ] Every experiment auto-reverts after ``duration_seconds``, even if the agent crashes
- [ ] The dashboard chaos page can start a run and shows it live
- [ ] ``docs/DEMO.md`` is followable cold by someone else

This is what makes the project demonstrable, so budget time for it. ``ChaosRun``, the
``chaos_runs`` table and the dashboard chaos page already exist - look at the ``cpu_hog``
run produced by ``seed_demo_data.py`` for the target shape.

Depends on: Modules 2, 4, 6, 7

$docLink
"@
    },
    @{
        Title  = '[Module 10] Kubernetes migration (stretch)'
        Labels = 'module,infra'
        Body   = @"
Move from Compose to real Kubernetes.

**Deliver**
- ``kind`` cluster definition
- Manifests or a Helm chart for the platform
- ``metrics_agent`` additionally reads ``kube-state-metrics`` and cAdvisor for real replica counts, restarts and OOMKill events
- Healing playbooks via the Kubernetes API: pod deletion, HPA patching, ``rollout undo``
- Real Mojaloop Helm deployment replacing the simulator

**Done when**
- [ ] One command creates the cluster and deploys the platform
- [ ] Playbooks work against real pods
- [ ] Predictive scaling drives a real HPA or replica patch
- [ ] **Nothing in modules 4, 5, 7 or 8 needed changing** - confirm this in the PR

``docs/ARCHITECTURE.md`` section 8 lists what changes and what does not. If this needs
edits to the models, correlation logic or dashboard, the abstraction leaked - raise it here.

Depends on: Modules 2-9

$docLink
"@
    },
    @{
        Title  = 'Add a Makefile / task runner for the common commands'
        Labels = 'good first issue'
        Body   = @"
The pre-PR check sequence in CONTRIBUTING.md section 5 is five separate commands. Wrap
them: ``make check``, ``make test``, ``make up``, ``make seed``.

Keep the PowerShell scripts working - Windows is the primary environment here, so this is
an addition, not a replacement.

$docLink
"@
    },
    @{
        Title  = 'Dockerfiles for the API gateway and dashboard'
        Labels = 'good first issue,infra'
        Body   = @"
``services/api_gateway/`` and ``dashboard/`` currently only run from source. Add
multi-stage Dockerfiles and a ``docker-compose.aiops.yml`` entry so the whole platform
starts with one command.

Note: services running **inside** Compose need ``POSTGRES_HOST=timescaledb`` and
``REDIS_HOST=redis``, not ``localhost``. See the comment at the top of ``.env.example`` -
the compose file should override those for container services.

**Done when**
- [ ] ``docker compose -f docker-compose.yml -f docker-compose.aiops.yml up -d`` serves the API and dashboard
- [ ] Images build in CI
- [ ] Non-root user in both images

$docLink
"@
    },
    @{
        Title  = 'Configure ESLint for the dashboard'
        Labels = 'good first issue,frontend'
        Body   = @"
``dashboard/package.json`` declares a ``lint`` script but ESLint is not configured, so
``npm run lint`` fails.

**Done when**
- [ ] Flat config for TypeScript + React
- [ ] ``npm run lint`` passes cleanly
- [ ] The step is added to the ``dashboard`` job in ``.github/workflows/ci.yml``

$docLink
"@
    },
    @{
        Title  = 'Audit dashboard loading and error states'
        Labels = 'good first issue,frontend'
        Body   = @"
Check all seven pages for what renders while a request is in flight, and after one fails.

An ops dashboard must never show a blank panel with no explanation. The reasoning is the
same as the stale-service bug recorded under module 8 in ``docs/MODULES.md``: silence looks
identical to "nothing is wrong", which is the worst thing a monitoring tool can do.

**Done when**
- [ ] Every panel has an explicit loading state
- [ ] Every panel has an error state that says what failed and what to try
- [ ] Empty states explain *why* it is empty (e.g. "module 4 is not running yet") rather than showing an empty chart

$docLink
"@
    },
    @{
        Title  = 'Grafana dashboard for the AIOps services themselves'
        Labels = 'good first issue,infra'
        Body   = @"
Every service already exposes ``aiops_http_requests_total``,
``aiops_http_request_duration_seconds``, ``aiops_service_up`` and ``aiops_service_ready``
through ``build_app()``. Nothing visualises them.

Provision a Grafana dashboard so the platform can be watched while it watches Mojaloop -
an AIOps tool that dies silently is worse than no AIOps tool.

**Done when**
- [ ] Dashboard JSON provisioned from ``infra/grafana/`` (not hand-created in the UI)
- [ ] Shows per-service up/ready, request rate, error rate and p99 latency

Depends on: Module 2 (needs Grafana)

$docLink
"@
    }
)

# -----------------------------------------------------------------------------
Write-Step "Creating $($issues.Count) issues"
$created = 0
foreach ($issue in $issues) {
    if ($DryRun) {
        Write-Host "    [dry run] $($issue.Title)" -ForegroundColor DarkGray
        Write-Host "              labels: $($issue.Labels)" -ForegroundColor DarkGray
        continue
    }

    # Body goes via a temp file: it is long and contains characters that do not survive
    # being passed as a command-line argument reliably.
    $bodyFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($bodyFile, $issue.Body, (New-Object System.Text.UTF8Encoding($false)))
        $url = & gh issue create --repo $Repo --title $issue.Title `
            --label $issue.Labels --body-file $bodyFile 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "$($issue.Title)"
            Write-Host "        $url" -ForegroundColor DarkGray
            $created++
        } else {
            Write-Warn "failed: $($issue.Title)"
            Write-Host "        $url" -ForegroundColor DarkGray
        }
    } finally {
        Remove-Item $bodyFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ''
if ($DryRun) {
    Write-Host "Dry run complete. $($issues.Count) issues would be created in $Repo." -ForegroundColor Green
    Write-Host 'Re-run without -DryRun to create them.' -ForegroundColor Green
} else {
    Write-Host "Created $created of $($issues.Count) issues in $Repo." -ForegroundColor Green
    Write-Host "Review them at: https://github.com/$Repo/issues" -ForegroundColor Green
}
