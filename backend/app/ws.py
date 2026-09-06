"""The WebSocket hub: fans out event-store changes to every connected
client. It subscribes once to the EventStore and, on each new event,
re-derives the projection and invariant check fresh from the full log —
the same pure functions a client could run itself, computed once here
instead of once per browser tab. It also relays a live snapshot of the
bus's in-flight messages and the ambulance's own progress/known_destination,
neither of which the event log alone can reconstruct (both are live state,
not history).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional, Protocol

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from app.checker import check
from app.events import Event, EventStore
from app.projection import project


class HubSource(Protocol):
    """What the hub needs beyond the event log — Simulation satisfies this
    structurally, no inheritance required."""

    def in_flight(self) -> list[dict]: ...

    def ambulance_view(self, transport_id: str) -> Optional[dict]: ...

    def hospital_view(self, hospital_id: str) -> Optional[dict]: ...  # Phase 17

    def hospital_ids(self) -> list[str]: ...  # Phase 17

    def transport_list(self) -> list[dict]: ...  # Phase 17


_HOSPITAL_EVENT_TYPES = frozenset(
    {"BedReserved", "BedReleased", "BedOccupied", "HospitalStatusChanged", "BedsReported"}
)
# Phase 21: these change *which* hospitals exist, not one hospital's numbers,
# so they trigger a whole-roster push rather than a per-hospital update — a
# decommissioned hospital has no view left to send.
_ROSTER_EVENT_TYPES = frozenset(
    {"HospitalRegistered", "HospitalUpdated", "HospitalDecommissioned"}
)
_ALERT_EVENT_TYPES = frozenset({"CapacityRebalance", "NoAcceptingFacility", "AutoRedirect"})


# Derived state (projection + invariant + hospital/in-flight snapshots) is
# recomputed and flushed at most this often, and everything produced in one
# window ships as a single "batch" message. Before this, every appended
# event triggered a replay+project+check and ~6 separate WebSocket sends —
# a burst (10 ambulances ticking, a mass-casualty preset) saturated the
# event loop serving HTTP, so the UI stopped responding to clicks, and the
# browser re-rendered once per message instead of once per burst.
_FLUSH_INTERVAL_S = 0.2


class Hub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store: Optional[EventStore] = None
        self._source: Optional[HubSource] = None
        self._pending_events: list[dict] = []
        self._dirty_transports: set[str] = set()
        self._dirty_hospitals: set[str] = set()
        self._pending_alerts: list[dict] = []
        self._roster_dirty = False
        self._moving: set[str] = set()
        self._flush_scheduled = False

    def configure(self, loop: asyncio.AbstractEventLoop, store: EventStore, source: HubSource) -> None:
        self._loop = loop
        self._store = store
        self._source = source
        store.subscribe(self._on_event)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        await self._send_snapshot(websocket)

    async def _send_snapshot(self, websocket: WebSocket) -> None:
        """Bring a newly connected client up to date immediately.

        Everything here is *live* state that no event will re-announce: the
        hub only pushes when something changes, so a client that connects
        mid-run showed an empty world until the next event happened to fire.
        On the per-endpoint screens that was stark — an ambulance already
        halfway to a hospital rendered as "no destination".
        """
        if self._store is None or self._source is None:
            return
        # Project the whole log once rather than walking transport_list():
        # that only returns *capacity-aware* transports, so the plain
        # three-hospital demo (AMB-101, no patient record) was skipped
        # entirely and its screens still opened empty. Every transport that
        # appears in the log belongs in a snapshot.
        events = self._store.replay()
        projection = project(events)
        messages: list[dict[str, Any]] = []
        for tid, view in projection.transports.items():
            messages.append({"kind": "transport", "transport_id": tid, "view": view})
            messages.append(
                {"kind": "invariant", "transport_id": tid, "result": check(self._store.replay(tid))}
            )
            ambulance = self._source.ambulance_view(tid)
            if ambulance is not None:
                messages.append({"kind": "ambulance", "transport_id": tid, **ambulance})
        for (facility_id, owner), facility_view in projection.facilities.items():
            messages.append(
                {"kind": "facility", "facility_id": facility_id,
                 "transport_id": owner, "view": facility_view}
            )
        messages.append({"kind": "roster", "hospitals": self._roster_views()})
        messages.append({"kind": "transport_list", "rows": self._source.transport_list()})
        messages.append({"kind": "in_flight", "messages": self._source.in_flight()})
        try:
            # Marked so a client (or a test) can tell "here is the world as it
            # stands" apart from "here is what just changed".
            await websocket.send_json(
                jsonable_encoder({"kind": "batch", "snapshot": True, "messages": messages})
            )
        except Exception:
            self._connections.discard(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    def rebind_source(self, source: HubSource) -> None:
        """Called by /demo/reset after it replaces the Simulation instance —
        the store stays the same (just cleared) so its subscription is
        still valid, but the old source would otherwise keep answering for
        a Bus/ambulance set nothing points to anymore."""
        self._source = source

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
        """Called synchronously from wherever EventStore.append() happened —
        so this does no derivation at all, just records what changed. The
        expensive part (replay/project/check) runs once per flush window on
        the loop, in _flush()."""
        self._pending_events.append(_event_payload(event))
        self._dirty_transports.add(event.transport_id)
        if event.type.value in _HOSPITAL_EVENT_TYPES:
            hospital_id = event.payload.get("hospital_id") or event.facility_id
            if hospital_id:
                self._dirty_hospitals.add(hospital_id)
        if event.type.value in _ROSTER_EVENT_TYPES:
            self._roster_dirty = True
        if event.type.value in _ALERT_EVENT_TYPES:
            self._pending_alerts.append(
                {"kind": "alert", "type": event.type.value, "transport_id": event.transport_id, "payload": event.payload}
            )
        self._schedule_flush()

    def note_movement(self, transport_id: str) -> None:
        """An ambulance changed position. Deliberately kept out of
        _dirty_transports: that set triggers a replay + project + check per
        transport, which is far too expensive to run on every movement tick
        for every ambulance. This only refreshes the ambulance's own live
        view, which needs no derivation from the log at all."""
        self._moving.add(transport_id)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_scheduled or self._loop is None or self._loop.is_closed():
            return
        self._flush_scheduled = True
        try:
            asyncio.run_coroutine_threadsafe(self._flush_after_delay(), self._loop)
        except RuntimeError:
            # The loop closed between the check above and here (shutdown, or
            # a test client tearing down mid-flush) — there is nobody left to
            # broadcast to, so dropping the flush is the whole correct
            # response.
            self._flush_scheduled = False

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            self._flush()
        finally:
            self._flush_scheduled = False
            # An event that landed while the flush above was running would
            # otherwise sit until the next one came along.
            if self._pending_events or self._pending_alerts or self._moving:
                self._schedule_flush()

    def _flush(self) -> None:
        events, self._pending_events = self._pending_events, []
        transports, self._dirty_transports = self._dirty_transports, set()
        hospitals, self._dirty_hospitals = self._dirty_hospitals, set()
        alerts, self._pending_alerts = self._pending_alerts, []
        roster_dirty, self._roster_dirty = self._roster_dirty, False
        moving, self._moving = self._moving, set()
        if not events and not alerts and not moving:
            return
        if not self._connections:
            return  # nobody listening: skip the replay/project/check entirely

        assert self._store is not None  # configure() runs before any event can fire
        messages: list[dict[str, Any]] = [{"kind": "event", "event": payload} for payload in events]

        for transport_id in transports:
            events_for_transport = self._store.replay(transport_id)
            projection = project(events_for_transport)
            transport_view = projection.transports.get(transport_id)
            if transport_view is not None:
                messages.append({"kind": "transport", "transport_id": transport_id, "view": transport_view})
            for (facility_id, tid), facility_view in projection.facilities.items():
                if tid == transport_id:
                    messages.append(
                        {"kind": "facility", "facility_id": facility_id, "transport_id": tid, "view": facility_view}
                    )
            messages.append(
                {"kind": "invariant", "transport_id": transport_id, "result": check(events_for_transport)}
            )
            if self._source is not None:
                ambulance = self._source.ambulance_view(transport_id)
                if ambulance is not None:
                    messages.append({"kind": "ambulance", "transport_id": transport_id, **ambulance})
                else:
                    # No live ambulance for a transport that just changed: it
                    # has been discharged and retired. Said explicitly, because
                    # "nothing to push" is indistinguishable from "unchanged"
                    # on the client, which left a discharged patient's marker
                    # sitting on the map forever.
                    messages.append({"kind": "ambulance", "transport_id": transport_id, "removed": True})

        if self._source is not None:
            messages.append({"kind": "in_flight", "messages": self._source.in_flight()})
            if transports:
                # Sent as one whole-list replacement rather than per-transport
                # deltas: the table shows current/pending/status/position, and
                # only the first of those is derivable from the per-transport
                # "transport" message above. Without this the table could only
                # be refreshed by hand and went stale on every redirect.
                messages.append({"kind": "transport_list", "rows": self._source.transport_list()})
            if roster_dirty:
                messages.append({"kind": "roster", "hospitals": self._roster_views()})
            for hospital_id in hospitals:
                hospital_view = self._source.hospital_view(hospital_id)
                if hospital_view is not None:
                    messages.append({"kind": "hospital", "hospital_id": hospital_id, "view": hospital_view})

        # Movement for transports that had no event this window — a cheap
        # position/progress refresh with none of the projection work above.
        if self._source is not None:
            for transport_id in moving - transports:
                ambulance = self._source.ambulance_view(transport_id)
                if ambulance is not None:
                    messages.append({"kind": "ambulance", "transport_id": transport_id, **ambulance})

        messages.extend(alerts)
        self.broadcast({"kind": "batch", "messages": messages})

    def broadcast_reset(self) -> None:
        """/demo/reset clears the log and swaps the Simulation without
        appending a single event — so nothing here fires, and every client
        would otherwise keep showing the pre-reset world indefinitely (bed
        bars stuck at their old counts, dead transports still listed) until
        someone reloaded the page. The store is the source of truth; when it
        is emptied the browser has to be told so explicitly.

        Buffered work from before the clear is dropped: it describes events
        that no longer exist, and the fresh views below supersede it."""
        self._pending_events.clear()
        self._dirty_transports.clear()
        self._dirty_hospitals.clear()
        self._pending_alerts.clear()

        messages: list[dict[str, Any]] = [{"kind": "reset"}]
        if self._source is not None:
            messages.append({"kind": "roster", "hospitals": self._roster_views()})
        # Reset first, then the post-reset truth: the client folds these in
        # order, so the wipe can never land on top of the fresh views.
        if self._source is not None:
            for hospital_id in self._source.hospital_ids():
                hospital_view = self._source.hospital_view(hospital_id)
                if hospital_view is not None:
                    messages.append(
                        {"kind": "hospital", "hospital_id": hospital_id, "view": hospital_view}
                    )
            messages.append({"kind": "in_flight", "messages": self._source.in_flight()})
            messages.append({"kind": "transport_list", "rows": self._source.transport_list()})
        self.broadcast({"kind": "batch", "messages": messages})

    def _roster_views(self) -> list[dict]:
        """The whole network, as the client should now see it. Sent wholesale
        rather than as a delta so a removal needs no special handling: the
        hospital is simply absent from the next roster."""
        if self._source is None:
            return []
        views = []
        for hospital_id in self._source.hospital_ids():
            view = self._source.hospital_view(hospital_id)
            if view is not None:
                views.append(view)
        return views

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
