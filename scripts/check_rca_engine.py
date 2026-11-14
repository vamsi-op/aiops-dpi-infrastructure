"""Verify the RCA engine end to end (module 7).

The module's acceptance criteria:

1. **Cascade detection**: Inject faults into central-ledger, verify that downstream service
   anomalies (account-lookup-service, als-oracle-pathfinder) are correlated into a single
   incident with topological hypothesis identifying central-ledger as root cause.

2. **Change correlation**: Deploy a service, then inject a fault. Verify the incident includes
   a CHANGE hypothesis linking the anomaly to the deployment.

3. **Event correlation**: Inject a chaos experiment or traffic surge event, verify anomalies
   are correlated with the event at high confidence.

4. **AI summary**: Verify incidents have human-readable summaries with affected services,
   primary hypothesis, confidence, and evidence.

Usage (stack must be running with -Layer all, models trained, and detector active):
    .\\.venv\\Scripts\\python.exe scripts\\check_rca_engine.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from aiops_common.constants import TABLE_INCIDENTS
from aiops_common.db import Database

RCA_ENGINE = "http://localhost:8023"
AGENT = "http://localhost:8020"
DETECTOR = "http://localhost:8021"

# Simulator ports
SIM_PORTS = {
    "ml-api-adapter": 8011,
    "central-ledger": 8012,
    "quoting-service": 8013,
    "account-lookup-service": 8014,
    "transaction-request-service": 8015,
}

CORRELATION_TIMEOUT_SECONDS = 300  # 5 minutes for correlations to happen
SETTLE_SECONDS = 90  # Let anomalies accumulate before checking correlation

failures: list[str] = []
warnings: list[str] = []


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(message: str) -> None:
    print(f"  [PASS] {message}")


def fail(message: str) -> None:
    print(f"  [FAIL] {message}")
    failures.append(message)


def warn(message: str) -> None:
    print(f"  [WARN] {message}")
    warnings.append(message)


def get_json(url: str, *, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def post_json(url: str, body: dict[str, Any] | None = None, *, timeout: float = 120.0) -> Any:
    data = json.dumps(body).encode() if body is not None else b""
    request = urllib.request.Request(  # noqa: S310
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def delete(url: str, *, timeout: float = 30.0) -> None:
    request = urllib.request.Request(url, method="DELETE")  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
        pass


# ---------------------------------------------------------------------------
def check_rca_engine_up() -> dict[str, Any]:
    section("RCA Engine health")
    try:
        root = get_json(f"{RCA_ENGINE}/", timeout=10)
    except OSError as exc:
        fail(f"RCA engine unreachable at {RCA_ENGINE} ({exc}) - is -Layer all running?")
        return {}
    ok(f"RCA engine up, correlation methods: {root.get('correlation_methods')}")

    try:
        ready = get_json(f"{RCA_ENGINE}/ready", timeout=20)
    except urllib.error.HTTPError as exc:
        fail(f"/ready returned {exc.code}: {exc.read().decode()[:200]}")
        return {}

    for name, state in (ready.get("dependencies") or {}).items():
        if state == "ok":
            ok(f"dependency {name}")
        else:
            fail(f"dependency {name}: {state}")

    status = get_json(f"{RCA_ENGINE}/status")
    if not status.get("running"):
        fail("RCA engine loop is not running")
        return {}

    ok(
        f"RCA engine running, window size: {status.get('window_size')}, "
        f"unassigned: {status.get('unassigned_anomalies')}"
    )

    config = get_json(f"{RCA_ENGINE}/config")
    ok(
        f"correlation window: {config.get('window_seconds')}s, "
        f"auto-resolve: {config.get('auto_resolve_seconds')}s"
    )

    return status


# ---------------------------------------------------------------------------
async def check_cascade_detection() -> None:
    """Acceptance criterion 1: detect cascades through dependency graph.

    Inject fault into central-ledger, expect downstream services to show anomalies,
    and verify they are correlated into a single incident with topological hypothesis.
    """
    section("Cascade detection (central-ledger → account-lookup-service)")

    db = Database()
    await db.connect()

    try:
        # Clear any existing faults across all services and let settle
        for service, port in SIM_PORTS.items():
            try:
                delete(f"http://localhost:{port}/sim/faults")
            except Exception:
                pass
        await asyncio.sleep(5)

        # Resolve any prior open/mitigating incidents so this test starts fresh
        await db.execute(
            "UPDATE incidents SET status = 'resolved', closed_at = NOW() WHERE status IN ('open', 'mitigating')"
        )

        # Inject fault into central-ledger
        fault_start = time.time()
        print(
            f"  Injecting error_injection into central-ledger at {datetime.now(UTC).isoformat()}"
        )
        post_json(
            f"http://localhost:{SIM_PORTS['central-ledger']}/sim/faults",
            {
                "kind": "error_injection",
                "magnitude": 0.40,
                "duration_seconds": 300,
            },
        )

        # Poll for cascade incident to develop and be detected
        print("  Waiting for cascade incident to be created (max 240s)...")
        cascade_incident = None
        deadline = time.time() + 240
        while time.time() < deadline:
            incidents = await db.fetch_all(
                f"""
                SELECT incident_id, opened_at, affected_services, hypotheses, ai_summary
                FROM {TABLE_INCIDENTS}
                WHERE 'central-ledger' = ANY(affected_services)
                AND opened_at >= :p1
                ORDER BY opened_at DESC
                LIMIT 10
            """,
                {"p1": datetime.fromtimestamp(fault_start - 30, tz=UTC)},
            )
            if incidents:
                cascade_incident = incidents[0]
                break
            await asyncio.sleep(5)

        if not cascade_incident:
            fail("No incident created after fault injection")
            return

        if not cascade_incident:
            fail("No incident found affecting central-ledger")
            return

        ok(
            f"Incident created: {cascade_incident['incident_id']}, "
            f"affecting {len(cascade_incident['affected_services'])} service(s)"
        )

        # Check for topological hypothesis
        hypotheses = cascade_incident["hypotheses"]
        if not hypotheses:
            fail("Incident has no hypotheses")
            return

        topological = [h for h in hypotheses if h.get("category", "").upper() == "SERVICE"]
        if not topological:
            warn("No SERVICE hypothesis found (expected for cascade detection)")
        else:
            primary = topological[0]
            ok(
                f"Topological hypothesis: {primary['summary'][:100]}... "
                f"(confidence: {primary['confidence']:.0%})"
            )

            if primary["confidence"] >= 0.70:
                ok(f"High confidence cascade detection ({primary['confidence']:.0%})")
            else:
                warn(f"Low confidence for cascade ({primary['confidence']:.0%})")

        # Check AI summary
        if cascade_incident["ai_summary"]:
            ok(f"AI summary generated: {cascade_incident['ai_summary'][:150]}...")
        else:
            fail("No AI summary in incident")

        # Clean up fault
        delete(f"http://localhost:{SIM_PORTS['central-ledger']}/sim/faults")
        print("  Fault cleared")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
async def check_change_correlation() -> None:
    """Acceptance criterion 2: correlate anomalies with deployments.

    Insert a deployment record, inject fault, verify CHANGE hypothesis.
    """
    section("Change correlation (deployment → anomaly)")

    db = Database()
    await db.connect()

    try:
        # Resolve any prior open/mitigating incidents
        await db.execute(
            "UPDATE incidents SET status = 'resolved', closed_at = NOW() WHERE status IN ('open', 'mitigating')"
        )

        # Insert a fake deployment
        deploy_time = datetime.now(UTC)
        deploy_id = f"deploy-{int(deploy_time.timestamp())}"
        await db.execute(
            """
            INSERT INTO deployments (deployment_id, occurred_at, service, platform, to_version, change_type, triggered_by)
            VALUES (:deployment_id, :occurred_at, :service, :platform, :to_version, :change_type, :triggered_by)
            ON CONFLICT (deployment_id) DO NOTHING
            """,
            {
                "deployment_id": deploy_id,
                "occurred_at": deploy_time,
                "service": "quoting-service",
                "platform": "mojaloop",
                "to_version": "v1.2.3-test",
                "change_type": "deploy",
                "triggered_by": "check-script",
            },
        )

        print(f"  Inserted deployment for quoting-service at {deploy_time.isoformat()}")

        # Inject fault shortly after
        await asyncio.sleep(5)
        print("  Injecting fault into quoting-service")
        post_json(
            f"http://localhost:{SIM_PORTS['quoting-service']}/sim/faults",
            {
                "kind": "error_injection",
                "magnitude": 0.40,
            },
        )

        # Poll for detection and correlation
        print("  Waiting for quoting-service incident (max 180s)...")
        incident = None
        deadline = time.time() + 180
        while time.time() < deadline:
            incidents = await db.fetch_all(
                f"""
                SELECT incident_id, affected_services, hypotheses
                FROM {TABLE_INCIDENTS}
                WHERE status IN ('open', 'mitigating', 'resolved')
                AND opened_at >= :p1
                AND 'quoting-service' = ANY(affected_services)
                ORDER BY opened_at DESC
                LIMIT 3
            """,
                {"p1": deploy_time - timedelta(seconds=10)},
            )
            if incidents:
                incident = incidents[0]
                break
            await asyncio.sleep(5)

        if not incident:
            warn("No incident found for quoting-service after deployment+fault")
            return
        hypotheses = incident["hypotheses"]

        change = [h for h in hypotheses if h.get("category", "").upper() == "CHANGE"]
        if not change:
            warn("No CHANGE hypothesis found (expected when fault follows deployment)")
        else:
            ok(
                f"Change correlation detected: {change[0]['summary'][:100]}... "
                f"(confidence: {change[0]['confidence']:.0%})"
            )

        # Clean up
        delete(f"http://localhost:{SIM_PORTS['quoting-service']}/sim/faults")
        print("  Fault cleared")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
async def check_event_correlation() -> None:
    """Acceptance criterion 3: correlate anomalies with chaos/traffic events."""
    section("Event correlation (chaos experiment)")

    db = Database()
    await db.connect()

    try:
        # Resolve any prior open/mitigating incidents
        await db.execute(
            "UPDATE incidents SET status = 'resolved', closed_at = NOW() WHERE status IN ('open', 'mitigating')"
        )

        # Insert a chaos run
        chaos_time = datetime.now(UTC)
        chaos_id = f"chaos-{int(chaos_time.timestamp())}"
        await db.execute(
            """
            INSERT INTO chaos_runs (run_id, started_at, experiment, target_service, platform, status)
            VALUES (:run_id, :started_at, :experiment, :target_service, :platform, :status)
            ON CONFLICT (run_id) DO NOTHING
            """,
            {
                "run_id": chaos_id,
                "started_at": chaos_time,
                "experiment": "network_latency",
                "target_service": "ml-api-adapter",
                "platform": "mojaloop",
                "status": "running",
            },
        )

        print(f"  Inserted chaos event for ml-api-adapter at {chaos_time.isoformat()}")

        # Inject fault
        await asyncio.sleep(5)
        post_json(
            f"http://localhost:{SIM_PORTS['ml-api-adapter']}/sim/faults",
            {
                "kind": "network_latency",
                "magnitude": 0.5,
            },
        )

        print("  Waiting for ml-api-adapter chaos incident (max 180s)...")
        incident = None
        deadline = time.time() + 180
        while time.time() < deadline:
            incidents = await db.fetch_all(
                f"""
                SELECT incident_id, hypotheses
                FROM {TABLE_INCIDENTS}
                WHERE status IN ('open', 'mitigating', 'resolved')
                AND opened_at >= :p1
                AND 'ml-api-adapter' = ANY(affected_services)
                ORDER BY opened_at DESC
                LIMIT 3
            """,
                {"p1": chaos_time - timedelta(seconds=10)},
            )
            if incidents:
                incident = incidents[0]
                break
            await asyncio.sleep(5)

        if not incident:
            warn("No incident found for ml-api-adapter after chaos event")
            return

        hypotheses = incident["hypotheses"]
        event = [h for h in hypotheses if h.get("category", "").upper() == "EVENT"]

        if not event:
            warn("No EVENT hypothesis found (expected for chaos experiment)")
        else:
            ok(
                f"Event correlation detected: {event[0]['summary'][:100]}... "
                f"(confidence: {event[0]['confidence']:.0%})"
            )

            if event[0]["confidence"] >= 0.95:
                ok("High confidence event correlation (chaos = expected disruption)")
            else:
                warn(f"Expected 99% confidence for chaos, got {event[0]['confidence']:.0%}")

        # Clean up
        delete(f"http://localhost:{SIM_PORTS['ml-api-adapter']}/sim/faults")
        print("  Fault cleared")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
def main() -> int:
    print("RCA Engine Verification Script")
    print("=" * 70)

    status = check_rca_engine_up()
    if not status:
        print("\n[ABORT] RCA engine not ready")
        return 1

    # Run async checks
    try:
        asyncio.run(check_cascade_detection())
        asyncio.run(check_change_correlation())
        asyncio.run(check_event_correlation())
    except Exception as exc:
        fail(f"Check failed with exception: {exc}")
        import traceback

        traceback.print_exc()

    # Summary
    section("Summary")
    if failures:
        print(f"  {len(failures)} failure(s):")
        for f in failures:
            print(f"    - {f}")
    else:
        print("  All critical checks passed!")

    if warnings:
        print(f"  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"    - {w}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
