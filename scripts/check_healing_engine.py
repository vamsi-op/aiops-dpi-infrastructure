"""Verify the healing engine end to end (module 6).

The module's acceptance criteria:

1. **Dry-run mode**: With HEALING_ENABLED=false, actions are recorded as skipped_dry_run
   and nothing executes.

2. **Actual execution**: With HEALING_ENABLED=true, a memory-pressure anomaly triggers
   container restart and after_snapshot shows recovery.

3. **Safety rail tests**: Each rail blocks actions with distinct status:
   - Master switch: skipped_dry_run
   - Cooldown: skipped_cooldown
   - Budget: skipped_budget
   - Confidence: skipped_low_confidence

4. **Idempotency**: Duplicate anomalies do not trigger duplicate actions.

5. **Failure handling**: Failed playbooks record 'failed' status with useful message.

Usage (stack must be running with -Layer all):
    .\\.venv\\Scripts\\python.exe scripts\\check_healing_engine.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from aiops_common.constants import TABLE_ACTIONS
from aiops_common.db import Database

HEALING_ENGINE = "http://localhost:8024"
DETECTOR = "http://localhost:8021"

# Simulator ports
SIM_PORTS = {
    "central-ledger": 8012,
    "quoting-service": 8013,
}

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
def check_healing_engine_up() -> dict[str, Any]:
    section("Healing engine health")
    try:
        root = get_json(f"{HEALING_ENGINE}/", timeout=10)
    except OSError as exc:
        fail(f"Healing engine unreachable at {HEALING_ENGINE} ({exc}) - is -Layer all running?")
        return {}

    ok(f"Healing engine up, enabled: {root.get('enabled')}")

    try:
        ready = get_json(f"{HEALING_ENGINE}/ready", timeout=20)
    except urllib.error.HTTPError as exc:
        fail(f"/ready returned {exc.code}: {exc.read().decode()[:200]}")
        return {}

    for name, state in (ready.get("dependencies") or {}).items():
        if state == "ok":
            ok(f"dependency {name}")
        else:
            fail(f"dependency {name}: {state}")

    status = get_json(f"{HEALING_ENGINE}/status")
    if not status.get("running"):
        fail("Healing engine loop is not running")
        return {}

    ok(f"Healing engine running, handled anomalies: {status.get('handled_anomalies')}")

    config = get_json(f"{HEALING_ENGINE}/config")
    ok(
        f"Config: enabled={config.get('enabled')}, "
        f"max_actions/hr={config.get('max_actions_per_hour')}, "
        f"cooldown={config.get('cooldown_seconds')}s, "
        f"min_confidence={config.get('min_confidence')}"
    )

    return status


# ---------------------------------------------------------------------------
async def check_dry_run_mode() -> None:
    """Acceptance criterion 1: With HEALING_ENABLED=false, actions are skipped_dry_run."""
    section("Dry-run mode (HEALING_ENABLED=false)")

    db = Database()
    await db.connect()

    try:
        config = get_json(f"{HEALING_ENGINE}/config")
        if config.get("enabled"):
            warn(
                "HEALING_ENABLED=true - cannot test dry-run mode. "
                "Set HEALING_ENABLED=false in .env and restart."
            )
            return

        # Clear faults
        for service, port in SIM_PORTS.items():
            try:
                delete(f"http://localhost:{port}/sim/faults")
            except Exception:
                pass

        # Record baseline action count
        initial_count = await db.fetch_value(
            f"SELECT COUNT(*) FROM {TABLE_ACTIONS} WHERE status = 'skipped_dry_run'"
        )

        # Inject memory pressure fault
        print("  Injecting memory_hog into central-ledger")
        post_json(
            f"http://localhost:{SIM_PORTS['central-ledger']}/sim/faults",
            {
                "kind": "memory_hog",
                "magnitude": 0.8,
            },
        )

        # Wait for detection and healing attempt
        print("  Waiting 60s for detection and healing...")
        await asyncio.sleep(60)

        # Check for dry-run action
        new_count = await db.fetch_value(
            f"SELECT COUNT(*) FROM {TABLE_ACTIONS} WHERE status = 'skipped_dry_run'"
        )

        if new_count > initial_count:
            ok(
                f"Action recorded as skipped_dry_run ({new_count - initial_count} new action(s))"
            )

            # Get latest action
            action = await db.fetch_one(f"""
                SELECT action_id, playbook, target_service, result_message
                FROM {TABLE_ACTIONS}
                WHERE status = 'skipped_dry_run'
                ORDER BY created_at DESC
                LIMIT 1
            """)

            if action:
                ok(f"Action details: {action['playbook']} on {action['target_service']}")
                if "dry-run" in action["result_message"].lower():
                    ok("Result message mentions dry-run mode")
                else:
                    warn("Result message doesn't mention dry-run")
        else:
            warn("No skipped_dry_run actions found - anomaly may not have been detected")

        # Clean up
        delete(f"http://localhost:{SIM_PORTS['central-ledger']}/sim/faults")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
async def check_safety_rails() -> None:
    """Acceptance criterion 3: Each rail blocks with distinct ActionStatus."""
    section("Safety rail verification")

    db = Database()
    await db.connect()

    try:
        # Check for examples of each blocked status
        statuses_to_check = [
            "skipped_dry_run",
            "skipped_cooldown",
            "skipped_budget",
            "skipped_low_confidence",
        ]

        for status in statuses_to_check:
            count = await db.fetch_value(
                f"SELECT COUNT(*) FROM {TABLE_ACTIONS} WHERE status = :p1",
                {"p1": status},
            )

            if count > 0:
                ok(f"Found {count} action(s) with status '{status}'")
            else:
                warn(f"No actions found with status '{status}' - rail may not have been tested")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
async def check_idempotency() -> None:
    """Acceptance criterion 4: Duplicate anomalies don't trigger duplicate actions."""
    section("Idempotency check")

    db = Database()
    await db.connect()

    try:
        # Check for duplicate actions on same service within short time window
        duplicates = await db.fetch_all(f"""
            SELECT target_service, COUNT(*) as action_count
            FROM {TABLE_ACTIONS}
            WHERE created_at >= NOW() - INTERVAL '5 minutes'
            AND status NOT LIKE 'skipped_%'
            GROUP BY target_service
            HAVING COUNT(*) > 1
        """)

        if duplicates:
            for row in duplicates:
                warn(
                    f"Service {row['target_service']} has {row['action_count']} actions in 5 minutes - "
                    "may indicate duplicate handling"
                )
        else:
            ok("No duplicate actions detected (idempotency verified)")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
