"""The WebSocket hub: fans out event-store changes to every connected
client. It subscribes once to the EventStore and, on each new event,
re-derives the projection and invariant check fresh from the full log —
the same pure functions a client could run itself, computed once here
instead of once per browser tab. It also relays a live snapshot of the
bus's in-flight messages, since that isn't something the event log alone
can reconstruct (it only shows what has already happened).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional, Protocol

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from app.checker import check
from app.events import Event, EventStore
from app.projection import project


class InFlightSource(Protocol):
    def in_flight(self) -> list[dict]: ...


class Hub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store: Optional[EventStore] = None
        self._in_flight_source: Optional[InFlightSource] = None

    def configure(self, loop: asyncio.AbstractEventLoop, store: EventStore, in_flight_source: InFlightSource) -> None:
        self._loop = loop
        self._store = store
        self._in_flight_source = in_flight_source
        store.subscribe(self._on_event)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    def rebind_in_flight_source(self, source: InFlightSource) -> None:
        """Called by /demo/reset after it replaces the Simulation instance —
        the store stays the same (just cleared) so its subscription is
        still valid, but the old in_flight_source would otherwise keep
        answering for a Bus nothing points to anymore."""
        self._in_flight_source = source

    def broadcast(self, message: dict[str, Any]) -> None:
        """Safe to call from any context — EventStore.subscribe() calls
        _on_event synchronously from wherever append() happened, which is
        not necessarily inside a coroutine on the hub's own loop."""
        if self._loop is None or not self._connections:
            return
        # WebSocket.send_json uses plain json.dumps, not FastAPI's request/
        # response encoding — messages here carry raw dataclasses and Enum
        # members (TransportView, CheckResult, ...), so they're encoded to
        # plain JSON-safe values up front, once, for every caller.
        encoded = jsonable_encoder(message)
        asyncio.run_coroutine_threadsafe(self._broadcast_async(encoded), self._loop)

    async def _broadcast_async(self, message: dict[str, Any]) -> None:
        dead = []
        for connection in list(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._connections.discard(connection)

    def _on_event(self, event: Event) -> None:
        self.broadcast({"kind": "event", "event": _event_payload(event)})

        assert self._store is not None  # configure() runs before any event can fire
        events_for_transport = self._store.replay(event.transport_id)
        projection = project(events_for_transport)
        result = check(events_for_transport)

        transport_view = projection.transports.get(event.transport_id)
        if transport_view is not None:
            self.broadcast({"kind": "transport", "transport_id": event.transport_id, "view": transport_view})

        for (facility_id, transport_id), facility_view in projection.facilities.items():
            if transport_id == event.transport_id:
                self.broadcast(
                    {"kind": "facility", "facility_id": facility_id, "transport_id": transport_id, "view": facility_view}
                )

        self.broadcast({"kind": "invariant", "transport_id": event.transport_id, "result": result})

        if self._in_flight_source is not None:
            self.broadcast({"kind": "in_flight", "messages": self._in_flight_source.in_flight()})

    def broadcast_fuzz(self, summary: dict[str, Any]) -> None:
        self.broadcast({"kind": "fuzz", **summary})


def _event_payload(event: Event) -> dict:
    return {
        "seq": event.seq,
        "transport_id": event.transport_id,
        "epoch": event.epoch,
        "ts_ms": event.ts_ms,
        "type": event.type.value,
        "facility_id": event.facility_id,
        "payload": event.payload,
    }
