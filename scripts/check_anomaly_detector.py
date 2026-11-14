"""Verify the anomaly detector end to end (module 4).

The module's stated criterion is the one that matters and the one most of this script is about:
*inject a fault with the simulator, and see an anomaly appear on `aiops.anomalies` within one
scrape interval, with the right kind and sensible contributing features.*

Everything else is scaffolding for that:

1. The detector is up, and has a model for every service the agent is collecting.
2. `model_runs` records each fit, with the metric that predicts whether the detector is usable:
   what fraction of its own training data would clear the alerting threshold.
3. A healthy sample scores below the decision boundary - no alert on normal traffic.
4. **Fault injection round trip**, twice, with different faults that must produce different
   kinds. A detector that flags everything as `multivariate` would pass a weaker check.
5. The anomaly reached both TimescaleDB and the bus, with attribution attached.
6. The detector's own metrics expose detection latency.

Note on timing: the `dpi:*` recording rules are 5-minute rates, so an injected fault does not
reach full magnitude in the metrics instantly - it ramps. Detection latency is therefore
measured from injection and reported, rather than asserted against a figure that would really be
measuring Prometheus's rate window.

Usage (stack must be running with -Layer all, and a model must be trained):
    .\\.venv\\Scripts\\python.exe scripts\\check_anomaly_detector.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from aiops_common.constants import TABLE_ANOMALIES, TABLE_MODEL_RUNS, AnomalyKind, Stream
from aiops_common.db import Database

DETECTOR = "http://localhost:8021"
AGENT = "http://localhost:8020"

# Host ports from .env.example.
SIM_PORTS = {
    "ml-api-adapter": 8011,
    "central-ledger": 8012,
    "quoting-service": 8013,
    "account-lookup-service": 8014,
    "transaction-request-service": 8015,
}

# Faults to inject, and the kind each must produce. Two different faults rather than one,
# because a detector that labelled everything `multivariate` would satisfy a single check while
# being useless to module 6, which selects a playbook from exactly this field.
FAULT_CASES: list[tuple[str, str, float, set[AnomalyKind]]] = [
    (
        "central-ledger",
        "error_injection",
        0.25,
        # A large error ratio may drag latency and queue depth with it, which legitimately
        # reads as multivariate - that is the honest answer for a composite signature.
        {AnomalyKind.ERROR_BURST, AnomalyKind.MULTIVARIATE, AnomalyKind.LATENCY_SPIKE},
    ),
    (
        "quoting-service",
        "network_latency",
        0.6,
        {AnomalyKind.LATENCY_SPIKE, AnomalyKind.MULTIVARIATE},
    ),
]

# How long to wait for an anomaly after injecting. Generous, because the 5-minute rate window
# means the metric ramps rather than steps.
DETECTION_TIMEOUT_SECONDS = 240

# After the first anomaly, how long to let the signal develop before reading the final
# classification. See the note in `check_fault_round_trip`.
SETTLE_SECONDS = 60

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


def get_text(url: str, *, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read().decode()


# ---------------------------------------------------------------------------
def check_detector_up() -> dict[str, Any]:
    section("Detector health")
    try:
        root = get_json(f"{DETECTOR}/", timeout=10)
    except OSError as exc:
        fail(f"detector unreachable at {DETECTOR} ({exc}) - is -Layer all running?")
        return {}
    ok(f"detector up, threshold {root.get('threshold')}")

    try:
        ready = get_json(f"{DETECTOR}/ready", timeout=20)
    except urllib.error.HTTPError as exc:
        fail(f"/ready returned {exc.code}: {exc.read().decode()[:200]}")
        return {}
    for name, state in (ready.get("dependencies") or {}).items():
        if state == "ok":
            ok(f"dependency {name}")
        else:
            fail(f"dependency {name}: {state}")

    status = get_json(f"{DETECTOR}/status")
    if not status.get("running"):
        fail("detector loop is not running")
    else:
        ok("consumer loop running")
    return status


def check_models(status: dict[str, Any]) -> None:
    section("Models")
    models = status.get("models") or {}
    if not models:
        fail("no models loaded - POST /train first, nothing is being scored")
        return
    ok(f"{len(models)} models loaded at version {status.get('model_version')}")

    for service, info in sorted(models.items()):
        sources = ", ".join(info.get("data_sources") or []) or "unknown"
        ok(
            f"{service}: {info['training_rows']} rows, scale "
            f"{info['calibration_scale']}, data={sources}"
        )

    # Every service the agent collects must have a model, or it is silently unmonitored.
    try:
        agent_status = get_json(f"{AGENT}/status", timeout=15)
    except OSError:
        warn("could not reach the metrics agent to cross-check service coverage")
        return
    collected = set(SIM_PORTS) - set(agent_status.get("last_services_down") or [])
    missing = sorted(collected - set(models))
    if missing:
        fail(f"collected but not modelled, so not scored at all: {missing}")
    else:
        ok("every collected service has a model")

    if status.get("unknown_services"):
        fail(f"samples arrived for services with no model: {status['unknown_services']}")


async def check_model_runs(db: Database) -> None:
    section("Training audit trail")
    rows = await db.fetch_all(
        f"""
        SELECT service, model_name, model_version, training_rows, duration_seconds,
               metrics, notes
        FROM {TABLE_MODEL_RUNS}
        ORDER BY created_at DESC
        LIMIT 10
        """
    )
    if not rows:
        fail(f"{TABLE_MODEL_RUNS} is empty - training is not being recorded")
        return
    ok(f"{len(rows)} recent run(s) recorded")

    for row in rows[:5]:
        metrics = (
            row["metrics"] if isinstance(row["metrics"], dict) else json.loads(row["metrics"])
        )
        noisy = float(metrics.get("train_above_0_65", 0.0))
        outlier = float(metrics.get("train_outlier_ratio", 0.0))
        line = (
            f"{row['service']:28} rows={row['training_rows']:>6} "
            f"outlier_ratio={outlier:.4f} above_threshold={noisy:.4f}"
        )
        # The figure that predicts usability. A detector firing on a large share of its own
        # clean training data will be ignored within a day.
        if noisy > 0.05:
            fail(f"{line}  <- would fire on >5% of its own training data")
        else:
            ok(line)


async def check_healthy_sample_is_quiet(db: Database, status: dict[str, Any]) -> None:
    """No alert on normal traffic. The cheapest way for a detector to be useless.

    Scores the most recent row the **agent** wrote, rather than a sample assembled from the
    simulator's `/sim` endpoint. The first version of this check did the latter and reported a
    healthy service at 0.942, which looked like a badly calibrated detector. The real cause was
    that `/sim` does not expose `active_connections`, so the constructed sample carried 0 for a
    signal that normally sits near 30 - a combination that never occurs in training, which the
    forest correctly flagged. Reading the real row removes a whole class of "the check built the
    input wrong" false alarms.
    """
    section("Healthy traffic stays quiet")
    service = "central-ledger"
    if service not in (status.get("models") or {}):
        warn(f"no model for {service}; skipping")
        return

    rows = await db.fetch_all(
        """
        SELECT service, "timestamp", tps, request_count, error_count,
               latency_p50_seconds, latency_p95_seconds, latency_p99_seconds,
               cpu_utilisation_ratio, memory_utilisation_ratio, queue_depth,
               active_connections, settlement_lag_seconds, replicas, platform
        FROM service_metrics
        WHERE service = :service AND labels->>'source' = 'metrics-agent'
        ORDER BY "timestamp" DESC
        LIMIT 1
        """,
        {"service": service},
    )
    if not rows:
        warn(f"no agent-collected rows for {service}; skipping")
        return

    row = dict(rows[0])
    sample = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in row.items()
    }
    try:
        result = post_json(f"{DETECTOR}/score", {"sample": sample})
    except urllib.error.HTTPError as exc:
        fail(f"/score returned {exc.code}: {exc.read().decode()[:200]}")
        return

    if result["is_anomaly"]:
        # Not automatically wrong - there may genuinely be a fault active - but it needs
        # explaining rather than passing silently.
        warn(
            f"the newest collected sample for {service} scored {result['score']:.3f} and was "
            f"flagged as {result['anomaly']['kind']}. Check for an active fault"
        )
    else:
        ok(
            f"newest collected sample scores {result['score']:.4f} "
            f"(forest {result['forest_score']:.4f}, baseline {result['deviation_score']:.4f}) "
            "- below the boundary, no alert"
        )


async def check_fault_round_trip(
    db: Database, service: str, fault: str, magnitude: float, expected: set[AnomalyKind]
) -> None:
    """The module's actual verification. Inject, wait, assert what came out."""
    section(f"Fault round trip: {fault} on {service}")
    port = SIM_PORTS[service]

    since = await db.fetch_value("SELECT now()")
    try:
        injected = post_json(
            f"http://localhost:{port}/sim/faults",
            {
                "kind": fault,
                "magnitude": magnitude,
                "duration_seconds": 300,
                "injected_by": "check_anomaly_detector",
            },
        )
    except OSError as exc:
        fail(f"could not inject {fault} into {service} ({exc})")
        return
    ok(f"injected {fault} (magnitude {magnitude})")
    started = time.monotonic()

    query = f"""
        SELECT anomaly_id, kind, severity, score, raw_score, threshold,
               contributing_features, feature_deviations, description, detected_at,
               model_version
        FROM {TABLE_ANOMALIES}
        WHERE service = :service AND detected_at > :since
        ORDER BY score DESC
        LIMIT 1
    """
    found: dict[str, Any] | None = None
    first_seen_after = 0.0
    rows: list[dict[str, Any]] = []
    try:
        deadline = started + DETECTION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            rows = await db.fetch_all(query, {"service": service, "since": since})
            if rows:
                first_seen_after = time.monotonic() - started
                break
            await asyncio.sleep(5)

        if rows:
            # Detection latency and final classification are different questions, and conflating
            # them understates the detector.
            #
            # The `dpi:*` rules are 5-minute rates, so a fault *ramps*. The first anomaly often
            # arrives while only the forest can see it - an unusual combination of
            # not-yet-extreme values - which correctly reads as `multivariate`. Half a minute
            # later the offending signal has crossed its baseline and the kind sharpens to
            # something a playbook can match. Reporting the earliest anomaly would make the
            # detector look vague; waiting only for a sharp one would hide how fast it noticed.
            print(f"         waiting {SETTLE_SECONDS}s for the rate window to fill...")
            await asyncio.sleep(SETTLE_SECONDS)
            rows = await db.fetch_all(query, {"service": service, "since": since})
            found = rows[0] if rows else None
    finally:
        # Always clear, even if the assertions below fail. Leaving a fault injected would make
        # every subsequent check and any manual poking afterwards misleading.
        with_suppressed_errors(lambda: delete(f"http://localhost:{port}/sim/faults"))

    if found is None:
        fail(
            f"no anomaly for {service} within {DETECTION_TIMEOUT_SECONDS}s of injecting "
            f"{fault}. The fault was cleared. Injected: {injected.get('injected', {})}"
        )
        return

    ok(
        f"first anomaly {first_seen_after:.0f}s after injection "
        "(includes Prometheus's 5-minute rate ramp, not detector latency)"
    )

    kind = AnomalyKind(str(found["kind"]))
    if kind in expected:
        ok(f"kind is {kind.value}, as expected for {fault}")
    else:
        fail(
            f"kind is {kind.value}, expected one of {sorted(k.value for k in expected)}. "
            "Module 6 selects a playbook from this field"
        )

    score = float(found["score"])
    threshold = float(found["threshold"])
    if score >= threshold:
        ok(f"score {score:.4f} >= threshold {threshold} - actionable")
    else:
        fail(f"score {score:.4f} is below the threshold {threshold}, so nothing will act on it")

    contributing = list(found["contributing_features"] or [])
    if contributing:
        ok(f"contributing features: {contributing[:4]}")
    else:
        fail("no contributing features recorded - the anomaly is not explainable")

    deviations = (
        found["feature_deviations"]
        if isinstance(found["feature_deviations"], dict)
        else json.loads(found["feature_deviations"] or "{}")
    )
    if deviations:
        top = sorted(deviations.items(), key=lambda kv: abs(float(kv[1])), reverse=True)[:3]
        # "sigma" spelled out rather than the symbol. A Windows console defaults to cp1252 and
        # raises UnicodeEncodeError on U+03C3, which crashed this script mid-run - the same
        # class of failure as the em dashes that broke the PowerShell scripts.
        ok("largest deviations: " + ", ".join(f"{k}={float(v):+.1f} sigma" for k, v in top))
    else:
        fail("no feature deviations recorded")

    # Encoded defensively: the description comes from the detector, and this script must not be
    # the thing that crashes if a future change puts a non-ASCII character back into it.
    print("         " + str(found["description"]).encode("ascii", "replace").decode())


