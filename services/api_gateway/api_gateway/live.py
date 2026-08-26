"""Live event fan-out: Redis Streams to browser WebSockets.

One background task tails the bus and broadcasts to every connected client. The
alternative, a consumer per socket, would multiply Redis load by the number of open
browser tabs for no benefit.

Design notes:

* Tailing uses `XREAD` from `$`, not a consumer group. A dashboard is an observer; it
  must never acknowledge messages and steal them from the real pipeline consumers.
* Broadcasting is best-effort. A slow or dead socket is dropped rather than allowed to
  block the loop, because the UI must never apply back-pressure to the platform.
* Clients receive a `hello` frame on connect describing which streams are live, so the
  UI can distinguish "quiet" from "not wired up yet".
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import orjson
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from aiops_common.bus import EventBus
from aiops_common.constants import Stream
from aiops_common.errors import EventBusError
from aiops_common.log import get_logger
from aiops_common.schemas import utc_now

log = get_logger(__name__)

# Streams the dashboard listens to, mapped to the message `type` the UI switches on.
WATCHED_STREAMS: dict[str, str] = {
    Stream.METRICS: "metrics",
    Stream.ANOMALIES: "anomaly",
    Stream.PREDICTIONS: "prediction",
    Stream.INCIDENTS: "incident",
    Stream.ACTIONS: "action",
    Stream.DEPLOYMENTS: "deployment",
    Stream.CHAOS: "chaos",
}

# Metric events are high volume and only drive sparkline nudges, so they are coalesced
# rather than forwarded one by one.
_HIGH_VOLUME = {"metrics"}

# Metric samples for the same scrape cycle are published back to back, and the relay
# reads them faster than the agent writes them. Without a flush window each broadcast
# would carry a single service, defeating the point of coalescing. One second is short
# enough to feel live and long enough to gather a whole cycle.
METRICS_FLUSH_SECONDS = 1.0
_TAIL_BLOCK_MS = 1_000


def _encode(payload: dict[str, Any]) -> str:
    return orjson.dumps(payload, default=str).decode()


class ConnectionManager:
    """Tracks open sockets and broadcasts frames to them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        log.info("live.client_connected", clients=self.client_count)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        log.info("live.client_disconnected", clients=self.client_count)

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        if websocket.client_state is not WebSocketState.CONNECTED:
            return
        with contextlib.suppress(Exception):
            await websocket.send_text(_encode(message))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send to all clients, dropping any that fail."""
        async with self._lock:
            targets = list(self._clients)
        if not targets:
            return

        frame = _encode(message)
        dead: list[WebSocket] = []
        for client in targets:
            if client.client_state is not WebSocketState.CONNECTED:
                dead.append(client)
                continue
            try:
                await client.send_text(frame)
            except Exception:  # noqa: BLE001 - a broken socket must not stop the loop
                dead.append(client)

        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
            log.debug("live.pruned_clients", removed=len(dead), remaining=self.client_count)


class EventRelay:
    """Background task tailing the event bus and broadcasting to WebSocket clients."""

    def __init__(self, bus: EventBus, manager: ConnectionManager) -> None:
        self._bus = bus
        self._manager = manager
        self._task: asyncio.Task[None] | None = None
        self._cursor: dict[str, str] = {}
        self._stopping = asyncio.Event()
        # Newest metric sample per service, awaiting the next flush.
        self._pending_metrics: dict[str, dict[str, Any]] = {}
        self._last_flush = 0.0
        # Populated as events arrive, so /api/overview can report which parts of the
        # pipeline are actually producing.
        self.seen_types: set[str] = set()
        self.last_event_at: Any = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            # Start the flush clock now, so the very first scrape cycle is batched
            # rather than emitting one frame per service.
            self._last_flush = time.monotonic()
            self._task = asyncio.create_task(self._run(), name="event-relay")
            log.info("live.relay_started", streams=len(WATCHED_STREAMS))

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            log.info("live.relay_stopped")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                events, self._cursor = await self._bus.tail(
                    list(WATCHED_STREAMS),
                    last_ids=self._cursor or None,
                    block_ms=_TAIL_BLOCK_MS,
                )
                backoff = 1.0
                if events:
                    await self._dispatch(events)
                await self._flush_metrics()
            except asyncio.CancelledError:
                raise
            except EventBusError as exc:
                # Redis restarted or is briefly unreachable. Keep the API serving and
                # retry with a capped backoff rather than killing the relay.
                log.warning("live.relay_bus_error", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                log.exception("live.relay_unexpected_error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, events: list[Any]) -> None:
        for event in events:
            message_type = WATCHED_STREAMS.get(event.stream, "unknown")
            self.seen_types.add(message_type)
            self.last_event_at = event.published_at

            if message_type in _HIGH_VOLUME:
                # Buffer, keeping only the newest sample per service. Flushed on a timer
                # so one frame carries a whole scrape cycle.
                self._pending_metrics[str(event.data.get("service", "unknown"))] = event.data
                continue

            # Everything else is low volume and operationally significant, so it goes
            # out immediately.
            await self._manager.broadcast(
                {
                    "type": message_type,
                    "event_id": event.event_id,
                    "source": event.source,
                    "published_at": event.published_at,
                    "data": event.data,
                }
            )

    async def _flush_metrics(self) -> None:
        if not self._pending_metrics:
            return
        now = time.monotonic()
        if now - self._last_flush < METRICS_FLUSH_SECONDS:
            return

        samples = list(self._pending_metrics.values())
        self._pending_metrics.clear()
        self._last_flush = now
        await self._manager.broadcast(
            {"type": "metrics", "published_at": utc_now(), "data": {"samples": samples}}
        )
