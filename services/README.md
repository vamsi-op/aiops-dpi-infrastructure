# services/

One directory per AIOps microservice. Each is self-contained: its own `requirements.txt`,
`Dockerfile`, and `app.py` entry point, all importing the shared `aiops_common` library.

| Directory | Module | Role |
|-----------|--------|------|
| `mojaloop_sim/` | 2 | Simulated Mojaloop microservices producing realistic telemetry |
| `metrics_agent/` | 3 | Collects and normalises metrics into `MetricSample` records |
| `anomaly_detector/` | 4 | Isolation Forest anomaly detection with feature attribution |
| `traffic_predictor/` | 5 | Load forecasting and replica recommendations |
| `healing_engine/` | 6 | Playbook execution behind safety rails |
| `rca_engine/` | 7 | Anomaly correlation and root cause hypotheses |
| `api_gateway/` | 8 | Read APIs plus WebSocket fan-out for the dashboard |
| `chaos_agent/` | 9 | Fault injection and experiment scorecards |

## Shape of a service

```
services/<name>/
├── Dockerfile
├── requirements.txt
├── app.py              # build_app(...) from aiops_common.service
├── <name>/             # package with the actual logic
│   ├── __init__.py
│   └── ...
└── tests/
```

Directories appear as their module is built — see `docs/MODULES.md`.
