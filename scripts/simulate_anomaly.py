"""Inject a temporary fault into a running Mojaloop simulator.

Usage:
    python scripts/simulate_anomaly.py
    python scripts/simulate_anomaly.py --service quoting-service --fault network_latency
    python scripts/simulate_anomaly.py --fault cpu_hog --duration-seconds 180 --magnitude 0.8

The simulator automatically expires injected faults. The stack must be running with the
observability layer enabled.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

SIM_PORTS = {
    "ml-api-adapter": 8011,
    "central-ledger": 8012,
    "quoting-service": 8013,
    "account-lookup-service": 8014,
    "transaction-request-service": 8015,
}

# Exclude kill_service: it terminates the simulator container instead of producing an
# ongoing metric signal.
FAULTS = (
    "network_latency",
    "error_injection",
    "memory_hog",
    "cpu_hog",
    "traffic_spike",
    "bad_deploy",
    "queue_backlog",
    "settlement_delay",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inject an anomaly-producing simulator fault.")
    parser.add_argument(
        "--service",
        choices=sorted(SIM_PORTS),
        default="central-ledger",
        help="simulated service to affect (default: central-ledger)",
    )
    parser.add_argument(
        "--fault",
        choices=FAULTS,
        default="error_injection",
        help="fault signature to inject (default: error_injection)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=120,
        help="duration before automatic expiry, from 1 to 1800 (default: 120)",
    )
    parser.add_argument(
        "--magnitude",
        type=float,
        help="fault strength; defaults to the simulator's value for this fault",
    )
    parser.add_argument("--notes", default="Manual anomaly simulation")
    args = parser.parse_args()

    if not 1 <= args.duration_seconds <= 1800:
        parser.error("--duration-seconds must be between 1 and 1800")
    if args.magnitude is not None and args.magnitude < 0:
        parser.error("--magnitude cannot be negative")
    return args


def inject(args: argparse.Namespace) -> dict[str, Any]:
    port = SIM_PORTS[args.service]
    url = f"http://localhost:{port}/sim/faults"
    body: dict[str, Any] = {
        "kind": args.fault,
        "duration_seconds": args.duration_seconds,
        "injected_by": "simulate_anomaly.py",
        "notes": args.notes,
    }
    if args.magnitude is not None:
        body["magnitude"] = args.magnitude

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return json.load(response)


def main() -> int:
    args = parse_args()
    try:
        result = inject(args)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"Simulator rejected the fault (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"Could not reach the simulator at localhost:{SIM_PORTS[args.service]}: {exc.reason}",
            file=sys.stderr,
        )
        return 1

    fault = result.get("injected", {})
    magnitude = fault.get("magnitude", "default")
    expiry = fault.get("expires_at", "unknown")
    print(f"Injected {args.fault} into {args.service} (magnitude {magnitude}).")
    print(f"Fault ID: {fault.get('fault_id', 'unknown')}")
    print(f"Expires at: {expiry}")
    print(
        "The fault will automatically expire; observe metrics and anomalies in the dashboard."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
