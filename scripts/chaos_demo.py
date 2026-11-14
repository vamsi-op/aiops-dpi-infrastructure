"""Chaos demo: inject faults through the API gateway and print a scorecard (module 9).

Runs one or more scenarios unattended. For each one it waits until the platform is quiet
(no actionable anomalies, no open incidents), starts the experiment with POST /api/chaos,
follows it until the fault is remediated or times out, and reports:

    time to detect      first anomaly on the target, from fault injection
    time to RCA         incident opened, with the root cause RCA named and its confidence
    time to remediate   first healing action on the root that succeeded
    side effects        healing actions on any *other* service during the scenario

Healing must be enabled (HEALING_ENABLED=true) for remediation to happen; otherwise the
scorecard shows detection and RCA only.

Usage:
    python scripts/chaos_demo.py                      # all three scenarios
    python scripts/chaos_demo.py --scenario cascade   # one of: oom, cascade, bad_deploy
    python scripts/chaos_demo.py --gateway http://<vm>:8080 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# name -> (experiment, target, what the platform is expected to do)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "oom": ("memory_hog", "account-lookup-service", "restart the leaking service"),
    "cascade": (
        "network_latency",
        "central-ledger",
        "blame the slow dependency, restart it, open its callers' circuit breakers",
    ),
    "bad_deploy": ("bad_deploy", "ml-api-adapter", "blame the deployment and roll it back"),
}

FAULT_SECONDS = 300
QUIET_TIMEOUT_SECONDS = 600
# After the fault ends, how long to keep watching for side effects.
FOLLOW_SECONDS = FAULT_SECONDS + 120
POLL_SECONDS = 10


@dataclass
class Score:
    scenario: str
    experiment: str
    target: str
    expected: str
    started_at: str = ""
    detect_s: float | None = None
    rca_s: float | None = None
    root_cause: str = ""
    confidence: float | None = None
    remediate_s: float | None = None
    playbook: str = ""
    action_status: str = ""
    side_effects: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Gateway:
    def __init__(self, base: str) -> None:
        if urllib.parse.urlparse(base).scheme not in ("http", "https"):
            raise ValueError(f"gateway URL must be http(s): {base}")
        self.base = base.rstrip("/") + "/api"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)

    def get(self, path: str, **query: Any) -> Any:
        if query:
            path += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        return self._call("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, body)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _since(start: datetime, value: str | None) -> float | None:
    return round((_ts(value) - start).total_seconds(), 1) if value else None


def wait_until_quiet(gw: Gateway) -> bool:
    deadline = time.monotonic() + QUIET_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        recent = [
            a
            for a in gw.get("/anomalies", hours=1, limit=200)
            if a["score"] >= a["threshold"]
            and (datetime.now(UTC) - _ts(a["detected_at"])).total_seconds() < 120
        ]
        open_incidents = [
            i
            for i in gw.get("/incidents", limit=50)
            if i["status"] not in ("resolved", "closed")
        ]
        if not recent and not open_incidents:
            return True
        print(
            f"    waiting for quiet: {len(recent)} recent anomalies, "
            f"{len(open_incidents)} open incidents",
            flush=True,
        )
        time.sleep(15)
    return False


def run_scenario(gw: Gateway, name: str) -> Score:
    experiment, target, expected = SCENARIOS[name]
    score = Score(name, experiment, target, expected)
    print(f"\n==> {name}: {experiment} on {target} (expect: {expected})", flush=True)

    if not wait_until_quiet(gw):
        score.notes.append("platform never went quiet; ran anyway")

    run = gw.post(
        "/chaos",
        {"experiment": experiment, "target_service": target, "duration_seconds": FAULT_SECONDS},
    )
    start = _ts(run["started_at"])
    score.started_at = run["started_at"]
    print(f"    injected at {start:%H:%M:%S} (run {run['run_id']})", flush=True)

    deadline = time.monotonic() + FOLLOW_SECONDS
    incident_id: str | None = None
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        current = next(
            (r for r in gw.get("/chaos", limit=20) if r["run_id"] == run["run_id"]), None
        )
        if current is None:
            continue
        if score.detect_s is None and current.get("detected_at"):
            score.detect_s = _since(start, current["detected_at"])
            print(f"    detected after {score.detect_s:.0f}s", flush=True)
        incident_id = incident_id or current.get("incident_id")
        if incident_id and score.rca_s is None:
            incident = gw.get(f"/incidents/{incident_id}")["incident"]
            score.rca_s = _since(start, incident["opened_at"])
            print(f"    incident {incident_id} after {score.rca_s:.0f}s", flush=True)
        if current.get("remediated_at") and score.remediate_s is None:
            score.remediate_s = _since(start, current["remediated_at"])
            print(f"    remediated after {score.remediate_s:.0f}s", flush=True)
        # Detected, explained and fixed: stop early, but keep watching for side effects for a
        # minute so a restart storm or a victim restart would still show up.
        if score.remediate_s is not None:
            deadline = min(deadline, time.monotonic() + 60)

    if incident_id:
        detail = gw.get(f"/incidents/{incident_id}")
        hypotheses = detail["incident"].get("hypotheses") or []
        if hypotheses:
            top = hypotheses[0]
            score.root_cause = f"{top['category']}: {top['target']}"
            score.confidence = round(float(top["confidence"]), 2)
    actions = [
        a
        for a in gw.get("/actions", hours=2, limit=500)
        if _ts(a["created_at"]) >= start and not a["status"].startswith("skipped")
    ]
    on_target = sorted(
        (a for a in actions if a["target_service"] == target), key=lambda a: a["created_at"]
    )
    if on_target:
        score.playbook = on_target[0]["playbook"]
        score.action_status = on_target[0]["status"]
    score.side_effects = [
        f"{a['playbook']} on {a['target_service']} ({a['status']})"
        for a in actions
        if a["target_service"] != target
        and a["playbook"] != "circuit_break_cascade"  # breakers on callers are the fix itself
    ]
    return score


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}s"


def print_scorecard(scores: list[Score]) -> None:
    print("\n" + "=" * 100)
    print(
        f"{'scenario':<11}{'detect':>8}{'RCA':>7}{'remediate':>11}  "
        f"{'root cause (confidence)':<38}{'playbook':<24}result"
    )
    print("-" * 100)
    for s in scores:
        cause = f"{s.root_cause} ({s.confidence})" if s.root_cause else "-"
        print(
            f"{s.scenario:<11}{_fmt(s.detect_s):>8}{_fmt(s.rca_s):>7}{_fmt(s.remediate_s):>11}  "
            f"{cause[:37]:<38}{(s.playbook or '-'):<24}{s.action_status or '-'}"
        )
    print("-" * 100)
    for s in scores:
        effects = "; ".join(s.side_effects) if s.side_effects else "none"
        print(f"{s.scenario}: side effects on other services: {effects}")
        for note in s.notes:
            print(f"{s.scenario}: note: {note}")
    print("=" * 100)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    parser.add_argument("--json", action="store_true", help="also print the scores as JSON")
    args = parser.parse_args()

    gw = Gateway(args.gateway)
    try:
        gw.get("/config")
    except (urllib.error.URLError, OSError) as exc:
        print(f"API gateway not reachable at {args.gateway}: {exc}", file=sys.stderr)
        return 2

    scores = [run_scenario(gw, name) for name in (args.scenario or list(SCENARIOS))]
    print_scorecard(scores)
    if args.json:
        print(json.dumps([asdict(s) for s in scores], indent=2))
    failed = [s for s in scores if s.remediate_s is None or s.side_effects]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