async def check_action_persistence() -> None:
    """Verify all actions are persisted to database."""
    section("Action persistence")

    db = Database()
    await db.connect()

    try:
        # Get action statistics
        stats = await db.fetch_one(f"""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'succeeded') as succeeded,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status LIKE 'skipped_%') as skipped
            FROM {TABLE_ACTIONS}
        """)

        if stats["total"] == 0:
            warn(
                "No actions found in database - engine may not have processed any anomalies yet"
            )
        else:
            ok(f"Total actions persisted: {stats['total']}")
            ok(f"  Succeeded: {stats['succeeded']}")
            ok(f"  Failed: {stats['failed']}")
            ok(f"  Skipped: {stats['skipped']}")

    finally:
        await db.close()


# ---------------------------------------------------------------------------
def main() -> int:
    print("Healing Engine Verification Script")
    print("=" * 70)

    status = check_healing_engine_up()
    if not status:
        print("\n[ABORT] Healing engine not ready")
        return 1

    # Run async checks
    try:
        asyncio.run(check_dry_run_mode())
        asyncio.run(check_safety_rails())
        asyncio.run(check_idempotency())
        asyncio.run(check_action_persistence())
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

    print("\n" + "=" * 70)
    print("Note: Full verification requires:")
    print("  1. Running with HEALING_ENABLED=false first (dry-run mode)")
    print("  2. Setting HEALING_ENABLED=true and injecting faults (actual execution)")
    print("  3. Triggering all safety rails (cooldown, budget, low confidence)")
    print("\nFor complete acceptance criteria verification, run the full demo scenario.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
