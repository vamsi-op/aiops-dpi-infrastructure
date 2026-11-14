# Contributing

## 1. Set up

Follow [docs/SETUP.md](docs/SETUP.md). For code changes alone you need only the Python
environment (`.\scripts\bootstrap.ps1`); unit tests run without Docker.

Branches: `feat/<short-description>`, `fix/<short-description>`, `docs/<short-description>`.
Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `ci:`, `chore:`).

---

## 2. How services stay independent

Services interact only through three shared contracts:

| Contract | Location | Contents |
|---|---|---|
| Data | `libs/aiops_common/src/aiops_common/schemas.py` | Pydantic models on the event bus and in API responses |
| Names | `libs/aiops_common/src/aiops_common/constants.py` | Stream and table names, metric names, enums, `ANOMALY_FEATURES` |
| Storage | `infra/timescaledb/init/` | Tables and continuous aggregates |

A service reads events and rows produced by others and writes its own. It never imports another
service's package or queries another service's internals.

Changing a shared contract affects every service:

- Prefer **additive** changes: a new optional field, enum value, or column with a default.
- `ANOMALY_FEATURES` is **append-only**. Reordering or removing an entry invalidates every
  trained model.
- Init SQL runs only on an empty database. Add a migration in `infra/timescaledb/migrations/`
  and mirror the change in `infra/timescaledb/init/`.
- The service graph belongs in the topology file (`config/`), never in code.

---

## 3. Conventions

Most are enforced by `ruff` and `mypy`.

- **Configuration** through `get_settings()`, never scattered `os.getenv`: a bad value fails
  at start-up.
- **Time** is timezone-aware UTC via `utc_now()`. Naive datetimes fail lint (`DTZ`).
- **Logging** is structured key/value through `get_logger(__name__)`:

  ```python
  log.info("anomaly.detected", service=name, score=score)   # yes
  log.info(f"anomaly in {name} score {score}")              # no
  ```

- **New services** use `build_app()` from `aiops_common.service`, which provides `/health`,
  `/ready`, `/metrics`, request IDs and uniform error handling.
- **Comments explain why**, not what — especially anything that had to be debugged.
- **Safety rails are features.** A blocked remediation is recorded with a `skipped_*` status,
  never swallowed. Test the rails as you test the happy path.
- **PowerShell scripts stay ASCII.** Windows PowerShell 5.1 reads BOM-less UTF-8 as
  Windows-1252, and a non-ASCII character can break parsing. Judge native commands by their
  exit code: under `ErrorActionPreference = 'Stop'`, PowerShell 5.1 treats a native program's
  stderr output as fatal.

---

## 4. Before opening a pull request

Run what CI runs:

```powershell
ruff format --check .
ruff check .
mypy libs services
pytest tests -q
```

With the stack running, also:

```powershell
$env:AIOPS_RUN_INTEGRATION = '1'; pytest tests -q
python scripts\check_api_contract.py     # dashboard types vs. real API responses
python scripts\ws_smoke.py               # live event relay end to end
```

For dashboard changes: `npm run typecheck` and `npm run build` in `dashboard/`.

Behaviour changes need a test, and a bug fix needs a regression test — one that would have
caught the bug. For changes to detection, RCA or healing, run the affected scenario of
`scripts/chaos_demo.py` and include the scorecard in the pull request.

---

## 5. Pull requests

- Keep each pull request to one concern, and fill in the template: what changed, how it was
  verified, what was deliberately left out.
- CI must pass, and one approving review is required.
- Never commit `.env`, credentials, `node_modules/`, `dist/` or model artifacts.
