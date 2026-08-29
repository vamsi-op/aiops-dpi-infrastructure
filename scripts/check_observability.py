"""Verify the observability plane end to end.

Checks, in order:

1. Prometheus is up and every Mojaloop target is being scraped.
2. Every recording rule in `infra/prometheus/rules/` actually evaluates to data.
3. The exporter agrees with the simulator - `rate(dpi_transaction_total[1m])` matches the
   TPS each service reports on `/sim`. A mismatch means the counter arithmetic is wrong,
   which would silently corrupt every metric downstream.
4. Grafana has the provisioned datasource and dashboard.
5. Every dashboard panel query returns at least one series, so no panel is silently blank.

Point 5 is the one that repays the effort. A dashboard whose JSON is valid and whose panels
all render "No data" looks finished and is useless, and nothing else in the test suite would
catch it.

Usage (stack must be running):
    python scripts/check_observability.py
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROMETHEUS = "http://localhost:9090"
GRAFANA = "http://localhost:3000"
GRAFANA_AUTH = base64.b64encode(b"admin:admin").decode()

# Host ports from .env.example. Kept here rather than parsed from .env so the check works
# on a fresh clone before anyone has customised ports.
SIM_PORTS = {
    "ml-api-adapter": 8011,
    "central-ledger": 8012,
    "quoting-service": 8013,
    "account-lookup-service": 8014,
    "transaction-request-service": 8015,
}

RECORDING_RULES = [
    "dpi:tps:rate5m",
    "dpi:request_rate:rate5m",
    "dpi:error_ratio:rate5m",
    "dpi:latency_p50:5m",
    "dpi:latency_p95:5m",
    "dpi:latency_p99:5m",
    "dpi:cpu:avg",
    "dpi:memory:max",
    "dpi:queue_depth:sum",
    "dpi:connections:sum",
    "dpi:replicas",
    "dpi:capacity_tps",
    "dpi:load_factor",
    "dpi:settlement_lag:max",
    "dpi:success_ratio",
    "dpi:platform_tps:rate5m",
    "dpi:platform_error_ratio:rate5m",
    "dpi:services_up",
]

# Rules that legitimately have no data yet, with the reason.
EXPECTED_EMPTY: dict[str, str] = {
    # Only account-lookup-service emits verification latency, and the histogram needs
    # traffic before a quantile exists.
    "dpi:verification_latency_p95:5m": "only one service emits it",
    "dpi:quote_latency_p95:5m": "only one service emits it",
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


def get_json(url: str, *, auth: str | None = None, timeout: float = 15.0) -> object:
    request = urllib.request.Request(url)  # noqa: S310 - fixed localhost URLs
    if auth:
        request.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def promql(expr: str) -> list[dict]:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    body = get_json(url)
    assert isinstance(body, dict)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "query failed"))
    return body["data"]["result"]


# ---------------------------------------------------------------------------
def check_targets() -> None:
    section("Prometheus targets")
    try:
        body = get_json(f"{PROMETHEUS}/api/v1/targets")
    except OSError as exc:
        fail(f"Prometheus unreachable at {PROMETHEUS} ({exc})")
        return

    assert isinstance(body, dict)
    targets = body["data"]["activeTargets"]
    mojaloop = [t for t in targets if t["labels"].get("job") == "mojaloop"]

    if not mojaloop:
        fail("no targets in the 'mojaloop' job - check infra/prometheus/prometheus.yml")
        return

    for target in sorted(mojaloop, key=lambda t: t["labels"].get("service", "")):
        service = target["labels"].get("service", target["labels"]["instance"])
        if target["health"] == "up":
            ok(f"scraping {service}")
        else:
            fail(
                f"{service} is {target['health']}: {target.get('lastError', 'no error given')}"
            )

    if len(mojaloop) != len(SIM_PORTS):
        fail(f"expected {len(SIM_PORTS)} Mojaloop targets, found {len(mojaloop)}")

    # The AIOps services are expected to be down until their modules exist. Prometheus
    # reporting that is correct behaviour, not a failure.
    aiops_down = [
        t for t in targets if t["labels"].get("job") == "aiops" and t["health"] != "up"
    ]
    if aiops_down:
        print(f"         ({len(aiops_down)} aiops targets down - expected, modules not built)")


def check_recording_rules() -> None:
    section("Recording rules")
    for rule in RECORDING_RULES:
        try:
            result = promql(rule)
        except (OSError, RuntimeError) as exc:
            fail(f"{rule}: {exc}")
            continue
        if result:
            ok(f"{rule} -> {len(result)} series")
        else:
            fail(f"{rule} returned no data")

    for rule, reason in EXPECTED_EMPTY.items():
        try:
            result = promql(rule)
        except (OSError, RuntimeError) as exc:
            warn(f"{rule}: {exc}")
            continue
        print(f"  [INFO] {rule} -> {len(result)} series ({reason})")


def check_exporter_matches_simulator() -> None:
    """The exporter's counter arithmetic must agree with the simulated traffic level.

    A 1-minute rate is used rather than 5, because a 5-minute window includes time before
    the containers started and would legitimately read low.
    """
    section("Exporter vs simulator")
    try:
        rates = {
            r["metric"].get("service"): float(r["value"][1])
            for r in promql("sum by (service) (rate(dpi_transaction_total[1m]))")
        }
    except (OSError, RuntimeError) as exc:
        fail(f"could not read transaction rate: {exc}")
        return

    for service, port in SIM_PORTS.items():
        try:
            snapshot = get_json(f"http://localhost:{port}/sim")
        except OSError as exc:
            fail(f"{service} /sim unreachable on port {port} ({exc})")
            continue

        assert isinstance(snapshot, dict)
        state = snapshot.get("state")
        if not state:
            fail(f"{service} has not produced a sample yet")
            continue

        reported = float(state["tps"])
        scraped = rates.get(service)
        if scraped is None:
            fail(f"{service} has no dpi_transaction_total series")
            continue

        # Generous tolerance: the simulator's value is instantaneous while the rate is a
        # 1-minute average over a curve that is still moving.
        if reported <= 0:
            warn(f"{service} reports zero traffic")
        elif abs(scraped - reported) / reported < 0.35:
            ok(f"{service}: sim {reported:.2f} tps ~ scraped {scraped:.2f} tps")
        else:
            fail(
                f"{service}: exporter disagrees with simulator "
                f"(sim {reported:.2f} vs scraped {scraped:.2f})"
            )


def check_grafana() -> None:
    section("Grafana")
    try:
        health = get_json(f"{GRAFANA}/api/health", auth=GRAFANA_AUTH)
    except OSError as exc:
        fail(f"Grafana unreachable at {GRAFANA} ({exc})")
        return
    assert isinstance(health, dict)
    ok(f"Grafana {health.get('version')} up, database {health.get('database')}")

    datasources = get_json(f"{GRAFANA}/api/datasources", auth=GRAFANA_AUTH)
    assert isinstance(datasources, list)
    uids = {d["uid"] for d in datasources}
    if "dpi-prometheus" in uids:
        ok("Prometheus datasource provisioned")
    else:
        fail(f"datasource 'dpi-prometheus' missing (found: {sorted(uids)})")

    try:
        get_json(f"{GRAFANA}/api/dashboards/uid/dpi-overview", auth=GRAFANA_AUTH)
        ok("dashboard 'dpi-overview' provisioned")
    except urllib.error.HTTPError:
        fail("dashboard 'dpi-overview' not provisioned")


def check_dashboard_panels() -> None:
    """Every panel must return data. A dashboard of 'No data' panels looks finished."""
    section("Dashboard panels return data")
    try:
        body = get_json(f"{GRAFANA}/api/dashboards/uid/dpi-overview", auth=GRAFANA_AUTH)
    except OSError:
        fail("could not load the dashboard to check its panels")
        return

    assert isinstance(body, dict)
    panels = [p for p in body["dashboard"]["panels"] if p.get("type") != "row"]
    if not panels:
        fail("dashboard has no panels")
        return

    for panel in panels:
        title = panel.get("title", "untitled")
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            # Substitute the template variable with a match-anything regex so the panel
            # query can be evaluated outside Grafana.
            resolved = re.sub(r"\$\{?service\}?", ".*", expr)
            try:
                result = promql(resolved)
            except (OSError, RuntimeError) as exc:
                fail(f"panel {title!r}: query error - {exc}")
                continue
            if result:
                ok(f"{title} -> {len(result)} series")
            else:
                fail(f"panel {title!r} would render 'No data'")


def main() -> int:
    print("Observability plane check (module 2)")
    check_targets()
    check_recording_rules()
    check_exporter_matches_simulator()
    check_grafana()
    check_dashboard_panels()

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

    print("=" * 46)
    print(" All observability checks passed. Module 2 OK.")
    print("=" * 46)
    return 0


if __name__ == "__main__":
    sys.exit(main())
