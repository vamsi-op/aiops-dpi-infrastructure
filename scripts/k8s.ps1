<#
.SYNOPSIS
    Run the whole AIOps platform on a local kind (Kubernetes-in-Docker) cluster.

.DESCRIPTION
    Module 10. The Kubernetes twin of `stack.ps1 up -Layer all`:
      up       create the kind cluster (if missing), build + load images, create
               ConfigMaps from infra/, apply deploy/k8s, wait for every rollout
      images   rebuild the platform images and load them into the cluster
      config   re-create the ConfigMaps (after editing anything under infra/)
      status   pods, deployments and rollout revisions
      healing  turn real remediation on or off (-Off) and restart the affected pods
      down     delete the kind cluster (all cluster state is lost)

    Every endpoint is published on the same host port Compose uses (8080, 3000, 9090,
    5432, 8011-8015, 8020-8024 ...), so the dashboard and every scripts/check_*.py run
    unchanged. Stop the Compose stack first: .\scripts\stack.ps1 down

.EXAMPLE
    .\scripts\k8s.ps1 up
.EXAMPLE
    .\scripts\k8s.ps1 healing          # HEALING_ENABLED=true on the cluster
.EXAMPLE
    .\scripts\k8s.ps1 healing -Off
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'images', 'config', 'status', 'healing', 'down')]
    [string]$Action = 'up',

    [switch]$Off,

    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$ClusterName = 'aiops'
$Namespace = 'aiops'
$Context = "kind-$ClusterName"
$Images = @(
    'aiops/mojaloop-sim:0.1.0',
    'aiops/metrics-agent:0.1.0',
    'aiops/anomaly-detector:0.1.0',
    'aiops/traffic-predictor:0.1.0',
    'aiops/rca-engine:0.1.0',
    'aiops/healing-engine:0.1.0',
    'aiops/api-gateway:0.1.0'
)

function Assert-Tool([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is not installed or not on PATH. $Hint"
    }
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    Write-Host "$Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe exited with code $LASTEXITCODE" }
}

function Invoke-Kubectl([string[]]$Arguments) {
    Invoke-Native 'kubectl' (@('--context', $Context) + $Arguments)
}

# Build a ConfigMap from files and apply it server-side (no 256 KB annotation limit, and
# re-running replaces the contents instead of failing because the map exists).
function Set-ConfigMap([string]$Name, [string[]]$FromFiles) {
    $kargs = @('--context', $Context, '-n', $Namespace, 'create', 'configmap', $Name,
              '--dry-run=client', '-o', 'yaml')
    foreach ($f in $FromFiles) { $kargs += "--from-file=$f" }
    $yaml = & kubectl @kargs
    if ($LASTEXITCODE -ne 0) { throw "could not render ConfigMap $Name" }
    $yaml | & kubectl --context $Context apply --server-side --force-conflicts -f - | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not apply ConfigMap $Name" }
    Write-Host "    configmap/$Name" -ForegroundColor DarkGray
}

function Initialize-ConfigMaps {
    Write-Host '==> ConfigMaps (from infra/ and deploy/k8s/config/)' -ForegroundColor Cyan
    Invoke-Kubectl @('apply', '-f', 'deploy/k8s/namespace.yaml')
    Set-ConfigMap 'timescaledb-init' @('infra/timescaledb/init')
    Set-ConfigMap 'prometheus-config' @('deploy/k8s/config/prometheus.yml')
    Set-ConfigMap 'prometheus-rules' @('infra/prometheus/rules')
    Set-ConfigMap 'alloy-config' @('deploy/k8s/config/config.alloy')
    Set-ConfigMap 'loki-config' @('infra/loki/loki.yml')
    Set-ConfigMap 'tempo-config' @('infra/tempo/tempo.yml')
    Set-ConfigMap 'alertmanager-config' @('infra/alertmanager/alertmanager.yml')
    Set-ConfigMap 'grafana-datasources' @('infra/grafana/provisioning/datasources')
    Set-ConfigMap 'grafana-dashboard-providers' @('infra/grafana/provisioning/dashboards')
    Set-ConfigMap 'grafana-dashboards' @('infra/grafana/dashboards')
}

function Import-Images {
    Write-Host '==> Building platform images (docker compose build)' -ForegroundColor Cyan
    Invoke-Native 'docker' @('compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.observability.yml',
                             '-f', 'docker-compose.aiops.yml', 'build')
    Write-Host '==> Loading images into the kind node' -ForegroundColor Cyan
    foreach ($image in $Images) {
        Invoke-Native 'kind' @('load', 'docker-image', $image, '--name', $ClusterName)
    }
}

function Wait-Rollouts {
    Write-Host "==> Waiting for rollouts (up to ${TimeoutSeconds}s each)" -ForegroundColor Cyan
    Invoke-Kubectl @('-n', $Namespace, 'rollout', 'status', 'statefulset/timescaledb', "--timeout=${TimeoutSeconds}s")
    $deployments = & kubectl --context $Context -n $Namespace get deployments -o name
    foreach ($d in $deployments) {
        Invoke-Kubectl @('-n', $Namespace, 'rollout', 'status', $d, "--timeout=${TimeoutSeconds}s")
    }
}

