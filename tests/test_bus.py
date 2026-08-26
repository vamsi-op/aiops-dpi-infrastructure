"""Event bus envelope handling (no Redis required)."""

from __future__ import annotations

import orjson
import pytest

from aiops_common.bus import EventBus
from aiops_common.constants import Stream
from aiops_common.errors import EventBusError
from aiops_common.schemas import AnomalyEvent, utc_now


def _fields(payload: dict[str, object]) -> dict[str, str]:
    return {
        "event_id": "evt_1234",
        "type": "anomaly",
        "source": "anomaly-detector",
        "published_at": utc_now().isoformat(),
        "data": orjson.dumps(payload).decode(),
    }


def test_decode_round_trips_a_payload() -> None:
    anomaly = AnomalyEvent(service="central-ledger", score=0.91)
    event = EventBus._decode(
        Stream.ANOMALIES, "1724500000000-0", _fields(anomaly.model_dump(mode="json"))
    )

    assert event is not None
    assert event.type == "anomaly"
    assert event.source == "anomaly-detector"
    assert event.message_id == "1724500000000-0"
    assert AnomalyEvent.model_validate(event.data).score == 0.91


def test_decode_returns_none_for_malformed_json() -> None:
    # A poison message must never kill a consumer loop — it is logged and skipped.
    broken = _fields({})
    broken["data"] = "{not json"
    assert EventBus._decode(Stream.ANOMALIES, "1-0", broken) is None


def test_decode_tolerates_missing_envelope_fields() -> None:
    event = EventBus._decode(Stream.METRICS, "7-0", {"data": "{}"})
    assert event is not None
    assert event.event_id == "7-0"  # falls back to the Redis message id
    assert event.type == "unknown"
    assert event.data == {}


def test_bus_requires_connect_before_use() -> None:
    # Failing loudly beats a confusing AttributeError deep inside a publish call.
    bus = EventBus(source="test")
    with pytest.raises(EventBusError, match="connect"):
        _ = bus.client