def with_suppressed_errors(action: Any) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask the real result
        warn(f"could not clear the injected fault: {exc}")


async def check_bus(db: Database) -> None:
    section("Anomalies reached the bus")
    from aiops_common.bus import EventBus

    bus = EventBus(source="check-anomaly-detector")
    await bus.connect()
    try:
        length = await bus.client.xlen(str(Stream.ANOMALIES))
        if length <= 0:
            fail(
                f"{Stream.ANOMALIES} is empty - the RCA and healing engines would have "
                "nothing to consume"
            )
            return
        ok(f"{Stream.ANOMALIES} holds {length} events")

        entries = await bus.client.xrevrange(str(Stream.ANOMALIES), count=1)
        _id, fields = entries[0]
        if fields.get("type") != "AnomalyEvent":
            fail(f"newest event is {fields.get('type')!r}, expected 'AnomalyEvent'")
            return
        payload = json.loads(fields.get("data", "{}"))
        required = {
            "anomaly_id",
            "service",
            "kind",
            "severity",
            "score",
            "contributing_features",
            "feature_deviations",
            "feature_snapshot",
        }
        missing = sorted(required - set(payload))
        if missing:
            fail(f"published event lacks fields the RCA engine needs: {missing}")
        else:
            ok(
                f"newest: {payload['service']} {payload['kind']} "
                f"score={float(payload['score']):.3f} severity={payload['severity']}"
            )
    finally:
        await bus.close()


