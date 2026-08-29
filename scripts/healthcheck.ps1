<#
.SYNOPSIS
    Verifies the running stack: container health, database schema, Redis connectivity.

.DESCRIPTION
    Read-only. Run it after `.\scripts\stack.ps1 up` to confirm module 1 is working
    before moving on. Exits with code 1 if any check fails.

.EXAMPLE
    .\scripts\healthcheck.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
# Defined up front because both the observability section and the test section use it.
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
Set-Location $repoRoot

$failures = @()

function Write-Section { param([string]$Name) Write-Host "`n=== $Name ===" -ForegroundColor Cyan }
function Write-Pass { param([string]$Message) Write-Host "  [PASS] $Message" -ForegroundColor Green }
function Write-Fail {
    param([string]$Message)
    Write-Host "  [FAIL] $Message" -ForegroundColor Red
    $script:failures += $Message
}
function Write-Info { param([string]$Message) Write-Host "         $Message" -ForegroundColor DarkGray }

# -----------------------------------------------------------------------------
Write-Section 'Docker'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Fail 'docker command not found - install Docker Desktop (docs/SETUP.md section 2)'
    Write-Host "`nStopping early: nothing else can be checked without Docker." -ForegroundColor Yellow
    exit 1
}
try {
    $serverVersion = (& docker info --format '{{.ServerVersion}}' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $serverVersion) {
        Write-Pass "Docker daemon running (engine $serverVersion)"
    } else {
        Write-Fail 'Docker daemon not responding - start Docker Desktop'
        exit 1
    }
} catch {
    Write-Fail "Docker check failed: $_"
    exit 1
}

# -----------------------------------------------------------------------------
Write-Section 'Containers'
$expected = @('aiops-timescaledb', 'aiops-redis')
foreach ($name in $expected) {
    $state = (& docker inspect --format '{{.State.Status}}' $name 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$name does not exist - run .\scripts\stack.ps1 up"
        continue
    }
    $health = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $name 2>$null)
    if ($state -eq 'running' -and $health -in @('healthy', 'none')) {
        Write-Pass "$name is $state (health: $health)"
    } else {
        Write-Fail "$name is $state (health: $health)"
        Write-Info "docker logs $name --tail 50"
    }
}

# -----------------------------------------------------------------------------
Write-Section 'TimescaleDB'
$dbUser = 'aiops'
$dbName = 'aiops'
if (Test-Path '.env') {
    $envLines = Get-Content '.env' | Where-Object { $_ -match '^\s*[A-Z_]+=' }
    foreach ($line in $envLines) {
        $key, $value = $line -split '=', 2
        if ($key.Trim() -eq 'POSTGRES_USER') { $dbUser = $value.Trim() }
        if ($key.Trim() -eq 'POSTGRES_DB')   { $dbName = $value.Trim() }
    }
}

function Invoke-Psql {
    param([string]$Sql)
    & docker exec aiops-timescaledb psql -U $dbUser -d $dbName -tAc $Sql 2>$null
}

