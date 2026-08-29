r"""API gateway: read APIs and live event fan-out for the operations dashboard.

Run locally:
    uvicorn api_gateway.asgi:app --reload --port 8000 --app-dir services/api_gateway

Or through the stack:
    .\scripts\stack.ps1 up -Layer aiops
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from aiops_common.bus import EventBus
from aiops_common.config import get_settings
from aiops_common.constants import AiopsService
from aiops_common.db import Database
from aiops_common.log import get_logger
from aiops_common.schemas import utc_now
from aiops_common.service import build_app

from .live import WATCHED_STREAMS, ConnectionManager, EventRelay
from .repository import DashboardRepository
from .routes import router

SERVICE_NAME = str(AiopsService.API_GATEWAY)

log = get_logger(SERVICE_NAME)
settings = get_settings()

database = Database()
bus = EventBus(source=SERVICE_NAME)
connections = ConnectionManager()
relay = EventRelay(bus, connections)


async def _startup() -> None:
    await database.connect()
    await bus.connect()
    relay.start()


async def _shutdown() -> None:
    await relay.stop()
    await bus.close()
    await database.close()


app: FastAPI = build_app(
    SERVICE_NAME,
    description=(
        "Read APIs and WebSocket fan-out powering the AIOps operations dashboard. "
        "Aggregates metrics, anomalies, forecasts, incidents, remediation actions and "
        "chaos experiments for DPI infrastructure."
    ),
    readiness_checks={"timescaledb": database.ping, "redis": bus.ping},
    on_startup=[_startup],
    on_shutdown=[_shutdown],
    # The dashboard dev server runs on a different origin, so it needs CORS. Tighten
    # this to the real dashboard origin before exposing the gateway anywhere shared.
    cors_origins=("*",),
)

app.state.repository = DashboardRepository(database)
app.state.connections = connections
app.state.relay = relay
app.include_router(router)


@app.websocket("/ws")
async def live_updates(websocket: WebSocket) -> None:
    """Push anomalies, incidents, actions and metric nudges as they happen.

    The client does not send anything meaningful; reads exist only to detect
    disconnects and to answer ping frames.
    """
    await connections.connect(websocket)
    try:
        await connections.send(
            websocket,
            {
                "type": "hello",
                "published_at": utc_now(),
                "data": {
                    "service": SERVICE_NAME,
                    "platform": settings.dpi_platform,
                    "streams": list(WATCHED_STREAMS.values()),
                    "clients": connections.client_count,
                },
            },
        )
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await connections.send(websocket, {"type": "pong", "published_at": utc_now()})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - one bad socket must not surface as a 500
        log.debug("live.socket_error", exc_info=True)
    finally:
        await connections.disconnect(websocket)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "platform": settings.dpi_platform,
        "docs": "/docs",
        "overview": "/api/overview",
        "websocket": "/ws",
    }


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        uvicorn.run(
            "api_gateway.asgi:app",
            host="0.0.0.0",  # noqa: S104 - container needs to bind all interfaces
            port=8000,
            log_config=None,  # structlog already owns logging
        )
