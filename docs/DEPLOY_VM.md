# Deploying to a cloud VM

The platform runs on **one Linux VM** with a kind Kubernetes cluster inside it: the same
manifests, healing executor and chaos experiments as on your laptop
([KUBERNETES.md](KUBERNETES.md)). Reviewers get a public dashboard URL. Everything else
stays private to the VM.

```
Internet ──► :80   dashboard (nginx, password-protected) ──► /api, /ws ──► api-gateway
         ──► :3000 Grafana (login required)
              VM: Docker ─► kind node ─► ~20 pods in namespace `aiops`
              Every other port (gateway, Prometheus, DB, simulators...) binds to 127.0.0.1
```

This guide uses AWS EC2. Any provider with an Ubuntu 24.04 x86_64 VM works the same way
(Azure, GCP, DigitalOcean); only step 1 differs.

## 1. Create the VM (AWS console)

**EC2 → Launch instance**

| Setting | Value |
|---|---|
| AMI | Ubuntu Server 24.04 LTS, **64-bit (x86)** |
| Instance type | `t3.xlarge` (4 vCPU, 16 GB) on a paid account. On the **AWS Free plan** (accounts created after 15 July 2025) choose **`m7i-flex.large`** (2 vCPU, 8 GB), the largest free-plan type: the running platform uses about 2.8 GB and a third of a core; only the first image build is slower. The setup adds 4 GB swap as a buffer |
| Key pair | Create one and keep the `.pem` file safe |
| Storage | 40 GB gp3 (images and build cache take ~10 GB) |
| Security group | SSH `22` from **My IP** only; HTTP `80` and TCP `3000` from anywhere (or only your reviewers' IPs) |

Then **Elastic IPs → Allocate → Associate** it with the instance, so the address survives
stop/start. AWS bills public IPv4 addresses (about $0.005/hour), including Elastic IPs.

**Before anything else, set a budget:** Billing → Budgets → a monthly cost budget with
email alerts (for example at $20 and $50).

## 2. Install and start

```bash
ssh -i aiops-key.pem ubuntu@<elastic-ip>
git clone https://github.com/vamsi-op/aiops-dpi-infrastructure.git aiops
cd aiops
bash deploy/vm/setup.sh          # Docker, kind, kubectl, Python venv, swap (~5 min)
exit                             # log out so the docker group applies
```

```bash
ssh -i aiops-key.pem ubuntu@<elastic-ip>
cd aiops
scripts/k8s.sh up --public       # builds images, creates the cluster, deploys (~10-15 min)
scripts/k8s.sh seed              # 28 days of history for the forecaster, retrains the detector
```

`up --public` prints the URLs and the generated logins. Print them again with
`scripts/k8s.sh credentials`. They live in `~/.aiops/` on the VM, nowhere else.

- **Dashboard:** `http://<elastic-ip>/`, user `admin`
- **Grafana:** `http://<elastic-ip>:3000/`, user `admin`

Healing starts in dry-run. For the live demo:

```bash
scripts/k8s.sh healing           # real remediation on
scripts/k8s.sh healing --off
```

## 3. Day-to-day

**Stop the instance when you're not demoing** (EC2 → Instance state → Stop). A stopped
instance costs only its disk (about $3/month for 40 GB), so a few hours a day keeps a
$100-200 credit going for months.

After starting it again:

```bash
cd aiops && scripts/k8s.sh status
```

Docker brings the kind node back with the VM, and the pods restart on their own within a
few minutes. If the cluster doesn't recover, rebuild it. TimescaleDB history is lost, so
re-seed afterwards:

```bash
scripts/k8s.sh down && scripts/k8s.sh up --public && scripts/k8s.sh seed
```

**Deploying new code:**

```bash
git pull
scripts/k8s.sh images            # rebuild, load, rolling restart
kubectl --context kind-aiops apply -k deploy/k8s    # only if manifests changed
```

**Private tools** (Prometheus, the gateway's `/docs`, the AIOps services) go through an
SSH tunnel instead of being exposed:

```bash
ssh -i aiops-key.pem -L 9090:127.0.0.1:9090 -L 8080:127.0.0.1:8080 ubuntu@<elastic-ip>
# then open http://localhost:9090 and http://localhost:8080/docs on your laptop
```

## Security notes

- Only ports 80 and 3000 listen publicly ([deploy/vm/kind-cluster.yaml](../deploy/vm/kind-cluster.yaml)).
  The database, Redis and the AIOps APIs bind to `127.0.0.1`, so they stay private even if
  the security group is misconfigured.
- The dashboard is behind HTTP basic auth because it can trigger chaos experiments and bad
  deploys. Share the password only with reviewers.
- Grafana's anonymous access is turned off and its admin password is generated. The
  local-dev defaults (`admin`/`admin`, anonymous viewers) never apply on the VM, because
  `--public` creates the overriding Secrets before any pod starts.
- Traffic is plain HTTP. For HTTPS, point a domain at the Elastic IP and put Caddy or
  nginx with Let's Encrypt in front of port 80. Basic-auth passwords are sent in clear
  over HTTP, so don't reuse them anywhere else.
- The database password in [deploy/k8s/data.yaml](../deploy/k8s/data.yaml) is the
  local-dev default. The database is unreachable from outside the VM, but change it if
  the VM is shared.
