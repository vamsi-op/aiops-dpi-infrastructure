# Running on Kubernetes (module 10)

The platform runs on a local **kind** (Kubernetes-in-Docker) cluster with the same images,
the same configuration files and the same host ports as the Compose stack. The AIOps logic
does not change. What changes is how the healing playbooks act on the cluster:

| Playbook | Under Compose (Docker SDK) | On Kubernetes (Kubernetes API) |
|---|---|---|
| `restart_on_oom` | `docker restart` the containers | Delete the named pod, or do a rolling restart of the Deployment (`kubectl rollout restart`) and wait for it to finish |
| `scale_out_preemptive` / `scale_in_idle` | Start or stop extra containers | Patch the Deployment's scale one replica at a time. If an HPA owns the Deployment, raise its `minReplicas` floor so the HPA doesn't undo the scale-out. If the new pods can't be scheduled, scale back and report the capacity problem |
| `rollback_bad_deploy` | Simulated: restarts the containers | **A real `rollout undo`**: the previous ReplicaSet revision's pod template is copied back onto the Deployment |
| `circuit_break_cascade` | Restarts the root dependency | Rolling restart of the root dependency |

The healing engine no longer mounts the Docker socket. It uses its own ServiceAccount,
bound to a namespaced Role that can only patch Deployments, read ReplicaSets, delete pods
and patch HPAs in the `aiops` namespace. Remediation can't reach anything outside it.

## Prerequisites

Everything in [SETUP.md](SETUP.md), plus:

```powershell
winget install Kubernetes.kind
kubectl version --client    # Docker Desktop already ships kubectl
```

Give Docker Desktop at least **6 GB of memory** (Settings → Resources). The whole platform
(about 20 pods) runs on one kind node.

## Run it

```powershell
.\scripts\stack.ps1 down          # free the host ports the Compose stack holds
.\scripts\k8s.ps1 up              # cluster + images + ConfigMaps + manifests (~5 min first time)
.\scripts\k8s.ps1 status          # pods, deployments, rollout history
```

The script:

1. creates the kind cluster from [deploy/k8s/kind-cluster.yaml](../deploy/k8s/kind-cluster.yaml) if it doesn't exist
2. builds the images with `docker compose build` and runs `kind load` on each
3. builds ConfigMaps straight from `infra/` (schema SQL, recording rules, Loki/Tempo/
   Alertmanager/Grafana config), so Compose and Kubernetes share one copy of each
4. runs `kubectl apply -k deploy/k8s` and waits for every rollout
5. copies any trained models in `ml/artifacts/` into the anomaly detector's volume

Everything is reachable on the same ports as Compose: gateway `:8080`, Grafana `:3000`,
Prometheus `:9090`, TimescaleDB `:5432`, simulators `:8011-8015`, AIOps services
`:8020-8024`. That means `cd dashboard; npm run dev` and every `scripts\check_*.py` work
against the cluster unchanged.

Healing starts in **dry-run**, as it does under Compose:

```powershell
.\scripts\k8s.ps1 healing         # HEALING_ENABLED=true, restarts healing-engine + gateway
.\scripts\k8s.ps1 healing -Off
```

## Demo: a real bad deploy and a real rollback

On Kubernetes the `bad_deploy` chaos experiment is a real release, not a flag inside a
running process. The gateway patches the target Deployment to a new version
(`v1.0.4 → v1.0.5-canary`) whose pod template sets `SIM_DEFECT=bad_deploy`. That creates a
new ReplicaSet revision, and the defect ships with the pods that roll out.

```powershell
.\scripts\k8s.ps1 healing
# Dashboard → Chaos → "Bad deploy" on central-ledger, or:
Invoke-RestMethod -Method Post http://localhost:8080/api/chaos -ContentType application/json `
  -Body '{"experiment":"bad_deploy","target_service":"central-ledger","duration_seconds":300}'

kubectl --context kind-aiops -n aiops rollout history deployment/central-ledger
```

The flow from there:

1. The new revision's pods start with the defect, and error rate and latency rise.
2. The anomaly detector flags them.
3. RCA correlates the anomalies with the deployment record and ranks a CHANGE hypothesis first.
4. The healing engine runs `rollback_bad_deploy`. This reads the ReplicaSets, finds the
   previous revision and restores its template.
5. `rollout history` now shows a newer revision with the change cause
   `aiops healing-engine: rollback to revision N`. The pods run `v1.0.4` without the
   defect, and the incident auto-resolves once the SLO window is clean.

## Layout

```
deploy/k8s/
  kind-cluster.yaml     one node, host ports → NodePorts
  kustomization.yaml    kubectl apply -k deploy/k8s
  namespace.yaml        namespace + shared env ConfigMaps (aiops-env, aiops-sim-env)
  data.yaml             TimescaleDB StatefulSet, Redis
  observability.yaml    Prometheus (Kubernetes service discovery), Grafana, Loki, Alloy, Tempo, Alertmanager
  mojaloop.yaml         the five simulated Mojaloop services
  aiops.yaml            metrics-agent, anomaly-detector, traffic-predictor, rca-engine,
                        healing-engine (+ RBAC), api-gateway (+ RBAC)
  config/prometheus.yml Kubernetes service discovery; same jobs and labels as Compose
  config/config.alloy   pod log discovery via the Kubernetes API
```

Code: [k8s_executor.py](../services/healing_engine/healing_engine/k8s_executor.py)
(selected by `HEALING_EXECUTOR=auto|docker|kubernetes`; `auto` picks Kubernetes inside a pod),
[k8s_chaos.py](../services/api_gateway/api_gateway/k8s_chaos.py), tests in
[test_k8s_executor.py](../tests/test_k8s_executor.py).

## Not done yet

- **Real Mojaloop Helm charts.** The simulator still stands in for Mojaloop. The
  Deployment names match the real Mojaloop charts, so the swap is a values file, not a code change.
- **kube-state-metrics and cAdvisor** as `metrics-agent` sources, for real restart and OOMKill counts.
- **HPAs.** kind ships without metrics-server, so no HPA manifests are included. The
  executor already handles an HPA when one owns a Deployment.