# Copy already-trained Isolation Forests from ml/artifacts so detection works at once,
# instead of waiting for enough cluster history to POST /train.
function Copy-Models {
    $models = Get-ChildItem 'ml/artifacts' -Filter '*.joblib' -ErrorAction SilentlyContinue
    if (-not $models) {
        Write-Host '    no trained models in ml/artifacts - run POST http://localhost:8021/train later' -ForegroundColor Yellow
        return
    }
    $pod = (& kubectl --context $Context -n $Namespace get pods -l app.kubernetes.io/name=anomaly-detector `
            -o jsonpath='{.items[0].metadata.name}')
    Write-Host "==> Copying $($models.Count) trained model(s) into $pod" -ForegroundColor Cyan
    foreach ($m in $models) {
        Invoke-Kubectl @('-n', $Namespace, 'cp', "ml/artifacts/$($m.Name)", "${pod}:/app/ml/artifacts/$($m.Name)")
    }
    # Models are loaded at start-up; restart so the detector picks them up.
    Invoke-Kubectl @('-n', $Namespace, 'rollout', 'restart', 'deployment/anomaly-detector')
    Invoke-Kubectl @('-n', $Namespace, 'rollout', 'status', 'deployment/anomaly-detector', "--timeout=${TimeoutSeconds}s")
}

switch ($Action) {
    'up' {
        Assert-Tool 'docker' 'See docs/SETUP.md section 2.'
        Assert-Tool 'kind' 'Install it with: winget install Kubernetes.kind'
        Assert-Tool 'kubectl' 'Docker Desktop ships kubectl; or: winget install Kubernetes.kubectl'

        $clusters = & kind get clusters 2>$null
        if ($clusters -notcontains $ClusterName) {
            Write-Host "==> Creating kind cluster '$ClusterName'" -ForegroundColor Cyan
            Invoke-Native 'kind' @('create', 'cluster', '--config', 'deploy/k8s/kind-cluster.yaml', '--wait', '120s')
        } else {
            Write-Host "==> kind cluster '$ClusterName' already exists" -ForegroundColor Cyan
        }

        Import-Images
        Initialize-ConfigMaps
        Write-Host '==> Applying manifests (kubectl apply -k deploy/k8s)' -ForegroundColor Cyan
        Invoke-Kubectl @('apply', '-k', 'deploy/k8s')
        Wait-Rollouts
        Copy-Models

        Write-Host ''
        Write-Host 'Platform is up on Kubernetes (kind).' -ForegroundColor Green
        Write-Host '  API gateway   http://localhost:8080/docs'
        Write-Host '  Grafana       http://localhost:3000'
        Write-Host '  Prometheus    http://localhost:9090/targets'
        Write-Host '  Dashboard     cd dashboard; npm run dev   (talks to :8080 as before)'
        Write-Host '  Healing is in DRY-RUN. Enable it with: .\scripts\k8s.ps1 healing'
    }
    'images' {
        Import-Images
        Invoke-Kubectl @('-n', $Namespace, 'rollout', 'restart', 'deployment')
        Wait-Rollouts
    }
    'config' {
        Initialize-ConfigMaps
        Invoke-Kubectl @('-n', $Namespace, 'rollout', 'restart', 'deployment')
        Wait-Rollouts
    }
    'status' {
        Invoke-Kubectl @('-n', $Namespace, 'get', 'pods,deployments,statefulsets,hpa', '-o', 'wide')
        Write-Host ''
        Write-Host '==> Mojaloop rollout history (rollback_bad_deploy undoes the latest revision)' -ForegroundColor Cyan
        foreach ($svc in 'ml-api-adapter', 'central-ledger', 'quoting-service', 'account-lookup-service', 'transaction-request-service') {
            & kubectl --context $Context -n $Namespace rollout history "deployment/$svc"
        }
    }
    'healing' {
        $value = if ($Off) { 'false' } else { 'true' }
        $patch = "{`"data`":{`"HEALING_ENABLED`":`"$value`"}}"
        Invoke-Kubectl @('-n', $Namespace, 'patch', 'configmap', 'aiops-env', '--type', 'merge', '-p', $patch)
        # envFrom is read at pod start, so the pods that read the flag must restart.
        Invoke-Kubectl @('-n', $Namespace, 'rollout', 'restart', 'deployment/healing-engine', 'deployment/api-gateway')
        Invoke-Kubectl @('-n', $Namespace, 'rollout', 'status', 'deployment/healing-engine', "--timeout=${TimeoutSeconds}s")
        Write-Host "HEALING_ENABLED=$value on the cluster" -ForegroundColor Green
    }
    'down' {
        Invoke-Native 'kind' @('delete', 'cluster', '--name', $ClusterName)
    }
}
