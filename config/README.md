# Platform configuration

## `topology.*.json` — the monitored DPI system

The AIOps services are not tied to one DPI platform. Anomaly detection learns each service
from its own metrics; root cause analysis and healing need only the **service graph**:
which services exist and which ones each of them calls.

```json
{
  "name": "my-dpi-system",
  "services": {
    "api-gateway": { "calls": ["registry", "ledger"] },
    "registry":    { "calls": [] },
    "ledger":      { "calls": [] }
  }
}
```

- `calls` lists the services a service *calls*. Failure flows the other way: when `ledger`
  degrades, `api-gateway` waits and fails too. RCA blames the deepest failing dependency, and
  the cascade playbook opens circuit breakers on all of its callers.
- Every name in `calls` must be a service in the file. Cycles are rejected, because a cycle
  has no "deepest" service to blame.
- Service names must match the `service` label on the DPI metrics (`dpi_transaction_total`
  etc.) that Prometheus scrapes.

`topology.mojaloop.json` describes the bundled Mojaloop simulator and is the default.

### Using another topology

- **Docker Compose:** put the file in this folder and set `AIOPS_TOPOLOGY=<file name>` in `.env`.
- **Kubernetes:** `scripts/k8s.ps1` / `scripts/k8s.sh` build the `aiops-topology` ConfigMap from
  `config/topology.mojaloop.json`; change that line to your file.
- **Host-side runs:** `AIOPS_TOPOLOGY_FILE=<path>`.

The dashboard reads the graph from the API gateway (`GET /api/topology`), so it follows
automatically.
