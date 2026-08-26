"""End-to-end proof of the live path: Redis Streams -> API gateway -> WebSocket.

Connects to the gateway's /ws, publishes one anomaly and one incident onto the event
bus, and asserts both arrive at the browser-facing socket. This is the only way to
verify the relay actually works; the HTTP endpoints all read from the database and
would pass even if fan-out were broken.

Usage (gateway must be running):
    python scripts/ws_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

from aiops_common.bus import EventBus
from aiops_common.constants import Severity, Stream
from aiops_common.log import configure_logging, get_logger
from aiops_common.schemas import AnomalyEvent, Incident, RootCauseHypothesis

WS_URL = "ws://127.0.0.1:8000/ws"
TIMEOUT_SECONDS = 15.0


async def main() -> int:
    configure_logging("ws-smoke")
    log = get_logger("ws-smoke")

    received: list[dict[str, object]] = []

    async with websockets.connect(WS_URL, open_timeout=10) as socket:
        log.info("ws.connected", url=WS_URL)

        # The gateway sends a snapshot/hello frame on connect; drain whatever is
        # already queued so it is not mistaken for a relayed event.
        try:
            while True:
                hello = await asyncio.wait_for(socket.recv(), timeout=2.0)
                log.info("ws.initial_frame", type=json.loads(hello).get("type"))
        except TimeoutError:
            pass

        # The relay starts at "$", so it only sees messages published after it began
        # reading. Publishing now is therefore the correct order.
        bus = EventBus(source="ws-smoke")
        await bus.connect()

        anomaly = AnomalyEvent(
            service="central-ledger",
            score=0.94,
            severity=Severity.CRITICAL,
            contributing_features=["memory_utilisation_ratio", "queue_depth"],
            description="ws smoke test",
        )
        incident = Incident(
            title="ws smoke test incident",
            severity=Severity.CRITICAL,
            affected_services=["central-ledger"],
            anomaly_ids=[anomaly.anomaly_id],
            hypotheses=[
                RootCauseHypothesis(
                    category="service",
                    target="central-ledger",
                    confidence=0.91,
                    summary="ws smoke test hypothesis",
                    evidence=["published by scripts/ws_smoke.py"],
                )
            ],
        )

        await bus.publish(Stream.ANOMALIES, anomaly, event_type="anomaly")
        await bus.publish(Stream.INCIDENTS, incident, event_type="incident")
        log.info("bus.published", anomaly_id=anomaly.anomaly_id)

        try:
            while len(received) < 2:
                raw = await asyncio.wait_for(socket.recv(), timeout=TIMEOUT_SECONDS)
                frame = json.loads(raw)
                log.info("ws.frame", type=frame.get("type"))
                received.append(frame)
        except TimeoutError:
            log.error("ws.timeout", got=len(received), expected=2)

        await bus.close()

    types = [frame.get("type") for frame in received]
    ok = "anomaly" in types and "incident" in types
    print(f"\nframes received: {types}")
    print("RESULT:", "PASS - live relay works end to end" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