$tsVersion = (Invoke-Psql "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
if ($LASTEXITCODE -eq 0 -and $tsVersion) {
    Write-Pass "TimescaleDB extension installed (version $($tsVersion.Trim()))"
} else {
    Write-Fail 'Could not query the timescaledb extension'
}

$expectedTables = @(
    'service_metrics', 'anomalies', 'traffic_predictions',
    'incidents', 'healing_actions', 'deployments', 'chaos_runs', 'model_runs'
)
$actualTables = (Invoke-Psql "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
$actualList = @()
if ($actualTables) { $actualList = $actualTables -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ } }

foreach ($table in $expectedTables) {
    if ($actualList -contains $table) {
        Write-Pass "table $table exists"
    } else {
        Write-Fail "table $table is missing"
        Write-Info 'Init scripts only run on an empty volume: .\scripts\stack.ps1 reset'
    }
}

$hypertable = (Invoke-Psql "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'service_metrics';")
if ($hypertable -and $hypertable.Trim() -eq '1') {
    Write-Pass 'service_metrics is a hypertable'
} else {
    Write-Fail 'service_metrics is not a hypertable'
}

$caggs = (Invoke-Psql "SELECT count(*) FROM timescaledb_information.continuous_aggregates;")
if ($caggs -and [int]$caggs.Trim() -ge 2) {
    Write-Pass "continuous aggregates created ($($caggs.Trim()))"
} else {
    Write-Fail "expected 2 continuous aggregates, found '$($caggs)'"
}

# -----------------------------------------------------------------------------
Write-Section 'Redis'
$pong = (& docker exec aiops-redis redis-cli ping 2>$null)
if ($pong -and $pong.Trim() -eq 'PONG') {
    Write-Pass 'Redis responds to PING'
} else {
    Write-Fail "Redis did not answer PING (got '$pong')"
}

# -----------------------------------------------------------------------------
Write-Section 'Observability plane (module 2, optional)'
# Only checked if the layer is running. The base stack is valid on its own, so a missing
# observability plane is reported as informational rather than as a failure.
$observabilityRunning = $false
foreach ($name in @('aiops-prometheus', 'aiops-grafana')) {
    $state = (& docker inspect --format '{{.State.Status}}' $name 2>$null)
    if ($LASTEXITCODE -eq 0 -and $state -eq 'running') { $observabilityRunning = $true }
}

if (-not $observabilityRunning) {
    Write-Info 'not running - start with: .\scripts\stack.ps1 up -Layer observability'
} else {
    $simulated = @('ml-api-adapter', 'central-ledger', 'quoting-service',
                   'account-lookup-service', 'transaction-request-service')
    foreach ($name in @('aiops-prometheus', 'aiops-grafana') + $simulated) {
        $state = (& docker inspect --format '{{.State.Status}}' $name 2>$null)
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "$name does not exist"
            continue
        }
        $health = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $name 2>$null)
        if ($state -eq 'running' -and $health -in @('healthy', 'none')) {
            Write-Pass "$name is $state (health: $health)"
        } else {
            Write-Fail "$name is $state (health: $health)"
        }
    }

    # The deep checks - targets, recording rules, exporter accuracy, dashboard panels -
    # live in a dedicated script because they need real PromQL evaluation.
    if (Test-Path $venvPython) {
        & $venvPython scripts\check_observability.py 2>&1 | ForEach-Object { Write-Info $_ }
        if ($LASTEXITCODE -eq 0) {
            Write-Pass 'observability checks pass (targets, rules, exporter, dashboards)'
        } else {
            Write-Fail 'observability checks failed (see output above)'
            Write-Info 'Metrics need ~60s after start-up before rate() windows fill.'
        }
    }
}

Write-Section 'Python foundation'
if (-not (Test-Path $venvPython)) {
    Write-Fail '.venv not found - run .\scripts\bootstrap.ps1'
} else {
    # The stack has already been verified healthy above, so run the integration
    # tests as well. They exercise the real schema and the real event bus.
    $previous = $env:AIOPS_RUN_INTEGRATION
    $env:AIOPS_RUN_INTEGRATION = '1'
    try {
        & $venvPython -m pytest tests -q --no-header 2>&1 | ForEach-Object { Write-Info $_ }
        if ($LASTEXITCODE -eq 0) {
            Write-Pass 'unit and integration tests pass'
        } else {
            Write-Fail 'tests failed (see output above)'
        }
    } finally {
        $env:AIOPS_RUN_INTEGRATION = $previous
    }
}

# -----------------------------------------------------------------------------
Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host '================================' -ForegroundColor Green
    if ($observabilityRunning) {
        Write-Host ' All checks passed. Modules 1 and 2 OK.' -ForegroundColor Green
    } else {
        Write-Host ' All checks passed. Module 1 OK (data layer only).' -ForegroundColor Green
    }
    Write-Host '================================' -ForegroundColor Green
    exit 0
} else {
    Write-Host '================================' -ForegroundColor Red
    Write-Host " $($failures.Count) check(s) failed:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host '================================' -ForegroundColor Red
    exit 1
}
