#!/usr/bin/env bash
# One-time bootstrap for a fresh Ubuntu 24.04 x86_64 VM; then run scripts/k8s.sh up --public.
#
#   git clone <repo> aiops && cd aiops
#   bash deploy/vm/setup.sh
#   # log out and back in (docker group), then:
#   scripts/k8s.sh up --public
#
# Installs Docker Engine (official apt repo), kind and kubectl (pinned, checksum-verified),
# a Python 3.12 venv for the seed / check scripts, raises the inotify limits kind needs
# for ~20 pods, and adds a swap file as a safety net against the OOM killer. Safe to re-run.
set -euo pipefail

KIND_VERSION=v0.26.0      # ships kindest/node:v1.31.4, the image deploy/*/kind-cluster.yaml pins
KUBECTL_VERSION=v1.31.4   # match the node's Kubernetes minor version
SWAP_SIZE_GB=4

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }

[[ "$(uname -m)" == "x86_64" ]] || {
  echo "This script and the platform images target x86_64. On ARM, rebuild the images for arm64 first." >&2
  exit 1
}
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || { echo "Written for Ubuntu; found $ID." >&2; exit 1; }
# 24.04 ships Python 3.12; the shared library needs >= 3.11, which 22.04 does not have.
[[ "${VERSION_ID%%.*}" -ge 24 ]] || { echo "Needs Ubuntu 24.04 or newer (Python 3.11+)." >&2; exit 1; }

info "Base packages"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg git openssl python3 python3-venv python3-pip

if ! command -v docker >/dev/null 2>&1; then
  info "Docker Engine"
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

install_binary() {  # name, url, sha256-url
  local name=$1 url=$2 sum_url=$3 tmp
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/$name" "$url"
  echo "$(curl -fsSL "$sum_url" | awk '{print $1}')  $tmp/$name" | sha256sum -c -
  sudo install -m 0755 "$tmp/$name" "/usr/local/bin/$name"
  rm -rf "$tmp"
}

if ! kind version 2>/dev/null | grep -q "$KIND_VERSION"; then
  info "kind $KIND_VERSION"
  install_binary kind \
    "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64" \
    "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64.sha256sum"
fi

if ! kubectl version --client 2>/dev/null | grep -q "$KUBECTL_VERSION"; then
  info "kubectl $KUBECTL_VERSION"
  install_binary kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
fi

info "inotify limits (kind: 'too many open files' with many pods otherwise)"
sudo tee /etc/sysctl.d/99-kind.conf >/dev/null <<'EOF'
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
EOF
sudo sysctl --system >/dev/null

if ! swapon --show | grep -q /swapfile; then
  info "${SWAP_SIZE_GB} GB swap file"
  sudo fallocate -l "${SWAP_SIZE_GB}G" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

info "Python venv for scripts/ (replay_history, check_*)"
cd "$(dirname "$0")/../.."
# Optional: only the helper scripts use it, and `k8s.sh seed` falls back to running inside a
# platform image. A distribution Python newer than the pinned wheels support (Ubuntu 26.04
# ships 3.14; psycopg-binary 3.2.3 has no 3.14 wheel) must not abort the whole setup.
if { [[ -d .venv ]] || python3 -m venv .venv; }    && .venv/bin/pip install -q --upgrade pip    && .venv/bin/pip install -q -r requirements-dev.txt; then
  :
else
  printf '[33m    venv install failed (Python %s) - fine: scripts/k8s.sh seed runs in a container[0m
'     "$(python3 -c 'import sys; print(sys.version.split()[0])')"
fi

echo
info "Done. Log out and back in (so the docker group applies), then run:"
echo "    cd $(pwd) && scripts/k8s.sh up --public"
