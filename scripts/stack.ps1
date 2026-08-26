<#
.SYNOPSIS
    Wrapper around `docker compose` that assembles the right set of compose files.

.DESCRIPTION
    The stack is split into layers so you can run only what you need:
      base          data layer: TimescaleDB + Redis                 (module 1)
      observability Prometheus, Grafana, Loki, Tempo, Alertmanager   (module 2)
      aiops         the Python AIOps services                       (modules 3-9)
      all           every layer

.PARAMETER Action
    up | down | restart | ps | logs | pull | config | reset

.PARAMETER Layer
    base | observability | aiops | all   (default: base)

.EXAMPLE
    .\scripts\stack.ps1 up
.EXAMPLE
    .\scripts\stack.ps1 up -Layer all
.EXAMPLE
    .\scripts\stack.ps1 logs -Service timescaledb
.EXAMPLE
    .\scripts\stack.ps1 reset          # destroys volumes, re-runs DB init scripts
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'restart', 'ps', 'logs', 'pull', 'config', 'reset')]
    [string]$Action = 'up',

    [ValidateSet('base', 'observability', 'aiops', 'all')]
    [string]$Layer = 'base',

    [string]$Service,

    [switch]$Follow
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker is not installed or not on PATH. See docs/SETUP.md section 2.'
}
if (-not (Test-Path '.env')) {
    Write-Host 'No .env found - creating it from .env.example' -ForegroundColor Yellow
    Copy-Item '.env.example' '.env'
}

# Build the -f argument list for the requested layer, skipping files that do not
# exist yet (later modules add them).
$files = @('docker-compose.yml')
switch ($Layer) {
    'observability' { $files += 'docker-compose.observability.yml' }
    'aiops'         { $files += 'docker-compose.aiops.yml' }
    'all'           { $files += @('docker-compose.observability.yml', 'docker-compose.aiops.yml') }
}

$composeArgs = @()
foreach ($file in $files) {
    if (Test-Path $file) {
        $composeArgs += @('-f', $file)
    } else {
        Write-Host "    skipping $file (not created yet)" -ForegroundColor DarkGray
    }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    Write-Host "docker compose $($composeArgs -join ' ') $($Arguments -join ' ')" -ForegroundColor DarkGray
    & docker compose @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker compose exited with code $LASTEXITCODE" }
}

switch ($Action) {
    'up' {
        Invoke-Compose @('up', '-d', '--remove-orphans')
        Write-Host "`nWaiting for containers to report healthy..." -ForegroundColor Cyan
        Start-Sleep -Seconds 5
        Invoke-Compose @('ps')
        Write-Host "`nRun .\scripts\healthcheck.ps1 to verify connectivity." -ForegroundColor Green
    }
    'down'    { Invoke-Compose @('down', '--remove-orphans') }
    'restart' {
        if ($Service) { Invoke-Compose @('restart', $Service) } else { Invoke-Compose @('restart') }
    }
    'ps'      { Invoke-Compose @('ps') }
    'logs'    {
        $logArgs = @('logs', '--tail', '200')
        if ($Follow) { $logArgs += '-f' }
        if ($Service) { $logArgs += $Service }
        Invoke-Compose $logArgs
    }
    'pull'    { Invoke-Compose @('pull') }
    'config'  { Invoke-Compose @('config') }
    'reset'   {
        Write-Host 'This DELETES all database and Redis data, then recreates the stack.' -ForegroundColor Red
        Write-Host 'The TimescaleDB init scripts in infra/timescaledb/init will run again.' -ForegroundColor Red
        $answer = Read-Host 'Type RESET to continue'
        if ($answer -ne 'RESET') { Write-Host 'Aborted.' -ForegroundColor Yellow; return }
        Invoke-Compose @('down', '-v', '--remove-orphans')
        Invoke-Compose @('up', '-d')
        Write-Host 'Stack reset. Give TimescaleDB ~30s to run its init scripts.' -ForegroundColor Green
    }
}
