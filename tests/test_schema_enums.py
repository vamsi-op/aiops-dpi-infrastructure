"""The database's CHECK constraints must accept every value the code writes.

Enum-like columns are TEXT + CHECK (see the header of 02_tables.sql). That keeps the schema
simple, but nothing ties a CHECK list to the StrEnum in constants.py - and when they drift,
the failure is an INSERT error at runtime, inside the code path that hits the new value.
For healing_actions.status that path is a safety rail blocking an action, so the audit
trail silently lost exactly the decisions it exists to prove. These tests make drift a CI
failure instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiops_common.constants import ActionStatus, Severity

ROOT = Path(__file__).resolve().parents[1]
TABLES_SQL = ROOT / "infra/timescaledb/init/02_tables.sql"
MIGRATION_001 = ROOT / "infra/timescaledb/migrations/001_healing_action_statuses.sql"


def check_values(sql: str, table: str, column: str) -> set[str]:
    """Values in `CHECK (<column> IN (...))` inside `CREATE TABLE <table>` (or anywhere)."""
    block = sql
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.DOTALL)
    if match:
        block = match.group(1)
    found = re.search(rf"CHECK \({column} IN \((.*?)\)\)", block, re.DOTALL)
    assert found, f"no CHECK on {table}.{column}"
    return set(re.findall(r"'([^']+)'", found.group(1)))


def test_healing_action_status_constraint_matches_enum():
    allowed = check_values(TABLES_SQL.read_text(encoding="utf-8"), "healing_actions", "status")
    assert allowed == {s.value for s in ActionStatus}


def test_migration_installs_the_same_status_list():
    schema = check_values(TABLES_SQL.read_text(encoding="utf-8"), "healing_actions", "status")
    migration = check_values(MIGRATION_001.read_text(encoding="utf-8"), "healing_actions", "status")
    assert migration == schema


@pytest.mark.parametrize("table", ["anomalies", "incidents"])
def test_severity_constraints_match_enum(table):
    allowed = check_values(TABLES_SQL.read_text(encoding="utf-8"), table, "severity")
    assert allowed == {s.value for s in Severity}
