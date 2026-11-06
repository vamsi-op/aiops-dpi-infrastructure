"""Chaos Engineering Experiment Runner.

Runs a controlled fault injection experiment against a DPI microservice and
records TTD (time-to-detect), TTR (time-to-RCA) and TTREM (time-to-remediate)
into the chaos_runs table.

Usage::

    python scripts/run_chaos_experiment.py --service central-ledger \
        --experiment error_injection --magnitude 0.3 --duration 120

Fault types: network_latency, error_injection, memory_hog, cpu_hog,
             traffic_spike, bad_deploy, kill_service
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_URL = os.getenv("POSTGRES_URL") or (
    f"postgresql+asyncpg://{os.getenv('POSTGRES_USER', 'aiops')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'aiops_local_dev_password')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'aiops')}"
)

# Map each sim service name to its host port
SIM_PORTS: dict[str, int] = {
    "ml-api-adapter": 8011,
    "central-ledger": 8012,
    "quoting-service": 8013,
    "account-lookup-service": 8014,
    "transaction-request-service": 8015,
}

POLL_INTERVAL = 5          # seconds between DB polls
MAX_WAIT_DETECT = 300      # 5 min to detect anomaly
MAX_WAIT_RCA = 300         # 5 min to create incident
MAX_WAIT_HEAL = 300        # 5 min for healing action


# ---------------------------------------------------------------------------
# DB helpers (synchronous via asyncio.run)
# ---------------------------------------------------------------------------

async def _run_experiment(args: argparse.Namespace) -> None:
    engine = create_async_engine(DB_URL, echo=False)

    async with engine.connect() as conn:
        platform = "mojaloop"
        run_id = f"chaos-{int(time.time())}"
        experiment_start = datetime.now(UTC)

        # 1. Resolve simulator port
        port = SIM_PORTS.get(args.service)
        if not port:
            print(f"[ERROR] Unknown service '{args.service}'. Known: {list(SIM_PORTS)}")
            sys.exit(1)

        sim_url = f"http://localhost:{port}"

        # 2. Insert chaos_run record
        print(f"\n{'='*60}")
        print(f" Chaos Experiment: {args.experiment} on {args.service}")
        print(f" Run ID : {run_id}")
        print(f" Start  : {experiment_start.isoformat()}")
        print(f"{'='*60}\n")

        await conn.execute(
            sa.text("""
                INSERT INTO chaos_runs
                    (run_id, started_at, experiment, target_service, platform,
                     duration_seconds, parameters, status)
                VALUES
                    (:run_id, :started_at, :experiment, :target_service, :platform,
                     :duration_seconds, CAST(:parameters AS jsonb), 'running')
            """),
            {
                "run_id": run_id,
                "started_at": experiment_start,
                "experiment": args.experiment,
                "target_service": args.service,
                "platform": platform,
                "duration_seconds": args.duration,
                "parameters": json.dumps({"magnitude": args.magnitude}),
            },
        )
        await conn.commit()
        print(f"[+] chaos_runs row inserted: {run_id}")

        # 3. Inject fault
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{sim_url}/sim/faults",
                    json={"kind": args.experiment, "magnitude": args.magnitude},
                )
                resp.raise_for_status()
                print(f"[+] Fault injected via {sim_url}/sim/faults  → {resp.status_code}")
        except Exception as exc:
            print(f"[ERROR] Could not inject fault: {exc}")
            await _update_chaos_run(conn, run_id, status="failed", notes={"error": str(exc)})
            await conn.commit()
            sys.exit(1)

        t_fault = time.monotonic()

        # ------------------------------------------------------------------
        # 4. Poll for anomaly detection (TTD)
        # ------------------------------------------------------------------
        print(f"\n[~] Waiting for anomaly on '{args.service}' (max {MAX_WAIT_DETECT}s)…")
        anomaly_id: str | None = None
        t_detected: datetime | None = None
        ttd: float | None = None

        deadline = t_fault + MAX_WAIT_DETECT
        while time.monotonic() < deadline:
            row = (await conn.execute(
                sa.text("""
                    SELECT anomaly_id, detected_at
                    FROM anomalies
                    WHERE service = :svc AND detected_at >= :start
                    ORDER BY detected_at
                    LIMIT 1
                """),
                {"svc": args.service, "start": experiment_start},
            )).fetchone()

            if row:
                anomaly_id = row[0]
                t_detected = row[1]
                # Make timezone-aware if naive
                if t_detected.tzinfo is None:
                    t_detected = t_detected.replace(tzinfo=UTC)
                ttd = (t_detected - experiment_start).total_seconds()
                print(f"[+] Anomaly detected!  ID={anomaly_id}  TTD={ttd:.1f}s")
                break

            await asyncio.sleep(POLL_INTERVAL)
        else:
            print(f"[WARN] No anomaly detected within {MAX_WAIT_DETECT}s")

        # ------------------------------------------------------------------
        # 5. Poll for incident creation (TTR = Time-to-RCA)
        # ------------------------------------------------------------------
        incident_id: str | None = None
        t_incident: datetime | None = None
        ttr: float | None = None

        if anomaly_id:
            print(f"\n[~] Waiting for incident (TTR) …")
            deadline = time.monotonic() + MAX_WAIT_RCA
            while time.monotonic() < deadline:
                row = (await conn.execute(
                    sa.text("""
                        SELECT incident_id, opened_at
                        FROM incidents
                        WHERE :svc = ANY(affected_services)
                          AND opened_at >= :start
                        ORDER BY opened_at
                        LIMIT 1
                    """),
                    {"svc": args.service, "start": experiment_start},
                )).fetchone()

                if row:
                    incident_id = row[0]
                    t_incident = row[1]
                    if t_incident.tzinfo is None:
                        t_incident = t_incident.replace(tzinfo=UTC)
                    ttr = (t_incident - experiment_start).total_seconds()
                    print(f"[+] Incident created!  ID={incident_id}  TTR={ttr:.1f}s")
                    break

                await asyncio.sleep(POLL_INTERVAL)
            else:
                print(f"[WARN] No incident created within {MAX_WAIT_RCA}s")

        # ------------------------------------------------------------------
        # 6. Poll for healing action (TTREM = Time-to-Remediate)
        # ------------------------------------------------------------------
        t_remediated: datetime | None = None
        ttrem: float | None = None
        healing_playbook: str | None = None

        if incident_id:
            print(f"\n[~] Waiting for healing action (TTREM) …")
            deadline = time.monotonic() + MAX_WAIT_HEAL
            while time.monotonic() < deadline:
                row = (await conn.execute(
                    sa.text("""
                        SELECT action_id, created_at, playbook, status
                        FROM healing_actions
                        WHERE incident_id = :iid
                        ORDER BY created_at
                        LIMIT 1
                    """),
                    {"iid": incident_id},
                )).fetchone()

                if row:
                    t_remediated = row[1]
                    if t_remediated.tzinfo is None:
                        t_remediated = t_remediated.replace(tzinfo=UTC)
                    ttrem = (t_remediated - experiment_start).total_seconds()
                    healing_playbook = row[2]
                    print(f"[+] Healing action!  playbook={healing_playbook}  TTREM={ttrem:.1f}s  status={row[3]}")
                    break

                # Also check anomaly-triggered actions (no incident_id)
                if anomaly_id:
                    row = (await conn.execute(
                        sa.text("""
                            SELECT action_id, created_at, playbook, status
                            FROM healing_actions
                            WHERE anomaly_id = :aid
                            ORDER BY created_at
                            LIMIT 1
                        """),
                        {"aid": anomaly_id},
                    )).fetchone()

                    if row:
                        t_remediated = row[1]
                        if t_remediated.tzinfo is None:
                            t_remediated = t_remediated.replace(tzinfo=UTC)
                        ttrem = (t_remediated - experiment_start).total_seconds()
                        healing_playbook = row[2]
                        print(f"[+] Healing action (via anomaly)!  playbook={healing_playbook}  TTREM={ttrem:.1f}s  status={row[3]}")
                        break

                await asyncio.sleep(POLL_INTERVAL)
            else:
                print(f"[WARN] No healing action within {MAX_WAIT_HEAL}s")
        elif anomaly_id:
            # Try without an incident (anomaly-only healing)
            print(f"\n[~] Waiting for anomaly-based healing action …")
            deadline = time.monotonic() + MAX_WAIT_HEAL
            while time.monotonic() < deadline:
                row = (await conn.execute(
                    sa.text("""
                        SELECT action_id, created_at, playbook, status
                        FROM healing_actions
                        WHERE anomaly_id = :aid
                        ORDER BY created_at
                        LIMIT 1
                    """),
                    {"aid": anomaly_id},
                )).fetchone()

                if row:
                    t_remediated = row[1]
                    if t_remediated.tzinfo is None:
                        t_remediated = t_remediated.replace(tzinfo=UTC)
                    ttrem = (t_remediated - experiment_start).total_seconds()
                    healing_playbook = row[2]
                    print(f"[+] Healing action!  playbook={healing_playbook}  TTREM={ttrem:.1f}s  status={row[3]}")
                    break

                await asyncio.sleep(POLL_INTERVAL)
            else:
                print(f"[WARN] No healing action within {MAX_WAIT_HEAL}s")

        # ------------------------------------------------------------------
        # 7. Clear fault
        # ------------------------------------------------------------------
        print(f"\n[~] Clearing faults on {sim_url} …")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(f"{sim_url}/sim/faults")
                print(f"[+] Faults cleared  → {resp.status_code}")
        except Exception as exc:
            print(f"[WARN] Could not clear fault: {exc}")

        # ------------------------------------------------------------------
        # 8. Update chaos_run with results
        # ------------------------------------------------------------------
        notes = {
            "ttd_seconds": round(ttd, 2) if ttd is not None else None,
            "ttr_seconds": round(ttr, 2) if ttr is not None else None,
            "ttrem_seconds": round(ttrem, 2) if ttrem is not None else None,
            "playbook": healing_playbook,
        }

        await _update_chaos_run(
            conn,
            run_id,
            status="completed",
            detected_at=t_detected,
            detected_anomaly_id=anomaly_id,
            incident_id=incident_id,
            remediated_at=t_remediated,
            notes=notes,
            ended_at=datetime.now(UTC),
        )
        await conn.commit()

        # ------------------------------------------------------------------
        # 9. Summary
        # ------------------------------------------------------------------
        print(f"\n{'='*60}")
        print(f" EXPERIMENT RESULTS — {run_id}")
        print(f"{'='*60}")
        print(f"  Service     : {args.service}")
        print(f"  Experiment  : {args.experiment} (magnitude={args.magnitude})")
        print(f"  TTD         : {f'{ttd:.1f}s' if ttd is not None else 'NOT DETECTED'}")
        print(f"  TTR         : {f'{ttr:.1f}s' if ttr is not None else 'NO INCIDENT'}")
        print(f"  TTREM       : {f'{ttrem:.1f}s' if ttrem is not None else 'NO HEALING'}")
        print(f"  Playbook    : {healing_playbook or 'N/A'}")
        print(f"  Anomaly ID  : {anomaly_id or 'N/A'}")
        print(f"  Incident ID : {incident_id or 'N/A'}")
        print(f"{'='*60}\n")

    await engine.dispose()


async def _update_chaos_run(
    conn: sa.ext.asyncio.AsyncConnection,
    run_id: str,
    *,
    status: str,
    notes: dict | None = None,
    detected_at: datetime | None = None,
    detected_anomaly_id: str | None = None,
    incident_id: str | None = None,
    remediated_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> None:
    await conn.execute(
        sa.text("""
            UPDATE chaos_runs SET
                status            = :status,
                ended_at          = :ended_at,
                detected_at       = :detected_at,
                detected_anomaly_id = :detected_anomaly_id,
                incident_id       = :incident_id,
                remediated_at     = :remediated_at,
                notes             = CAST(:notes AS jsonb)
            WHERE run_id = :run_id
        """),
        {
            "run_id": run_id,
            "status": status,
            "ended_at": ended_at or datetime.now(UTC),
            "detected_at": detected_at,
            "detected_anomaly_id": detected_anomaly_id,
            "incident_id": incident_id,
            "remediated_at": remediated_at,
            "notes": json.dumps(notes or {}),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a chaos engineering experiment")
    parser.add_argument(
        "--service", default="central-ledger",
        choices=list(SIM_PORTS),
        help="Target microservice",
    )
    parser.add_argument(
        "--experiment", default="error_injection",
        choices=["network_latency", "error_injection", "memory_hog",
                 "cpu_hog", "traffic_spike", "bad_deploy", "kill_service"],
        help="Fault type to inject",
    )
    parser.add_argument(
        "--magnitude", type=float, default=0.25,
        help="Fault magnitude 0.0-1.0",
    )
    parser.add_argument(
        "--duration", type=int, default=120,
        help="Expected experiment duration in seconds (informational)",
    )
    args = parser.parse_args()
    asyncio.run(_run_experiment(args))


if __name__ == "__main__":
    main()
