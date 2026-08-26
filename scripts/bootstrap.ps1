<#
.SYNOPSIS
    One-time developer setup: virtual environment, dependencies, .env file.

.DESCRIPTION
    Safe to re-run. Creates .venv in the repo root, installs the shared library in
    editable mode plus dev tooling, and creates .env from .env.example if missing.

.EXAMPLE
    .\scripts\bootstrap.ps1

.EXAMPLE
    .\scripts\bootstrap.ps1 -Recreate     # throw away .venv and start clean
#>
[CmdletBinding()]
param(
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    !   $Message" -ForegroundColor Yellow }

# -----------------------------------------------------------------------------
Write-Step 'Checking Python'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python not found on PATH. Install Python 3.11+ from https://www.python.org/downloads/ and re-open your terminal.'
}
$versionRaw = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
$version = [version]$versionRaw
if ($version -lt [version]'3.11') {
    throw "Python $versionRaw found, but 3.11 or newer is required."
}
if ($version -ge [version]'3.13') {
    Write-Warn "Python $versionRaw detected. Some ML wheels (statsmodels, prophet) lag on 3.13."
    Write-Warn 'If module 5 fails to install, use Python 3.12 instead.'
}
Write-Ok "Python $versionRaw"

# -----------------------------------------------------------------------------
Write-Step 'Virtual environment (.venv)'
if ($Recreate -and (Test-Path '.venv')) {
    Write-Warn 'Removing existing .venv'
    Remove-Item -Recurse -Force '.venv'
}
if (-not (Test-Path '.venv')) {
    & python -m venv .venv
    Write-Ok 'Created .venv'
} else {
    Write-Ok '.venv already exists'
}

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment looks broken: $venvPython not found. Re-run with -Recreate."
}

# -----------------------------------------------------------------------------
Write-Step 'Installing dependencies'
& $venvPython -m pip install --upgrade pip setuptools wheel --quiet
& $venvPython -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Scroll up for the pip error.' }
Write-Ok 'Dependencies installed'

# -----------------------------------------------------------------------------
Write-Step 'Configuration (.env)'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Ok 'Created .env from .env.example'
    Write-Warn 'Review .env and change the default passwords before any shared deployment.'
} else {
    Write-Ok '.env already exists (left untouched)'
}

# -----------------------------------------------------------------------------
Write-Step 'Local directories'
foreach ($dir in @('data', 'logs', 'ml\artifacts', 'ml\datasets')) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
Write-Ok 'data/, logs/, ml/artifacts/, ml/datasets/ ready'

# -----------------------------------------------------------------------------
Write-Step 'Verifying the shared library imports'
& $venvPython -c "from aiops_common import get_settings, __version__; s = get_settings(); print(f'aiops_common {__version__} | env={s.env} | platform={s.dpi_platform}')"
if ($LASTEXITCODE -ne 0) { throw 'aiops_common failed to import.' }

# -----------------------------------------------------------------------------
Write-Step 'Docker'
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Warn 'Docker not found on PATH.'
    Write-Warn 'Install Docker Desktop: https://www.docker.com/products/docker-desktop/'
    Write-Warn 'See docs/SETUP.md section 2 for step-by-step instructions.'
} else {
    try {
        & docker info --format '{{.ServerVersion}}' | Out-Null
        Write-Ok 'Docker daemon is running'
    } catch {
        Write-Warn 'Docker is installed but the daemon is not responding. Start Docker Desktop.'
    }
}

Write-Host "`nBootstrap complete." -ForegroundColor Green
Write-Host 'Next steps:' -ForegroundColor Green
Write-Host '  1. Activate the venv:   .\.venv\Scripts\Activate.ps1'
Write-Host '  2. Start the stack:     .\scripts\stack.ps1 up'
Write-Host '  3. Verify it:           .\scripts\healthcheck.ps1'
