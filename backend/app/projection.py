"""Pure derivation of a point-in-time view from the event log. Replaying the
same events twice gives the same views twice — nothing here holds state
between calls or reaches into a live Dispatcher/Facility.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Sequence

from app.dispatcher_status import DispatcherStatus
from app.events import Event, EventType
from app.messages import FacilityState
from app.models import (
    BedType,
    ConditionCategory,
    Diversion,
    Hospital,
    HospitalStatus,
    Need,
    Patient,
    Specialist,
    hospital_from_payload,
)


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
    # Phase 12+: every candidate declined and there was nothing to fall back
    # to. The projection could not represent this at all, so a transport the
    # dispatcher had given up on replayed as STARTING toward a hospital it
    # was never placed at. Matches DispatcherStatus.NO_ACCEPTING_FACILITY,
    # which the frontend already styles.
    NO_ACCEPTING_FACILITY = "NO_ACCEPTING_FACILITY"


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
    elif event.type is EventType.NO_ACCEPTING_FACILITY and event.payload.get("gave_up"):
        # The dispatcher abandoned this placement: every candidate declined
        # and there was nothing to fall back to. Without this the log replayed
        # the transport as still STARTING toward a hospital it was never
        # placed at, so anything rebuilding state from the log alone — a
        # restart, an audit — disagreed with what actually happened.
        #
        # Only the sites that genuinely park the transport carry `gave_up`.
        # R17 also logs NoAcceptingFacility when it finds no candidate but
        # keeps the current destination, and that case must not clear
        # anything, which is why the flag exists rather than the event type
        # being enough on its own.
        state.pending_destination = None
        state.pending_epoch = None
        state.cutover_at = None
        state.status = DispatcherStatus.NO_ACCEPTING_FACILITY

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


# -- Phase 12: the bed ledger (multi-hospital capacity extension) -----------
# Pure, same spirit as project()/check() above: replays BedReserved/
# BedReleased/BedOccupied and derives, per hospital, which transports
# currently hold a reservation or occupancy in each bed type. A ventilator
# hold is not in these events' payload (Need.VENTILATOR is a static patient
# fact, not something the bus carries) — `patients` supplies it once, here,
# rather than smuggling it onto every BedReserved payload.


@dataclass(frozen=True)
class HospitalLedgerView:
    hospital_id: str
    reserved: dict[BedType, frozenset[str]] = field(default_factory=dict)
    occupied: dict[BedType, frozenset[str]] = field(default_factory=dict)
    ventilator_holders: frozenset[str] = frozenset()


def empty_ledger_view(hospital_id: str) -> HospitalLedgerView:
    """What project_ledger(...) would produce for a hospital with no
    BedReserved/BedReleased/BedOccupied events yet — callers that look a
    hospital_id up in project_ledger's result dict and may not find one
    (nothing has happened there) use this instead of special-casing a
    missing key."""
    return HospitalLedgerView(hospital_id=hospital_id)


@dataclass
class _MutableLedger:
    reserved: dict[BedType, set[str]] = field(default_factory=dict)
    occupied: dict[BedType, set[str]] = field(default_factory=dict)
    ventilator_holders: set[str] = field(default_factory=set)


def project_ledger(events: list[Event], patients: dict[str, Patient]) -> dict[str, HospitalLedgerView]:
    mutable: dict[str, _MutableLedger] = {}
    for event in events:
        if event.type is EventType.BED_RESERVED:
            _apply_bed_reserved(mutable, event, patients)
        elif event.type is EventType.BED_RELEASED:
            _apply_bed_released(mutable, event)
        elif event.type is EventType.BED_OCCUPIED:
            _apply_bed_occupied(mutable, event)

    return {
        hospital_id: HospitalLedgerView(
            hospital_id=hospital_id,
            reserved={bed_type: frozenset(ids) for bed_type, ids in ledger.reserved.items()},
            occupied={bed_type: frozenset(ids) for bed_type, ids in ledger.occupied.items()},
            ventilator_holders=frozenset(ledger.ventilator_holders),
        )
        for hospital_id, ledger in mutable.items()
    }


def _apply_bed_reserved(mutable: dict[str, _MutableLedger], event: Event, patients: dict[str, Patient]) -> None:
    hospital_id = event.payload["hospital_id"]
    transport_id = event.payload["transport_id"]
    bed_type = BedType(event.payload["bed_type"])
    ledger = mutable.setdefault(hospital_id, _MutableLedger())
    ledger.reserved.setdefault(bed_type, set()).add(transport_id)
    patient = patients.get(transport_id)
    if patient is not None and Need.VENTILATOR in patient.needs:
        ledger.ventilator_holders.add(transport_id)


def _apply_bed_released(mutable: dict[str, _MutableLedger], event: Event) -> None:
    hospital_id = event.payload["hospital_id"]
    transport_id = event.payload["transport_id"]
    ledger = mutable.setdefault(hospital_id, _MutableLedger())
    for ids in ledger.reserved.values():
        ids.discard(transport_id)
    # Occupancy too — BedReleased means "this transport no longer holds a bed
    # here", and dropping only the reservation left a released occupied bed
    # counted against the hospital for the rest of the log.
    for ids in ledger.occupied.values():
        ids.discard(transport_id)
    ledger.ventilator_holders.discard(transport_id)


def _apply_bed_occupied(mutable: dict[str, _MutableLedger], event: Event) -> None:
    # R19: moves a transport from reserved to occupied. If no reservation
    # actually existed, this still records the occupancy — the ledger is a
    # passive replay, not a validator; a missing reservation is checker.py's
    # I3 to flag, not something to silently correct here.
    hospital_id = event.payload["hospital_id"]
    transport_id = event.payload["transport_id"]
    bed_type = BedType(event.payload["bed_type"])
    ledger = mutable.setdefault(hospital_id, _MutableLedger())
    for ids in ledger.reserved.values():
        ids.discard(transport_id)
    ledger.occupied.setdefault(bed_type, set()).add(transport_id)


@dataclass(frozen=True)
class HospitalRoster:
    """What replaying the roster events produces.

    `active` is the network as it stands now. `known` also holds every
    hospital that has ever been registered, including decommissioned ones,
    at its last-registered configuration — the checker needs those: it
    re-runs accept() over historical events to verify I4, and judging a past
    acceptance against a roster that no longer contains the hospital would
    turn a perfectly valid decision into a phantom violation."""

    active: dict[str, Hospital] = field(default_factory=dict)
    known: dict[str, Hospital] = field(default_factory=dict)


def project_hospitals(events: Sequence[Event]) -> HospitalRoster:
    """Pure: the hospital roster is a fold over the log, exactly like
    TransportView and the bed ledger. Nothing else may be a source of truth
    for which hospitals exist.

    Roster events carry the hospital id in `transport_id` rather than a
    dedicated column — the events table declares it NOT NULL and predates
    hospital-scoped events, so HospitalStatusChanged and BedsReported already
    use it that way. Following the same convention keeps this a pure addition
    with no migration; `facility_id` carries the same id for readability.
    """
    active: dict[str, Hospital] = {}
    known: dict[str, Hospital] = {}
    for event in events:
        if event.type is EventType.HOSPITAL_REGISTERED:
            hospital = hospital_from_payload(event.payload)
            active[hospital.id] = hospital
            known[hospital.id] = hospital
        elif event.type is EventType.HOSPITAL_UPDATED:
            hospital = hospital_from_payload(event.payload)
            known[hospital.id] = hospital
            # An update to a decommissioned hospital records the new config
            # without resurrecting it; re-registering is what brings one back.
            if hospital.id in active:
                active[hospital.id] = hospital
        elif event.type is EventType.HOSPITAL_DECOMMISSIONED:
            active.pop(event.payload.get("hospital_id") or event.transport_id, None)
    return HospitalRoster(active=active, known=known)


def without_transport(view: HospitalLedgerView, transport_id: str) -> HospitalLedgerView:
    """The same ledger with one transport's *reservation* taken out.

    Acceptance has to be asked "do you have room for this patient", not "do
    you have room for this patient on top of the bed you are already holding
    for them". The dispatcher reserves optimistically before sending PREPARE,
    so by the time the facility evaluates the request its own reservation is
    already in the ledger — counting it made a hospital refuse the last bed
    of a type to the very transport that had just claimed it, which meant no
    transport could ever be given a hospital's last bed.

    Occupancy is deliberately left alone: an occupied bed has a patient
    physically in it, and no transport should ever hold both.
    """
    return HospitalLedgerView(
        hospital_id=view.hospital_id,
        reserved={
            bed_type: ids - {transport_id} for bed_type, ids in view.reserved.items()
        },
        occupied=view.occupied,
        ventilator_holders=view.ventilator_holders - {transport_id},
    )


def free(view: HospitalLedgerView, status: HospitalStatus, bed_type: BedType) -> int:
    used = len(view.reserved.get(bed_type, frozenset())) + len(view.occupied.get(bed_type, frozenset()))
    return status.beds_total.get(bed_type, 0) - used


def load(view: HospitalLedgerView, status: HospitalStatus) -> float:
    total_beds = sum(status.beds_total.values())
    if total_beds <= 0:
        return 0.0
    used = sum(
        len(view.reserved.get(bed_type, frozenset())) + len(view.occupied.get(bed_type, frozenset()))
        for bed_type in status.beds_total
    )
    return used / total_beds


def ventilators_free(view: HospitalLedgerView, status: HospitalStatus) -> int:
    return status.ventilators_total - len(view.ventilator_holders)


# -- Phase 14 (F3): live HospitalStatus, replayed the same way as everything
# else — apply_status_event() is pure (old status + one event -> new
# status), so a Facility's live status is always "whatever replaying every
# HospitalStatusChanged/BedsReported it's seen produces", never separately
# mutated state that could drift from the log.


def apply_status_event(status: HospitalStatus, event: Event) -> HospitalStatus:
    if event.type is EventType.HOSPITAL_STATUS_CHANGED:
        return replace(status, **_decode_status_changes(event.payload))
    if event.type is EventType.BEDS_REPORTED:
        bed_type = BedType(event.payload["bed_type"])
        beds_total = dict(status.beds_total)
        beds_total[bed_type] = event.payload["total"]
        return replace(status, beds_total=beds_total)
    return status


def _decode_status_changes(payload: dict) -> dict:
    """HospitalStatusChanged's payload is a partial HospitalStatus — only
    the fields actually changing, JSON-safe (the API layer, Phase 17,
    builds this from POST /hospitals/{id}/status). Decodes just the keys
    present back into the real enum/frozenset types."""
    changes: dict = {}
    if "specialists_on_shift" in payload:
        changes["specialists_on_shift"] = frozenset(Specialist(s) for s in payload["specialists_on_shift"])
    if "ed_saturation" in payload:
        changes["ed_saturation"] = float(payload["ed_saturation"])
    if "diversion" in payload:
        changes["diversion"] = Diversion(payload["diversion"])
    if "diverted_categories" in payload:
        changes["diverted_categories"] = frozenset(ConditionCategory(c) for c in payload["diverted_categories"])
    return changes
