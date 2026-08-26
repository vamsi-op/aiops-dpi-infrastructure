# Contributing

This is a team project split into independent modules. The goal is that two people can
work on different modules at the same time without blocking or breaking each other.

Read this once before your first pull request. It is short.

---

## 1. Get set up

Full instructions, including Docker and WSL 2 on Windows: **[docs/SETUP.md](docs/SETUP.md)**

```powershell
git clone <repo-url>
cd Project
.\scripts\bootstrap.ps1        # venv + dependencies + .env
.\scripts\stack.ps1 up         # TimescaleDB + Redis
.\scripts\healthcheck.ps1      # must print "All checks passed"
```

If `healthcheck.ps1` is not green, stop and fix that before writing code. Everything
else assumes it passes.

---

## 2. Pick up work

1. Find an open issue labelled `module-N`. Each module is one issue.
2. Comment on it to claim it, so nobody duplicates your work.
3. Read that module's section in **[docs/MODULES.md](docs/MODULES.md)** — it states what
   the module owns, what it explicitly does *not* own, and the command that proves it works.
4. Branch from `main`:

   ```powershell
   git switch -c module-3-metrics-agent
   ```

Branch naming: `module-N-short-description`, or `fix/short-description` for bugs.

---

## 3. How the modules stay independent

This is the most important section. The architecture is designed so you rarely need to
touch code someone else owns.

**Everything crosses module boundaries through three places, and only these three:**

| Boundary | Where it lives | Rule |
|----------|----------------|------|
| Data contracts | `libs/aiops_common/schemas.py` | Pydantic models on the event bus and in API responses |
| Shared names | `libs/aiops_common/constants.py` | Service names, metric names, stream names, enums |
| Storage | `infra/timescaledb/init/` | Tables every module reads and writes |

So: **your module reads events and rows produced by others, and writes its own.** It does
not import another service's Python package, and it does not query another service's
internals.

**If you need to change a shared contract**, that is fine, but it affects everyone:

- Open a short discussion on the issue first.
- Prefer **additive** changes: new optional field, new enum value, new table column with
  a default. Those break nobody.
- `ANOMALY_FEATURES` in `constants.py` is **append-only**. Reordering or removing an entry
  silently invalidates every trained model.
- Schema changes: the SQL in `infra/timescaledb/init/` only runs on an empty volume. Add a
  new numbered file, and tell the team they need `.\scripts\stack.ps1 reset`.

---

## 4. House rules

These are enforced by tooling, so you will find out quickly either way.

**Configuration.** Use `get_settings()` from `aiops_common.config`. No `os.getenv`
scattered through services — a bad value should fail loudly at start-up, not at 3 a.m.

**Time.** Always timezone-aware UTC via `utc_now()`. Naive `datetime.now()` is a lint
error (ruff rule `DTZ`).

**Logging.** `configure_logging(service_name)` once at start-up, then `get_logger(__name__)`.
Log structured key/values, not f-strings:

```python
log.info("anomaly.detected", service=name, score=score)   # yes
log.info(f"anomaly in {name} score {score}")              # no
```

**New services.** Build the app with `build_app()` from `aiops_common.service`. You get
`/health`, `/ready`, `/metrics`, request IDs and uniform error handling for free. An AIOps
platform that cannot be monitored itself is not finished.

**Comments explain why, not what.** The code already says what it does. Write down the
reasoning that is not obvious from reading it — especially anything you had to debug.

**Safety rails are features.** Automated remediation is off by default
(`HEALING_ENABLED=false`). A blocked action is a *correct* outcome and should be recorded
with a `skipped_*` status, not swallowed. Test the rails like you test the happy path.

**PowerShell scripts must stay ASCII.** Windows PowerShell 5.1 decodes BOM-less UTF-8 as
Windows-1252, which turns an em dash into a character it treats as a string delimiter, and
the script stops parsing. This has already bitten us once.

---

## 5. Before you open a pull request

Run the same checks CI runs:

```powershell
.\.venv\Scripts\Activate.ps1

ruff format .                 # format
ruff check . --fix            # lint
mypy libs services            # types
pytest tests -q               # unit tests

$env:AIOPS_RUN_INTEGRATION = '1'
pytest tests -q               # + integration tests (stack must be up)
```

Dashboard work as well:

```powershell
cd dashboard
npm run typecheck
npm run build
```

**Tests are required** for new behaviour. The bar is not coverage percentage, it is: would
this test have caught the bug? Bugs found during development deserve a regression test —
see the ones in `tests/test_integration_stack.py` for the tone.

If you touched the API and the dashboard, also run:

```powershell
python scripts\check_api_contract.py    # UI types vs real API responses
python scripts\ws_smoke.py             # live event relay end to end
```

---

## 6. Pull requests

- Keep them scoped to one module where you can. A reviewable PR gets reviewed.
- Fill in the template: what changed, how you verified it, anything you deliberately left out.
- Link the issue (`Closes #12`).
- Update `docs/MODULES.md` to tick your module off and record anything surprising you found.
- Green CI is required before merge. One approving review.
- Do not commit `.env`, credentials, `node_modules`, `dist/`, or model artifacts. `.gitignore`
  covers these, but check `git status` before committing.

---

## 7. Getting unstuck

- The stack misbehaving: `docs/SETUP.md` section 10 covers the common failures.
- Wondering *why* something is built a certain way: `docs/ARCHITECTURE.md` section 6.
- Not sure what your module should own: `docs/MODULES.md` has an explicit
  "owns / does not own" table.
- Still stuck: comment on your issue. A blocked teammate is worse than a question.