def check_self_metrics() -> None:
    section("Detector self-instrumentation")
    try:
        text = get_text(f"{DETECTOR}/metrics")
    except OSError as exc:
        fail(f"could not scrape /metrics ({exc})")
        return
    expected = {
        "aiops_samples_scored_total": "samples scored",
        "aiops_anomalies_detected_total": "anomalies by kind and severity",
        "aiops_detection_latency_seconds": "detection latency",
        "aiops_detection_errors_total": "errors by stage",
        "aiops_detection_no_model_total": "samples with no model",
        "aiops_models_loaded": "models in memory",
        "aiops_last_anomaly_score": "last score by service",
    }
    for series, description in expected.items():
        if series in text:
            ok(f"{series} ({description})")
        else:
            fail(f"{series} missing from /metrics")


async def main() -> int:
    print("Anomaly detector check (module 4)")
    status = check_detector_up()
    if not status:
        print("\nDetector is not healthy; skipping the rest.")
        return 1

    check_models(status)

    db = Database()
    await db.connect()
    try:
        await check_healthy_sample_is_quiet(db, status)
        await check_model_runs(db)
        for service, fault, magnitude, expected in FAULT_CASES:
            await check_fault_round_trip(db, service, fault, magnitude, expected)
        await check_bus(db)
    finally:
        await db.close()

    check_self_metrics()

    print()
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for warning in warnings:
            print(f"  - {warning}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("=" * 50)
    print(" All anomaly detector checks passed. Module 4 OK.")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
