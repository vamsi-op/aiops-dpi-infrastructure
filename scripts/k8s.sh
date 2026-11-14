#!/usr/bin/env bash
# Run the AIOps platform on a kind cluster - the Linux / cloud VM twin of scripts/k8s.ps1.
#
#   scripts/k8s.sh up [--public]   create cluster, build + load images, ConfigMaps, apply, wait
#   scripts/k8s.sh images          rebuild images, load them, restart pods
#   scripts/k8s.sh config          re-create ConfigMaps from infra/, restart pods
#   scripts/k8s.sh status          pods, deployments, rollout history
#   scripts/k8s.sh healing [--off] HEALING_ENABLED=true|false, restart healing-engine + gateway
#   scripts/k8s.sh seed            load 28 days of history (the traffic forecaster needs it)
#   scripts/k8s.sh credentials     print the dashboard / Grafana logins (public mode)
#   scripts/k8s.sh down            delete the cluster
#
# --public is for a VM on the internet (docs/DEPLOY_VM.md). It is remembered, so later
# commands keep the VM locked down without repeating the flag. It:
#   * creates the cluster from deploy/vm/kind-cluster.yaml: only :80 (dashboard) and
#     :3000 (Grafana) listen publicly, everything else on 127.0.0.1
#   * puts the dashboard behind HTTP basic auth (anyone with the URL could otherwise
#     trigger chaos experiments), and turns off Grafana's anonymous access
#   * generates both passwords once into ~/.aiops/ (mode 600)
set -euo pipefail

CLUSTER=aiops
NS=aiops
CTX="kind-$CLUSTER"
TIMEOUT="${TIMEOUT:-300}"
STATE_DIR="$HOME/.aiops"
PUBLIC_MARKER="$STATE_DIR/public"
IMAGES=(
  aiops/mojaloop-sim:0.1.0
  aiops/metrics-agent:0.1.0
  aiops/anomaly-detector:0.1.0
  aiops/traffic-predictor:0.1.0
  aiops/rca-engine:0.1.0
  aiops/healing-engine:0.1.0
  aiops/api-gateway:0.1.0
  aiops/dashboard:0.1.0
)

cd "$(dirname "$0")/.."

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
kc()   { kubectl --context "$CTX" "$@"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "$1 is not installed. $2" >&2; exit 1; }
}

is_public() { [[ -f "$PUBLIC_MARKER" ]]; }

set_configmap() {  # name, then one or more --from-file sources
  local name=$1; shift
  local args=()
  for f in "$@"; do args+=("--from-file=$f"); done
  kc -n "$NS" create configmap "$name" "${args[@]}" --dry-run=client -o yaml \
    | kc apply --server-side --force-conflicts -f - >/dev/null
  echo "    configmap/$name"
}

init_configmaps() {
  info "ConfigMaps (from infra/ and deploy/k8s/config/)"
  kc apply -f deploy/k8s/namespace.yaml
  set_configmap timescaledb-init infra/timescaledb/init
  # The monitored DPI system's service graph (aiops_common.topology).
  set_configmap aiops-topology topology.json=config/topology.mojaloop.json
  set_configmap prometheus-config deploy/k8s/config/prometheus.yml
  set_configmap prometheus-rules infra/prometheus/rules
  set_configmap alloy-config deploy/k8s/config/config.alloy
  set_configmap loki-config infra/loki/loki.yml
  set_configmap tempo-config infra/tempo/tempo.yml
  set_configmap alertmanager-config infra/alertmanager/alertmanager.yml
  set_configmap grafana-datasources infra/grafana/provisioning/datasources
  set_configmap grafana-dashboard-providers infra/grafana/provisioning/dashboards
  set_configmap grafana-dashboards infra/grafana/dashboards
}

import_images() {
  info "Building platform images"
  docker compose -f docker-compose.yml -f docker-compose.observability.yml \
    -f docker-compose.aiops.yml build
  docker build -t aiops/dashboard:0.1.0 dashboard
  info "Loading images into the kind node"
  for image in "${IMAGES[@]}"; do
    kind load docker-image "$image" --name "$CLUSTER"
  done
}

wait_rollouts() {
  info "Waiting for rollouts (up to ${TIMEOUT}s each)"
  kc -n "$NS" rollout status statefulset/timescaledb --timeout="${TIMEOUT}s"
  for d in $(kc -n "$NS" get deployments -o name); do
    kc -n "$NS" rollout status "$d" --timeout="${TIMEOUT}s"
  done
}

