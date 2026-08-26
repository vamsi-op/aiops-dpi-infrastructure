# aiops_common

Shared building blocks for every service in the DPI AIOps platform. Import it instead of
re-implementing plumbing, so all services behave identically in config, logging and
event handling.

| Module | Purpose |
|--------|---------|
| `config` | Typed, environment-driven settings (`get_settings()`) |
| `log` | Structured logging via structlog — JSON in Docker, pretty in a terminal |
| `constants` | Canonical names: services, metrics, event streams, severities |
| `schemas` | Pydantic contracts exchanged over the event bus and HTTP APIs |
| `bus` | Redis Streams event bus — publish / consume with consumer groups |
| `db` | Async SQLAlchemy engine + session factory for TimescaleDB |
| `promql` | Thin Prometheus HTTP API client (instant + range queries) |
| `service` | `build_app()` FastAPI factory with health, readiness and `/metrics` |
| `errors` | Shared exception hierarchy |

## Install (editable, from repo root)

```powershell
pip install -e .\libs\aiops_common[dev]
```

## Usage

```python
from aiops_common import get_settings, configure_logging, get_logger

configure_logging(service_name="metrics-agent")
log = get_logger(__name__)
settings = get_settings()

log.info("starting", dpi_platform=settings.dpi_platform, db=settings.postgres.host)
```
