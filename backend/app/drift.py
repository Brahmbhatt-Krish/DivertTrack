"""Phase 14 (F4): status drift — a scheduled routine that emits
HospitalStatusChanged/BedsReported events on a timeline (a specialist going
off shift, an ED saturation ramp, a diversion toggle, a bed count drop) and
applies each to the matching Facility's live status as it fires.

Deterministic from the preset: a StatusDrift/BedDrift list is just data, in
the same spirit as presets.py's fixed delay tables — nothing here rolls
dice. Chaos mode doesn't get a separate code path: a caller wanting random
drift builds its StatusDrift/BedDrift list with a random generator instead
of literal values and hands it to the exact same schedule_drift().
"""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Optional

from app.clock import Clock
from app.dispatcher import Dispatcher
from app.events import Event, EventStore, EventType
from app.facility import Facility
from app.models import BedType


@dataclass(frozen=True)
class StatusDrift:
    at_ms: int
    hospital_id: str
    # Partial HospitalStatus fields, JSON-safe — see
    # projection._decode_status_changes for the keys understood.
    changes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BedDrift:
    at_ms: int
    hospital_id: str
    bed_type: BedType
    total: int


def schedule_drift(
    clock: Clock,
    store: EventStore,
    facilities: dict[str, Facility],
    status_drifts: list[StatusDrift] = (),
    bed_drifts: list[BedDrift] = (),
    dispatcher: Optional[Dispatcher] = None,
) -> None:
    for drift in status_drifts:
        clock.schedule(drift.at_ms, lambda d=drift: _apply_status_drift(clock, store, facilities, d, dispatcher))
    for drift in bed_drifts:
        clock.schedule(drift.at_ms, lambda d=drift: _apply_bed_drift(clock, store, facilities, d, dispatcher))


def _apply_status_drift(
    clock: Clock, store: EventStore, facilities: dict[str, Facility], drift: StatusDrift, dispatcher: Optional[Dispatcher]
) -> None:
    persisted = store.append(
        Event(
            # Hospital-scoped, not transport-scoped — Event.transport_id is
            # required, so the hospital_id fills that role here too (see
            # the phase report: this is a disclosed, pragmatic reuse of the
            # existing transport-centric Event schema rather than a schema
            # migration). checker.py groups by transport_id, so this simply
            # forms its own inert group there — it carries no CutoverApplied/
            # FacilityStateChanged/Arrived for I1 to ever look at.
            transport_id=drift.hospital_id, epoch=0, ts_ms=clock.now_ms(),
            type=EventType.HOSPITAL_STATUS_CHANGED, facility_id=drift.hospital_id,
            payload=dict(drift.changes),
        )
    )
    facility = facilities.get(drift.hospital_id)
    if facility is not None:
        facility.on_status_event(persisted)
    if dispatcher is not None and facility is not None and facility.status_of() is not None:
        dispatcher.on_hospital_status_changed(drift.hospital_id, facility.status_of())


def _apply_bed_drift(
    clock: Clock, store: EventStore, facilities: dict[str, Facility], drift: BedDrift, dispatcher: Optional[Dispatcher]
) -> None:
    persisted = store.append(
        Event(
            transport_id=drift.hospital_id, epoch=0, ts_ms=clock.now_ms(),
            type=EventType.BEDS_REPORTED, facility_id=drift.hospital_id,
            payload={"bed_type": drift.bed_type.value, "total": drift.total},
        )
    )
    facility = facilities.get(drift.hospital_id)
    if facility is not None:
        facility.on_status_event(persisted)
    if dispatcher is not None:
        dispatcher.on_beds_reported(drift.hospital_id, drift.bed_type, drift.total)