copy_models() {
  shopt -s nullglob
  local models=(ml/artifacts/*.joblib)
  shopt -u nullglob
  if (( ${#models[@]} == 0 )); then
    warn "    no trained models in ml/artifacts - POST /train on the anomaly detector later"
    return
  fi
  local pod
  pod=$(kc -n "$NS" get pods -l app.kubernetes.io/name=anomaly-detector \
        -o jsonpath='{.items[0].metadata.name}')
  info "Copying ${#models[@]} trained model(s) into $pod"
  for m in "${models[@]}"; do
    kc -n "$NS" cp "$m" "$pod:/app/ml/artifacts/$(basename "$m")"
  done
  kc -n "$NS" rollout restart deployment/anomaly-detector
  kc -n "$NS" rollout status deployment/anomaly-detector --timeout="${TIMEOUT}s"
}

# A password generated once and reused, so re-running `up` does not lock you out.
secret_file() {
  local file="$STATE_DIR/$1"
  if [[ ! -s "$file" ]]; then
    ( umask 077; openssl rand -base64 24 | tr -d '/+=\n' | cut -c1-20 > "$file" )
  fi
  echo "$file"
}

# The manifests read optional Secrets (dashboard-auth, dashboard-public, grafana-public)
# that only exist here, so re-applying the manifests never undoes the lock-down. Created
# BEFORE the manifests are applied, so on a fresh cluster Grafana and the dashboard are
# never reachable unprotected, not even for a moment.
harden_public() {
  info "Locking down public endpoints"
  local dash_pw graf_pw
  dash_pw=$(cat "$(secret_file dashboard-password)")
  graf_pw=$(cat "$(secret_file grafana-password)")

  printf 'admin:%s
' "$(openssl passwd -apr1 "$dash_pw")"     | kc -n "$NS" create secret generic dashboard-auth --from-file=htpasswd=/dev/stdin         --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc -n "$NS" create secret generic dashboard-public       --from-literal=DASHBOARD_AUTH="AIOps Dashboard"       --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc -n "$NS" create secret generic grafana-public       --from-literal=GF_AUTH_ANONYMOUS_ENABLED=false       --from-literal=GF_SECURITY_ADMIN_PASSWORD="$graf_pw"       --dry-run=client -o yaml | kc apply -f - >/dev/null

}

# envFrom is read at pod start: pick up the Secrets on a cluster that was already
# running before it went public (a no-op restart otherwise).
restart_public_endpoints() {
  kc -n "$NS" rollout restart deployment/dashboard deployment/grafana >/dev/null
  kc -n "$NS" rollout status deployment/dashboard --timeout="${TIMEOUT}s"
  kc -n "$NS" rollout status deployment/grafana --timeout="${TIMEOUT}s"
}

print_credentials() {
  if ! is_public; then
    echo "Not in public mode: the dashboard has no login and Grafana is anonymous."
    return
  fi
  echo "Dashboard  user: admin  password: $(cat "$STATE_DIR/dashboard-password")"
  echo "Grafana    user: admin  password: $(cat "$STATE_DIR/grafana-password")"
}

cmd=${1:-up}; shift || true
case "$cmd" in
  up)
    public=false
    for arg in "$@"; do [[ "$arg" == "--public" ]] && public=true; done
    need docker "Run deploy/vm/setup.sh, or see docs/SETUP.md."
    need kind "Run deploy/vm/setup.sh."
    need kubectl "Run deploy/vm/setup.sh."
    if $public; then
      need openssl "sudo apt-get install -y openssl"
      mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"; touch "$PUBLIC_MARKER"
    fi

    if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
      info "kind cluster '$CLUSTER' already exists"
    else
      config=deploy/k8s/kind-cluster.yaml
      is_public && config=deploy/vm/kind-cluster.yaml
      info "Creating kind cluster '$CLUSTER' from $config"
      kind create cluster --config "$config" --wait 120s
    fi

    import_images
    init_configmaps
    if is_public; then harden_public; fi
    info "Applying manifests"
    kc apply -k deploy/k8s
    wait_rollouts
    copy_models
    if is_public; then restart_public_endpoints; fi

    echo
    ok "Platform is up on Kubernetes (kind)."
    if is_public; then
      ip=$(curl -fsS --max-time 3 https://checkip.amazonaws.com 2>/dev/null || echo "<vm-public-ip>")
      echo "  Dashboard   http://$ip/"
      echo "  Grafana     http://$ip:3000/"
      print_credentials
    else
      echo "  Dashboard   http://localhost:8081"
      echo "  API gateway http://localhost:8080/docs"
      echo "  Grafana     http://localhost:3000"
    fi
    echo "  Healing is in DRY-RUN. Enable it with: scripts/k8s.sh healing"
    ;;
  images)
    import_images
    kc -n "$NS" rollout restart deployment
    wait_rollouts
    ;;
  config)
    init_configmaps
    kc -n "$NS" rollout restart deployment
    wait_rollouts
    ;;
  status)
    kc -n "$NS" get pods,deployments,statefulsets -o wide
    echo
    info "Mojaloop rollout history (rollback_bad_deploy undoes the latest revision)"
    for svc in ml-api-adapter central-ledger quoting-service account-lookup-service \
               transaction-request-service; do
      kc -n "$NS" rollout history "deployment/$svc"
    done
    ;;
  healing)
    value=true
    [[ "${1:-}" == "--off" ]] && value=false
    kc -n "$NS" patch configmap aiops-env --type merge -p "{\"data\":{\"HEALING_ENABLED\":\"$value\"}}"
    kc -n "$NS" rollout restart deployment/healing-engine deployment/api-gateway
    kc -n "$NS" rollout status deployment/healing-engine --timeout="${TIMEOUT}s"
    ok "HEALING_ENABLED=$value on the cluster"
    ;;
  seed)
    # Runs on the VM against TimescaleDB's NodePort (127.0.0.1:5432); the defaults in
    # aiops_common.config already match the cluster's credentials.
    [[ -x .venv/bin/python ]] || { echo "No .venv - run deploy/vm/setup.sh first." >&2; exit 1; }
    info "Loading the archived 28-day training set (docs/DATA.md) into TimescaleDB"
    .venv/bin/python scripts/replay_history.py --from-csv ml/datasets/mojaloop_replay_28d.csv
    info "Retraining the anomaly detector on the loaded history"
    curl -fsS -X POST http://127.0.0.1:8021/train >/dev/null && echo "    anomaly-detector: trained"
    ok "History loaded. Forecasts refresh on the traffic predictor's next cycle (<= 10 min)."
    ;;
  credentials)
    print_credentials
    ;;
  down)
    kind delete cluster --name "$CLUSTER"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 1
    ;;
esac
