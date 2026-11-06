"""Check the dashboard's TypeScript types against what the API actually returns.

`tsc` validates the frontend against `types.ts`, and pytest validates the backend
against its Pydantic models, but nothing checks those two agree. A field renamed on one
side and not the other passes both and renders an empty card in the browser. This
script closes that gap.

It parses the interfaces out of `dashboard/src/api/types.ts`, calls the matching
endpoint, and reports fields the UI expects but the API never sends.

Usage (gateway must be running):
    python scripts/check_api_contract.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:8080"
TYPES_FILE = Path(__file__).resolve().parents[1] / "dashboard/src/api/types.ts"

# TypeScript interface -> (endpoint, how to reach a representative object)
#   "list"   the endpoint returns an array; check the first element
#   "object" the endpoint returns the object itself
#   dotted   drill into the response, e.g. "services.0" or "incident"
CHECKS: list[tuple[str, str, str]] = [
    ("Overview", "/api/overview", "object"),
    ("PlatformTotals", "/api/overview", "totals"),
    ("OverviewCounts", "/api/overview", "counts"),
    ("PipelineStatus", "/api/overview", "pipeline"),
    ("ServiceHealth", "/api/services", "0"),
    ("ServiceDetail", "/api/services/central-ledger", "object"),
    ("Anomaly", "/api/anomalies?limit=1", "0"),
    ("Incident", "/api/incidents?limit=1", "0"),
    ("IncidentDetail", "/api/incidents", "resolve-incident"),
    ("Prediction", "/api/predictions", "0"),
    ("HealingAction", "/api/actions?limit=1", "0"),
    ("AutomationSummary", "/api/automation", "object"),
    ("ChaosRun", "/api/chaos", "0"),
    ("Deployment", "/api/deployments", "0"),
    ("AppConfig", "/api/config", "object"),
    ("Topology", "/api/topology", "object"),
    ("Hypothesis", "/api/incidents?limit=1", "0.hypotheses.0"),
    ("TimelineBucket", "/api/anomalies/timeline", "0"),
    ("ForecastPoint", "/api/predictions", "0.points.0"),
    ("MetricPoint", "/api/services/central-ledger", "metrics.0"),
]

INTERFACE_RE = re.compile(r"export\s+interface\s+(\w+)\s*\{(.*?)\n\}", re.DOTALL)
# Matches `  fieldName?: type;` and captures the name plus whether it is optional.
FIELD_RE = re.compile(r"^\s{2}(\w+)(\??):", re.MULTILINE)


def parse_interfaces(source: str) -> dict[str, dict[str, bool]]:
    """Return {interface: {field: is_optional}}."""
    interfaces: dict[str, dict[str, bool]] = {}
    for name, body in INTERFACE_RE.findall(source):
        interfaces[name] = {
            field: optional == "?" for field, optional in FIELD_RE.findall(body)
        }
    return interfaces


def fetch(path: str) -> Any:
    with urllib.request.urlopen(BASE_URL + path, timeout=20) as response:  # noqa: S310
        return json.load(response)


def resolve(payload: Any, selector: str) -> Any:
    if selector == "object":
        return payload
    if selector == "resolve-incident":
        incidents = fetch("/api/incidents?limit=1")
        if not incidents:
            return None
        return fetch(f"/api/incidents/{incidents[0]['incident_id']}")
    current = payload
    for part in selector.split("."):
        if part.isdigit():
            if not isinstance(current, list) or not current:
                return None
            current = current[int(part)]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
    return current


def main() -> int:
    if not TYPES_FILE.is_file():
        print(f"types file not found: {TYPES_FILE}")
        return 1

    interfaces = parse_interfaces(TYPES_FILE.read_text(encoding="utf-8"))
    print(f"parsed {len(interfaces)} interfaces from {TYPES_FILE.name}\n")

    problems: list[str] = []
    skipped: list[str] = []

    for interface, endpoint, selector in CHECKS:
        expected = interfaces.get(interface)
        if expected is None:
            problems.append(f"{interface}: no such interface in types.ts")
            continue

        try:
            sample = resolve(fetch(endpoint), selector)
        except urllib.error.HTTPError as exc:
            problems.append(f"{interface}: {endpoint} returned HTTP {exc.code}")
            continue
        except OSError as exc:
            problems.append(f"{interface}: {endpoint} unreachable ({exc})")
            continue

        if not isinstance(sample, dict):
            skipped.append(f"{interface} ({endpoint} had no sample data)")
            continue

        actual = set(sample)
        # Only required fields are errors. An absent optional field is legitimate.
        missing = sorted(f for f, opt in expected.items() if not opt and f not in actual)
        extra = sorted(actual - set(expected))

        status = "OK  " if not missing else "FAIL"
        print(f"{status} {interface:20} {endpoint:34} fields={len(expected):2}")
        if missing:
            problems.append(f"{interface}: UI requires fields the API never sent: {missing}")
        if extra:
            # Informational: the API is ahead of the UI. Not a failure.
            print(f"     {'':20} api also sends: {extra}")

    print()
    for note in skipped:
        print(f"SKIP {note}")

    if problems:
        print(f"\n{len(problems)} contract problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nPASS - every required UI field is present in the API responses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
