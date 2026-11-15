"""Verify the observability plane end to end.

Part A - metrics:

1. Prometheus is up and every Mojaloop target is being scraped.
2. Every recording rule in `infra/prometheus/rules/` actually evaluates to data.
3. The exporter agrees with the simulator - `rate(dpi_transaction_total[1m])` matches the
   TPS each service reports on `/sim`. A mismatch means the counter arithmetic is wrong,
   which would silently corrupt every metric downstream.
4. Grafana has the provisioned datasources and dashboard.
5. Every dashboard panel query returns at least one series, so no panel is silently blank.

Part B - logs, traces, alerting:

6. Loki is ready, and lines from all five simulated services have arrived with the
   `service` and `level` labels Alloy is supposed to promote.
7. The log pipeline is actually moving, measured from Alloy's own metrics rather than
   inferred from the presence of old data.
8. A request to the simulator produces a trace that Tempo can return **by id**, containing
   the expected span tree.
9. Tempo's metrics generator has remote-written span-derived series into Prometheus.
10. Alertmanager is up, Prometheus has it registered, and every alert rule loaded.

Points 5, 6 and 8 are the ones that repay the effort, and they are all the same kind of
check: a component that is *running* proves nothing. A dashboard whose panels all render
"No data", a Loki that is healthy but receiving nothing, and a Tempo with no spans in it all
look exactly like a working system from the outside.

Usage (stack must be running):
    python scripts/check_observability.py

Traces only exist if something has driven /simulate/request, so this script generates that
traffic itself rather than assuming someone else did.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROMETHEUS = "http://localhost:9090"
GRAFANA = "http://localhost:3000"
LOKI = "http://localhost:3100"
TEMPO = "http://localhost:3200"
ALERTMANAGER = "http://localhost:9093"
ALLOY = "http://localhost:12345"
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
    "dpi:connections:avg",
    "dpi:replicas",
    "dpi:capacity_tps",
    "dpi:load_factor",
    "dpi:settlement_lag:max",
    "dpi:success_ratio",
    "dpi:platform_tps:rate5m",
    "dpi:platform_error_ratio:rate5m",
    "dpi:services_up",
]

# Every alert rule defined in infra/prometheus/rules/dpi_alerts.yml. Listed here rather
# than derived from the file so that deleting a rule is caught as a regression instead of
# quietly agreeing with itself.
EXPECTED_ALERTS = [
    "DpiServiceDown",
    "DpiPlatformDegraded",
    "DpiHighErrorRatio",
    "DpiCriticalErrorRatio",
    "DpiLatencyP99High",
    "DpiSettlementLagHigh",
    "DpiMemoryPressure",
    "DpiCpuSaturated",
    "DpiOverCapacity",
    "DpiQueueBacklog",
    "AiopsLogPipelineStalled",
    "AiopsTraceIngestStalled",
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


def post_json(url: str, *, timeout: float = 30.0) -> object:
    """POST with no body. Used to drive /simulate/request so there is a trace to find."""
    request = urllib.request.Request(url, data=b"", method="POST")  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def wait_until_ready(name: str, url: str, *, timeout: float = 180.0) -> bool:
    """Poll a readiness endpoint until it returns 200, or give up.

    Loki and Tempo both serve traffic - and Prometheus scrapes them, and Alloy pushes to
    them successfully - for a couple of minutes before `/ready` turns 200. Loki holds it
    back until the ingester is ACTIVE in the ring; Tempo until the block list has been
    polled and the WAL replayed.

    Failing immediately made this script unusable straight after `stack.ps1 up`, and a
    check that fails because it was run too early trains people to ignore it. Waiting and
    reporting how long it took keeps the check meaningful: an unbounded startup is still a
    failure, it just is not this one.
    """
    deadline = time.time() + timeout
    started = time.time()
    last_error = "no response"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
                if response.status == 200:
                    waited = time.time() - started
                    suffix = f" after {waited:.0f}s" if waited > 2 else ""
                    ok(f"{name} ready{suffix}")
                    return True
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(5)

    fail(f"{name} not ready within {timeout:.0f}s (last: {last_error}) at {url}")
    return False


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

    # The observability plane itself. This carries more weight than it looks: Loki, Tempo
    # and Alloy ship distroless or minimal images with no shell or HTTP client, so they
    # have no Docker healthcheck. These scrapes are their health signal, and the
    # AiopsLogPipelineStalled / AiopsTraceIngestStalled rules are built on these series.
    plane = [t for t in targets if t["labels"].get("job") in {"observability", "alloy"}]
    if not plane:
        fail("no targets in the 'observability' or 'alloy' jobs")
        return
    for target in sorted(plane, key=lambda t: t["labels"].get("component", "")):
        component = target["labels"].get("component", target["labels"]["instance"])
        if target["health"] == "up":
            ok(f"scraping {component}")
        else:
            fail(
                f"{component} is {target['health']}: "
                f"{target.get('lastError', 'no error given')}"
            )


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


# ===========================================================================
# Part B - logs, traces, alerting
# ===========================================================================
def check_loki() -> None:
    """Loki has logs from every simulated service, with the labels Alloy promotes.

    Checking label *values* rather than just "Loki is up" matters: a broken `stage.labels`
    block in the Alloy config still ships every line, so the data is all there and every
    query in the Grafana dashboards and in module 7 returns nothing.
    """
    section("Loki - logs")
    if not wait_until_ready("Loki", f"{LOKI}/ready"):
        return

    try:
        labels = get_json(f"{LOKI}/loki/api/v1/labels")
    except OSError as exc:
        fail(f"could not list Loki labels ({exc})")
        return
    assert isinstance(labels, dict)
    names = set(labels.get("data") or [])

    for expected in ("service", "level", "container", "service_group", "platform"):
        if expected in names:
            ok(f"label '{expected}' present")
        else:
            fail(f"label '{expected}' missing - check discovery.relabel in config.alloy")

    # Every simulated service must have shipped at least one line.
    try:
        values = get_json(f"{LOKI}/loki/api/v1/label/service/values")
    except OSError as exc:
        fail(f"could not list `service` label values ({exc})")
        return
    assert isinstance(values, dict)
    seen = set(values.get("data") or [])
    for service in SIM_PORTS:
        if service in seen:
            ok(f"logs from {service}")
        else:
            fail(f"no logs from {service} in Loki")

    # And the lines must still be JSON-parseable at query time, which is what module 7
    # will rely on to read numeric fields off a log line.
    end = int(time.time() * 1_000_000_000)
    start = end - 10 * 60 * 1_000_000_000
    query = urllib.parse.urlencode(
        {
            "query": '{service="central-ledger"} | json | event != ""',
            "start": start,
            "end": end,
            "limit": 5,
        }
    )
    try:
        body = get_json(f"{LOKI}/loki/api/v1/query_range?{query}")
    except OSError as exc:
        fail(f"Loki query failed ({exc})")
        return
    assert isinstance(body, dict)
    streams = body["data"]["result"]
    if streams:
        ok(f"JSON pipeline works - {len(streams)} stream(s) with a parsed `event` field")
    else:
        fail(
            "no central-ledger lines parsed as JSON in the last 10 minutes "
            "(stage.json / stage.match in config.alloy, or AIOPS_LOG_FORMAT not json)"
        )


def check_log_pipeline_moving() -> None:
    """Measure that logs are flowing *now*, not that old logs exist.

    Alloy is the one component whose failure is invisible: if it stops, Loki stays healthy
    and full of yesterday's data, every dashboard still renders, and nothing reports a
    problem. This reads Alloy's own send counter, which is the same series the
    AiopsLogPipelineStalled alert is built on.
    """
    section("Alloy - log pipeline")
    if not wait_until_ready("Alloy", f"{ALLOY}/-/ready", timeout=60):
        return

    try:
        sent = promql("sum(loki_write_sent_entries_total)")
    except (OSError, RuntimeError) as exc:
        fail(f"could not read Alloy's send counter from Prometheus ({exc})")
        return

    if not sent:
        fail(
            "loki_write_sent_entries_total absent - Prometheus is not scraping the "
            "`alloy` job, or Alloy has never sent a batch"
        )
        return

    total = float(sent[0]["value"][1])
    if total <= 0:
        fail("Alloy has sent 0 log entries to Loki")
        return
    ok(f"Alloy has sent {int(total)} entries to Loki")

    # Two samples a few seconds apart: a non-zero total only proves it worked once.
    time.sleep(6)
    try:
        again = float(promql("sum(loki_write_sent_entries_total)")[0]["value"][1])
    except (OSError, RuntimeError, IndexError) as exc:
        warn(f"could not re-read the send counter ({exc})")
        return
    if again > total:
        ok(f"still shipping (+{int(again - total)} entries in ~6s)")
    else:
        # Not a hard failure: Prometheus scrapes every 15s, so two reads 6s apart can
        # legitimately return the same scrape.
        warn(
            "send counter did not advance within 6s - expected when both reads hit the "
            "same 15s scrape, suspicious if it persists"
        )


def check_tempo() -> None:
    """Drive a real request, then fetch its trace back from Tempo by id.

    Looking the trace up by the id the service itself reported is what makes this a
    verification rather than a vibe check: searching for "any recent trace" would pass on
    a trace left over from a previous run, and would pass while this request's spans were
    dropped.
    """
    section("Tempo - traces")
    if not wait_until_ready("Tempo", f"{TEMPO}/ready"):
        return

    port = SIM_PORTS["central-ledger"]
    trace_id = None
    # The endpoint fails on purpose at the simulated error ratio, and under a fault that
    # can be most requests. A few attempts makes the check robust without hiding a real
    # failure to trace.
    for _ in range(5):
        try:
            body = post_json(f"http://localhost:{port}/simulate/request?endpoint=/transfers")
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                continue  # simulated downstream failure, try again
            fail(f"/simulate/request returned HTTP {exc.code}")
            return
        except OSError as exc:
            fail(f"could not reach central-ledger on port {port} ({exc})")
            return
        assert isinstance(body, dict)
        trace_id = body.get("trace_id")
        if trace_id:
            break

    if not trace_id:
        fail(
            "/simulate/request never returned a trace_id - tracing is not configured on "
            "the simulator (check configure_tracing in mojaloop_sim/asgi.py)"
        )
        return
    ok(f"request traced as {trace_id}")

    # Spans are batched by the SDK and buffered by Tempo's ingester before they are
    # queryable. Poll rather than sleep a fixed amount, and report how long it took - a
    # sudden increase is the signal that ingester.complete_block_timeout drifted.
    deadline = time.time() + 45
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            trace = get_json(f"{TEMPO}/api/traces/{trace_id}")
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                fail(f"Tempo returned HTTP {exc.code} for trace {trace_id}")
                return
            time.sleep(3)
        except OSError as exc:
            fail(f"Tempo trace lookup failed ({exc})")
            return
    else:
        fail(f"trace {trace_id} never became queryable in Tempo within 45s")
        return

    ok(f"trace retrieved from Tempo after {attempt} attempt(s)")

    # The span tree must have the shape routes.py builds, otherwise the trace is present
    # but useless for locating *where* a request was slow.
    assert isinstance(trace, dict)
    span_names: set[str] = set()
    resource_services: set[str] = set()
    for batch in trace.get("batches", []):
        for attribute in batch.get("resource", {}).get("attributes", []):
            if attribute.get("key") == "service.name":
                value = attribute.get("value", {}).get("stringValue")
                if value:
                    resource_services.add(value)
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                span_names.add(span.get("name", ""))

    for expected in (
        "mojaloop.handle_request",
        "mojaloop.validate_participants",
        "mojaloop.commit_transfer",
    ):
        if expected in span_names:
            ok(f"span '{expected}' present")
        else:
            fail(f"span '{expected}' missing from the trace (found: {sorted(span_names)})")

    if "central-ledger" in resource_services:
        ok("resource attribute service.name = central-ledger")
    else:
        fail(
            "service.name is not 'central-ledger' on the trace resource "
            f"(found: {sorted(resource_services) or 'none'}) - Tempo's generated metrics "
            "will not join to the dpi_* series"
        )


def check_span_metrics() -> None:
    """Tempo's metrics generator must have remote-written span-derived series.

    This is what justifies running Tempo at all rather than treating traces as a debugging
    nicety: module 7 correlates anomalies topologically using MOJALOOP_DEPENDENCIES, a
    hand-maintained constant, and these series are the same topology measured.
    """
    section("Tempo metrics generator -> Prometheus")

    # The generator collects its registry on an interval and then remote-writes, and
    # Prometheus only exposes the result after ingesting it. Spans were created seconds ago
    # by check_tempo, so poll rather than assume the pipeline has already caught up.
    series: list[dict] = []
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            series = promql('{__name__=~"traces_.+"}')
        except (OSError, RuntimeError) as exc:
            fail(f"could not query for span-derived series ({exc})")
            return
        if series:
            break
        time.sleep(5)

    if not series:
        fail(
            "no traces_* series in Prometheus. Either the metrics_generator processors "
            "are not enabled in infra/tempo/tempo.yml, or Prometheus was started without "
            "--web.enable-remote-write-receiver (Tempo logs the rejected writes)"
        )
        return

    names = sorted({s["metric"]["__name__"] for s in series})
    ok(f"{len(series)} span-derived series across {len(names)} metric name(s)")
    for name in names:
        print(f"         {name}")

    if any(name.startswith("traces_service_graph") for name in names):
        ok("service graph metrics present")
        # Worth being precise about what the graph currently contains. Tempo records an
        # edge for every client-server span pair, and a request arriving from outside the
        # instrumented system is attributed to a synthetic `user` client - so the edges
        # today are all `user -> <service>`, not service-to-service. The declared topology
        # in MOJALOOP_DEPENDENCIES cannot be checked against this until module 9 makes the
        # simulator fan out to its peers.
        try:
            edges = promql("traces_service_graph_request_total")
        except (OSError, RuntimeError):
            return
        internal = [e for e in edges if e["metric"].get("client", "user") not in {"user", ""}]
        if internal:
            ok(f"{len(internal)} service-to-service edge(s) observed")
        else:
            print(
                f"  [INFO] all {len(edges)} edge(s) are `user -> service`. Service-to-service "
                "edges appear once the simulator calls its peers (module 9)."
            )
    else:
        fail("no traces_service_graph_* series - the service-graphs processor is not running")


def check_alerting() -> None:
    """Alertmanager is reachable, wired to Prometheus, and every rule loaded."""
    section("Alerting")
    try:
        status = get_json(f"{ALERTMANAGER}/api/v2/status")
    except OSError as exc:
        fail(f"Alertmanager unreachable at {ALERTMANAGER} ({exc})")
        return
    assert isinstance(status, dict)
    ok(f"Alertmanager {status.get('versionInfo', {}).get('version', '?')} up")

    # Prometheus must know about it. A typo in the `alerting` block leaves both components
    # healthy and no alert ever delivered.
    try:
        discovered = get_json(f"{PROMETHEUS}/api/v1/alertmanagers")
    except OSError as exc:
        fail(f"could not read Prometheus's alertmanager list ({exc})")
        return
    assert isinstance(discovered, dict)
    active = discovered["data"]["activeAlertmanagers"]
    if active:
        ok(f"Prometheus has {len(active)} active alertmanager: {active[0]['url']}")
    else:
        fail("Prometheus has no active alertmanager - check the `alerting` block")

    # Every alert rule must be loaded. A YAML error in one group makes Prometheus skip
    # that file, and the only symptom is alerts that never fire.
    try:
        body = get_json(f"{PROMETHEUS}/api/v1/rules?type=alert")
    except OSError as exc:
        fail(f"could not read Prometheus rules ({exc})")
        return
    assert isinstance(body, dict)
    loaded = {
        rule["name"]
        for group in body["data"]["groups"]
        for rule in group["rules"]
        if rule.get("type") == "alerting"
    }
    for name in EXPECTED_ALERTS:
        if name in loaded:
            ok(f"alert rule {name}")
        else:
            fail(f"alert rule {name} not loaded")

    unexpected = sorted(loaded - set(EXPECTED_ALERTS))
    if unexpected:
        print(f"         (also loaded, not in this script's list: {unexpected})")

    firing = [
        rule["name"]
        for group in body["data"]["groups"]
        for rule in group["rules"]
        if rule.get("type") == "alerting" and rule.get("state") == "firing"
    ]
    if firing:
        # Not a failure. Under normal simulated load nothing should fire, but a fault left
        # injected from a previous check makes this expected.
        print(f"  [INFO] currently firing: {sorted(firing)}")


def main() -> int:
    print("Observability plane check (module 2)")
    check_targets()
    check_recording_rules()
    check_exporter_matches_simulator()
    check_grafana()
    check_dashboard_panels()
    check_loki()
    check_log_pipeline_moving()
    check_tempo()
    check_span_metrics()
    check_alerting()

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

    print("=" * 52)
    print(" All observability checks passed. Module 2 A+B OK.")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    sys.exit(main())
