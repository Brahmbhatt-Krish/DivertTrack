"""Pure derivation of a point-in-time view from the event log. Replaying the
same events twice gives the same views twice — nothing here holds state
between calls or reaches into a live Dispatcher/Facility.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.dispatcher import DispatcherStatus
from app.events import Event, EventType
from app.messages import FacilityState


class TransportViewStatus(str, Enum):
    STARTING = "STARTING"
    STABLE = "STABLE"
    PREPARING = "PREPARING"
    CUTOVER_SCHEDULED = "CUTOVER_SCHEDULED"
    NOT_READY = "NOT_READY"
    # Produced only by project(..., mark_interrupted=True): a backend restart
    # replays the log but resumes no timers (E21), so a transport frozen
    # mid-transition needs a reset, not a wait.
    INTERRUPTED = "INTERRUPTED"


_INTERRUPTIBLE = {DispatcherStatus.STARTING, DispatcherStatus.PREPARING, DispatcherStatus.CUTOVER_SCHEDULED}


@dataclass(frozen=True)
class TransportView:
    transport_id: str
    current_destination: Optional[str]
    current_epoch: int
    pending_destination: Optional[str]
    pending_epoch: Optional[int]
    status: TransportViewStatus
    cutover_at: Optional[int]
    queued_redirect: Optional[str]
    arrived_at: Optional[str]


@dataclass(frozen=True)
class FacilityView:
    state: FacilityState
    epoch: int
    stale: bool


@dataclass(frozen=True)
class Projection:
    transports: dict[str, TransportView]
    facilities: dict[tuple[str, str], FacilityView]  # (facility_id, transport_id)


def project(events: list[Event], mark_interrupted: bool = False) -> Projection:
    transports = _project_transports(events, mark_interrupted)
    facilities = _project_facilities(events, transports)
    return Projection(transports=transports, facilities=facilities)


@dataclass
class _MutableTransport:
    current_destination: Optional[str] = None
    current_epoch: int = 0
    pending_destination: Optional[str] = None
    pending_epoch: Optional[int] = None
    status: DispatcherStatus = DispatcherStatus.STARTING
    cutover_at: Optional[int] = None
    queued_redirect: Optional[str] = None
    arrived_at: Optional[str] = None


def _project_transports(events: list[Event], mark_interrupted: bool) -> dict[str, TransportView]:
    mutable: dict[str, _MutableTransport] = {}
    for event in events:
        state = mutable.setdefault(event.transport_id, _MutableTransport())
        _apply_transport_event(state, event)

    views: dict[str, TransportView] = {}
    for transport_id, state in mutable.items():
        interrupted = mark_interrupted and state.status in _INTERRUPTIBLE
        status = TransportViewStatus.INTERRUPTED if interrupted else TransportViewStatus(state.status.value)
        views[transport_id] = TransportView(
            transport_id=transport_id,
            current_destination=state.current_destination,
            current_epoch=state.current_epoch,
            pending_destination=state.pending_destination,
            pending_epoch=state.pending_epoch,
            status=status,
            cutover_at=state.cutover_at,
            queued_redirect=state.queued_redirect,
            arrived_at=state.arrived_at,
        )
    return views


def _apply_transport_event(state: _MutableTransport, event: Event) -> None:
    if event.type is EventType.TRANSPORT_STARTED:
        state.pending_destination = event.payload["destination"]
        state.pending_epoch = event.epoch
        state.status = DispatcherStatus.STARTING
    elif event.type is EventType.REDIRECT_REQUESTED:
        state.pending_destination = event.payload["target"]
        state.pending_epoch = event.epoch
        # A redirect fired before start()'s own cutover ever applied stays
        # STARTING (Dispatcher._redirect_before_first_activation) — every
        # other redirect goes PREPARING (Dispatcher._begin_redirect).
        state.status = (
            DispatcherStatus.STARTING if event.payload.get("still_starting") else DispatcherStatus.PREPARING
        )
    elif event.type is EventType.REDIRECT_QUEUED:
        state.queued_redirect = event.payload["target"]
    elif event.type is EventType.CUTOVER_SCHEDULED:
        state.cutover_at = event.payload["cutover_at"]
        state.status = DispatcherStatus.CUTOVER_SCHEDULED
    elif event.type is EventType.CUTOVER_CANCELLED:
        state.current_epoch = event.epoch
        state.pending_destination = None
        state.pending_epoch = None
        state.cutover_at = None
        state.status = DispatcherStatus.STABLE
    elif event.type is EventType.CUTOVER_APPLIED:
        state.current_destination = event.payload["current_destination"]
        state.current_epoch = event.epoch
        state.pending_destination = None
        state.pending_epoch = None
        state.cutover_at = None
        state.status = DispatcherStatus.STABLE
        state.queued_redirect = None  # consumed here — see Dispatcher._run_queued_redirect
    elif event.type is EventType.REDIRECT_ABORTED:
        state.current_epoch = event.epoch
        state.pending_destination = None
        state.pending_epoch = None
        state.cutover_at = None
        state.status = DispatcherStatus.NOT_READY
        state.queued_redirect = None  # see Dispatcher._abort
    elif event.type is EventType.ARRIVED:
        state.arrived_at = event.payload["at"]


@dataclass
class _MutableFacility:
    state: FacilityState = FacilityState.IDLE
    epoch: int = 0


def _project_facilities(
    events: list[Event], transports: dict[str, TransportView]
) -> dict[tuple[str, str], FacilityView]:
    mutable: dict[tuple[str, str], _MutableFacility] = {}
    for event in events:
        if event.facility_id is None:
            continue
        key = (event.facility_id, event.transport_id)
        facility = mutable.setdefault(key, _MutableFacility())
        if event.type is EventType.FACILITY_STATE_CHANGED:
            facility.state = FacilityState(event.payload["to"])
            facility.epoch = max(facility.epoch, event.epoch)
        elif event.type is EventType.ILLEGAL_TRANSITION:
            # step 4 still raises the fence even when the transition itself
            # is rejected (E17) — the facility's epoch moves even though
            # its state doesn't.
            facility.epoch = max(facility.epoch, event.epoch)

    views: dict[tuple[str, str], FacilityView] = {}
    for (facility_id, transport_id), facility in mutable.items():
        transport = transports.get(transport_id)
        in_current_plan = transport is not None and facility_id in (
            transport.current_destination,
            transport.pending_destination,
        )
        stale = facility.state in (FacilityState.ARMED, FacilityState.ACTIVE) and not in_current_plan
        views[(facility_id, transport_id)] = FacilityView(state=facility.state, epoch=facility.epoch, stale=stale)
    return views
