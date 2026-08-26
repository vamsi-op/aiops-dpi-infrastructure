<!--
Keep this short but real. The verification section is the part reviewers rely on most.
-->

## What this changes

<!-- One or two sentences. What now works that did not before? -->

Closes #

## How I verified it

<!--
Paste the actual command and its result. "Tests pass" on its own is not verification.
-->

```
```

- [ ] `ruff format .` and `ruff check .` clean
- [ ] `mypy libs services` clean
- [ ] `pytest tests -q` passes
- [ ] `AIOPS_RUN_INTEGRATION=1 pytest tests -q` passes (stack running)
- [ ] Dashboard touched? `npm run typecheck` and `npm run build` pass
- [ ] API or UI types touched? `python scripts/check_api_contract.py` passes

## Shared contracts

<!--
Did you change libs/aiops_common/schemas.py, constants.py, or infra/timescaledb/init/?
Those affect every module. Say what changed and whether teammates need to act
(for example: "schema change, everyone needs .\scripts\stack.ps1 reset").
ANOMALY_FEATURES is append-only.
-->

- [ ] No shared contract changes
- [ ] Shared contracts changed, described above, and the team has been told

## Anything left out or worth knowing

<!--
Known gaps, deliberate shortcuts, surprises you hit. Bugs you found and fixed along
the way are worth a line each - and a regression test.
-->

- [ ] `docs/MODULES.md` updated if this completes a module
