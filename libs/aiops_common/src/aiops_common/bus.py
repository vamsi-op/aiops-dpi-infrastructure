"""Redis Streams event bus.

Why streams rather than pub/sub: streams persist, support consumer groups, and let a
service that was restarted pick up where it left off. In an AIOps platform, silently
dropping an anomaly because a consumer happened to be down is not acceptable.

Envelope format written to Redis (all values are strings, as streams require):
    event_id      unique id of this message
    type          logical event type, e.g. "anomaly"
    source        publishing service name
    published_at  ISO-8601 UTC timestamp
    data          JSON payload

Typical producer:
    bus = EventBus(source="anomaly-detector")
    await bus.connect()
    await bus.publish(Stream.ANOMALIES, anomaly, event_type="anomaly")

Typical consumer:
    async for event in bus.subscribe(Stream.ANOMALIES, group="rca", consumer="rca-1"):
        anomaly = AnomalyEvent.model_validate(event.data)
        ...                      # bus auto-acks after the loop body completes
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Self, cast

import orjson
import redis.asyncio as redis
from pydantic import BaseModel
from redis.exceptions import ResponseError

from .config import RedisSettings, get_settings
from .constants import Stream
from .errors import EventBusError
from .log import get_logger
from .schemas import new_id, utc_now

log = get_logger(__name__)


class Event(BaseModel):
    """A message read off the bus, envelope plus decoded payload."""

    event_id: str
    stream: str
    message_id: str
    type: str
    source: str
    published_at: datetime
    data: dict[str, Any]


def _dumps(value: Any) -> str:
    return orjson.dumps(value, default=str).decode()


# redis-py types the XADD field mapping invariantly, so the dict we build has to be
# declared with exactly the union it expects rather than the `dict[str, str]` we use.
_FieldValue = bytes | memoryview | str | int | float

# Same story for the XREAD cursor map: redis-py types it invariantly, so our dict has
# to be declared with exactly the unions it expects, not the `dict[str, str]` we build.
_StreamKey = bytes | str | memoryview
_StreamId = int | bytes | str | memoryview


class EventBus:
    """Async Redis Streams client. One instance per service is plenty."""

    def __init__(
        self,
        source: str | None = None,
        *,
        settings: RedisSettings | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        root = get_settings()
        self._settings = settings or root.redis
        self._source = source or root.service_name
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> Self:
        """Create the connection pool and verify Redis is reachable."""
        if self._client is None:
            self._client = redis.Redis(
                host=self._settings.host,
                port=self._settings.port,
                db=self._settings.db,
                password=self._settings.password,
                socket_timeout=self._settings.socket_timeout_seconds,
                socket_connect_timeout=self._settings.socket_timeout_seconds,
                decode_responses=True,
                health_check_interval=30,
            )
        await self.ping()
        return self

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise EventBusError("EventBus used before connect(); call `await bus.connect()`")
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception as exc:
            raise EventBusError("Redis ping failed", host=self._settings.host) from exc

    # -- producing ---------------------------------------------------------
    async def publish(
        self,
        stream: Stream | str,
        payload: BaseModel | dict[str, Any],
        *,
        event_type: str | None = None,
        maxlen: int | None = None,
    ) -> str:
        """Append one event to `stream`; returns the Redis message id."""
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        event_id = new_id("evt")
        fields: dict[_FieldValue, _FieldValue] = {
            "event_id": event_id,
            "type": event_type or type(payload).__name__,
            "source": self._source,
            "published_at": utc_now().isoformat(),
            "data": _dumps(data),
        }
        try:
            message_id = await self.client.xadd(
                name=str(stream),
                fields=fields,
                maxlen=maxlen or self._settings.stream_maxlen,
                approximate=True,
            )
        except Exception as exc:
            raise EventBusError("Failed to publish event", stream=str(stream)) from exc

        log.debug(
            "event.published", stream=str(stream), event_id=event_id, message_id=message_id
        )
        return str(message_id)

    # -- consuming ---------------------------------------------------------
    async def ensure_group(self, stream: Stream | str, group: str) -> None:
        """Create the consumer group (and the stream) if they do not exist yet."""
        try:
            await self.client.xgroup_create(
                name=str(stream), groupname=group, id="0", mkstream=True
            )
            log.info("bus.group_created", stream=str(stream), group=group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise EventBusError(
                    "Failed to create consumer group", stream=str(stream), group=group
                ) from exc

    async def read(
        self,
        stream: Stream | str,
        group: str,
        consumer: str,
        *,
        count: int = 32,
        block_ms: int = 5_000,
    ) -> list[Event]:
        """Read up to `count` new events, blocking up to `block_ms` if the stream is idle."""
        try:
            response = await self.client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={str(stream): ">"},
                count=count,
                block=block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.ensure_group(stream, group)
                return []
            raise EventBusError("Failed to read stream", stream=str(stream)) from exc
        except Exception as exc:
            raise EventBusError("Failed to read stream", stream=str(stream)) from exc

        events: list[Event] = []
        for stream_name, messages in response or []:
            for message_id, fields in messages:
                event = self._decode(stream_name, message_id, fields)
                if event is not None:
                    events.append(event)
        return events

    async def ack(self, stream: Stream | str, group: str, *message_ids: str) -> None:
        if not message_ids:
            return
        await self.client.xack(str(stream), group, *message_ids)

    async def subscribe(
        self,
        stream: Stream | str,
        group: str,
        consumer: str,
        *,
        count: int = 32,
        block_ms: int = 5_000,
        auto_ack: bool = True,
    ) -> AsyncIterator[Event]:
        """Yield events forever, acknowledging each one after the loop body returns.

        If the loop body raises, the message stays pending and can be reclaimed later,
        which is exactly what we want for an at-least-once pipeline.
        """
        await self.ensure_group(stream, group)
        while True:
            for event in await self.read(
                stream, group, consumer, count=count, block_ms=block_ms
            ):
                yield event
                if auto_ack:
                    await self.ack(stream, group, event.message_id)

    async def tail(
        self,
        streams: Sequence[Stream | str],
        *,
        last_ids: dict[str, str] | None = None,
        count: int = 64,
        block_ms: int = 5_000,
    ) -> tuple[list[Event], dict[str, str]]:
        """Read new events across several streams without joining a consumer group.

        This is the right primitive for a live UI tail. Consumer groups would be wrong
        here for two reasons: they split messages between instances (the dashboard needs
        every gateway to see everything), and acking would interfere with the real
        pipeline consumers. Nothing is acknowledged and no state is left in Redis.

        Returns the events plus an updated cursor to pass back on the next call. A
        `None` cursor starts at `$`, meaning "only messages published from now on" -
        history belongs to the database, not the live feed.

        One call per invocation rather than an endless generator, so the caller keeps
        control of reconnection and backoff when Redis blips.
        """
        cursor: dict[str, str] = (
            dict(last_ids) if last_ids else await self._resolve_tail_start(streams)
        )
        # redis-py types the cursor mapping invariantly, so it must be declared with
        # exactly the unions it expects rather than the dict[str, str] we work with.
        request = cast("dict[_StreamKey, _StreamId]", cursor)
        try:
            response = await self.client.xread(streams=request, count=count, block=block_ms)
        except Exception as exc:
            raise EventBusError("Failed to tail streams", streams=list(cursor)) from exc

        events: list[Event] = []
        for stream_name, messages in response or []:
            for message_id, fields in messages:
                cursor[str(stream_name)] = str(message_id)
                event = self._decode(str(stream_name), message_id, fields)
                if event is not None:
                    events.append(event)
        return events, cursor

    async def _resolve_tail_start(self, streams: Sequence[Stream | str]) -> dict[str, str]:
        """Turn "start from now" into concrete message ids, one per stream.

        This exists because of a subtle and expensive XREAD behaviour. `$` is not a
        fixed position - Redis re-evaluates it as "the stream's current end" on *every*
        call. XREAD also returns as soon as *any* one stream has data, so a stream that
        has not produced yet keeps its `$` sentinel. Anything published to that stream
        between two XREAD calls then falls into the gap and is silently lost.

        Resolving every cursor to a real id once, before the first read, removes the
        gap entirely: from then on we only ever advance from concrete positions.
        """
        names = [str(stream) for stream in streams]
        pipe = self.client.pipeline()
        for name in names:
            # Last entry currently in the stream, or nothing if it is empty/absent.
            pipe.xrevrange(name, "+", "-", count=1)
        results = await pipe.execute()

        cursor: dict[str, str] = {}
        for name, entries in zip(names, results, strict=True):
            # "0-0" is correct for an empty stream: there is nothing to replay, and it
            # guarantees the very first message published is delivered.
            cursor[name] = str(entries[0][0]) if entries else "0-0"
        return cursor

    async def reclaim_stale(
        self,
        stream: Stream | str,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int = 60_000,
        count: int = 32,
    ) -> list[Event]:
        """Take over messages another consumer read but never acked (e.g. it crashed)."""
        try:
            _next_id, messages, _deleted = await self.client.xautoclaim(
                name=str(stream),
                groupname=group,
                consumername=consumer,
                min_idle_time=min_idle_ms,
                count=count,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.ensure_group(stream, group)
                return []
            raise EventBusError("Failed to reclaim messages", stream=str(stream)) from exc

        events = [
            event
            for message_id, fields in messages
            if (event := self._decode(str(stream), message_id, fields)) is not None
        ]
        if events:
            log.warning("bus.reclaimed_stale", stream=str(stream), count=len(events))
        return events

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _decode(stream_name: str, message_id: str, fields: dict[str, str]) -> Event | None:
        """Turn raw stream fields into an `Event`; poison messages are logged and skipped."""
        try:
            return Event(
                event_id=fields.get("event_id", message_id),
                stream=stream_name,
                message_id=message_id,
                type=fields.get("type", "unknown"),
                source=fields.get("source", "unknown"),
                published_at=datetime.fromisoformat(
                    fields.get("published_at") or utc_now().isoformat()
                ),
                data=orjson.loads(fields.get("data") or "{}"),
            )
        except Exception:
            log.exception("bus.decode_failed", stream=stream_name, message_id=message_id)
            return None

    # -- small key/value helpers used by the healing engine -----------------
    async def set_flag(
        self, key: str, value: str = "1", *, ttl_seconds: int | None = None
    ) -> None:
        await self.client.set(key, value, ex=ttl_seconds)

    async def get_flag(self, key: str) -> str | None:
        value = await self.client.get(key)
        return str(value) if value is not None else None

    async def delete_flag(self, key: str) -> None:
        await self.client.delete(key)

    async def incr_with_ttl(self, key: str, *, ttl_seconds: int) -> int:
        """Atomically increment a counter that expires — used for rate budgets."""
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        result = await pipe.execute()
        return int(result[0])
