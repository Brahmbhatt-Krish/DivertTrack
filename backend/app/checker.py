"""The invariant checker. A facility is *effectively* active for a transport
iff its local state is ACTIVE and it is that transport's current_destination
(see the README's "effective-active rule") — that definition is what makes
this a pure, independent audit rather than a re-statement of whatever the
dispatcher already believes: it walks the raw event log itself and never
imports Dispatcher or Facility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.acceptance import accept
from app.events import Event, EventType
from app.messages import FacilityState
from app.models import BedType, Hospital, Patient, initial_status
from app.projection import project_hospitals, HospitalLedgerView, apply_status_event


class ViolationKind(str, Enum):
    MULTI_ACTIVE = "MULTI_ACTIVE"
    ZERO_ACTIVE = "ZERO_ACTIVE"
    ARRIVED_AT_WRONG_FACILITY = "ARRIVED_AT_WRONG_FACILITY"
    # Phase 16 (I2-I4, multi-hospital capacity extension):
    OVERBOOKED = "OVERBOOKED"
    ARRIVED_WITHOUT_RESERVATION = "ARRIVED_WITHOUT_RESERVATION"
    INVALID_ACCEPTANCE = "INVALID_ACCEPTANCE"


@dataclass(frozen=True)
class Violation:
    transport_id: str
    ts_ms: int
    kind: ViolationKind
    detail: dict


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    violations: list[Violation]
    max_local_overlap_ms: int
    transitions_checked: int
    # Phase 16 (I2-I4, multi-hospital capacity extension) — all empty/zero
    # when hospitals/patients aren't supplied (the plain 3-hospital demo
    # never has capacity events to check in the first place). Defaulted so
    # _check_transport's own internal (per-transport, capacity-unaware)
    # CheckResult construction doesn't have to name them.
    capacity_warnings: list[str] = field(default_factory=list)
    max_load_per_hospital: dict[str, float] = field(default_factory=dict)
    declines_by_reason: dict[str, int] = field(default_factory=dict)


def check(
    events: list[Event],
    patients: Optional[dict[str, Patient]] = None,
    hospitals: Optional[dict[str, Hospital]] = None,
    saturation_limit: float = 0.9,
) -> CheckResult:
    violations: list[Violation] = []
    max_local_overlap_ms = 0
    transitions_checked = 0

    for transport_id, transport_events in _group_by_transport(events).items():
        result = _check_transport(transport_id, transport_events)
        violations.extend(result.violations)
        max_local_overlap_ms = max(max_local_overlap_ms, result.max_local_overlap_ms)
        transitions_checked += result.transitions_checked

    capacity_warnings: list[str] = []
    max_load_per_hospital: dict[str, float] = {}
    declines_by_reason: dict[str, int] = {}
    if hospitals is None:
        # Phase 21: derive the roster from the log rather than being told it.
        # `known` (not `active`) on purpose — I4 re-runs accept() over
        # historical events, and a hospital decommissioned since would
        # otherwise vanish from beds_total, turning every past reservation it
        # held into a phantom overbooking and every valid acceptance into a
        # violation. History has to be judged against the network as it was.
        hospitals = project_hospitals(events).known
    if hospitals:
        capacity_violations, capacity_warnings, max_load_per_hospital, declines_by_reason = _check_capacity(
            events, patients or {}, hospitals, saturation_limit
        )
        violations.extend(capacity_violations)

    return CheckResult(
        passed=len(violations) == 0,
        violations=violations,
        max_local_overlap_ms=max_local_overlap_ms,
        transitions_checked=transitions_checked,
        capacity_warnings=capacity_warnings,
        max_load_per_hospital=max_load_per_hospital,
        declines_by_reason=declines_by_reason,
    )


def _group_by_transport(events: list[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.transport_id, []).append(event)
    return grouped


def _check_transport(transport_id: str, events: list[Event]) -> CheckResult:
    violations: list[Violation] = []
    transitions_checked = 0
    max_local_overlap_ms = 0

    current_destination: Optional[str] = None
    facility_states: dict[str, FacilityState] = {}
    overlap_started_at: dict[str, int] = {}  # facility_id -> ts_ms it went ACTIVE while not current

    for _, instant in _group_by_instant(events):
        for event in instant:
            if event.type is EventType.CUTOVER_APPLIED:
                current_destination = event.payload["current_destination"]
            elif event.type is EventType.FACILITY_STATE_CHANGED and event.facility_id is not None:
                facility_states[event.facility_id] = FacilityState(event.payload["to"])
            elif event.type is EventType.ARRIVED:
                # E19: compared against current, never pending — pending
                # isn't even tracked here.
                if current_destination is not None and event.payload["at"] != current_destination:
                    violations.append(
                        Violation(
                            transport_id,
                            event.ts_ms,
                            ViolationKind.ARRIVED_AT_WRONG_FACILITY,
                            {"arrived_at": event.payload["at"], "current_destination": current_destination},
                        )
                    )
            # CutoverCancelled and RedirectAborted deliberately leave
            # current_destination untouched — neither one changes who's
            # actually receiving the patient.

        if current_destination is None:
            continue

        ts_ms = instant[-1].ts_ms
        transitions_checked += 1
        current_is_active = facility_states.get(current_destination) is FacilityState.ACTIVE
        if not current_is_active:
            violations.append(
                Violation(
                    transport_id,
                    ts_ms,
                    ViolationKind.ZERO_ACTIVE,
                    {"current_destination": current_destination, "facility_states": _snapshot(facility_states)},
                )
            )

        # Overlap is informational only (spec: a stale facility being
        # locally ACTIVE "is not a violation of this invariant") — tracked
        # here purely for max_local_overlap_ms.
        for facility_id, state in facility_states.items():
            is_overlapping = facility_id != current_destination and state is FacilityState.ACTIVE
            if is_overlapping:
                overlap_started_at.setdefault(facility_id, ts_ms)
            elif facility_id in overlap_started_at:
                max_local_overlap_ms = max(max_local_overlap_ms, ts_ms - overlap_started_at.pop(facility_id))

    if events:
        last_ts_ms = events[-1].ts_ms
        for start_ms in overlap_started_at.values():
            max_local_overlap_ms = max(max_local_overlap_ms, last_ts_ms - start_ms)

    return CheckResult(
        passed=len(violations) == 0,
        violations=violations,
        max_local_overlap_ms=max_local_overlap_ms,
        transitions_checked=transitions_checked,
    )


def _group_by_instant(events: list[Event]) -> list[tuple[int, list[Event]]]:
    """Groups consecutive (already seq-ordered) events sharing one ts_ms —
    an "atomic instant" per the spec — preserving arrival order."""
    groups: list[tuple[int, list[Event]]] = []
    for event in events:
        if groups and groups[-1][0] == event.ts_ms:
            groups[-1][1].append(event)
        else:
            groups.append((event.ts_ms, [event]))
    return groups


def _snapshot(facility_states: dict[str, FacilityState]) -> dict[str, str]:
    return {facility_id: state.value for facility_id, state in facility_states.items()}


# -- Phase 16: I2 (no overbooking), I3 (arrival has a reservation), I4
# (every acceptance was valid at the moment it was given) — a second, global
# pass over the same event log (capacity is a cross-transport resource,
# unlike I1's per-transport handoff invariant above).


def _check_capacity(
    events: list[Event],
    patients: dict[str, Patient],
    hospitals: dict[str, Hospital],
    saturation_limit: float,
) -> tuple[list[Violation], list[str], dict[str, float], dict[str, int]]:
    violations: list[Violation] = []
    warnings: set[str] = set()
    max_load_per_hospital: dict[str, float] = {}
    declines_by_reason: dict[str, int] = {}

    reserved: dict[tuple[str, BedType], set[str]] = {}
    occupied: dict[tuple[str, BedType], set[str]] = {}
    beds_total: dict[tuple[str, BedType], int] = {
        (hospital_id, bed_type): total
        for hospital_id, hospital in hospitals.items()
        for bed_type, total in hospital.beds_total.items()
    }
    statuses = {hospital_id: initial_status(hospital) for hospital_id, hospital in hospitals.items()}
    ever_reserved: set[str] = set()
    claimed: dict[tuple[str, str], bool] = {}
    command_hospital: dict[str, str] = {}

    for event in events:
        if event.type is EventType.COMMAND_SENT and event.facility_id is not None:
            command_id = event.payload.get("command_id")
            if command_id is not None:
                command_hospital[command_id] = event.facility_id

        elif event.type is EventType.BED_RESERVED:
            hospital_id, transport_id = event.payload["hospital_id"], event.payload["transport_id"]
            bed_type = BedType(event.payload["bed_type"])
            reserved.setdefault((hospital_id, bed_type), set()).add(transport_id)
            ever_reserved.add(transport_id)
            claimed[(hospital_id, transport_id)] = True
            _check_overbooked(hospital_id, bed_type, reserved, occupied, beds_total, violations, warnings, event.ts_ms, "reserve")

        elif event.type is EventType.BED_RELEASED:
            hospital_id, transport_id = event.payload["hospital_id"], event.payload["transport_id"]
            bed_type = BedType(event.payload["bed_type"])
            reserved.get((hospital_id, bed_type), set()).discard(transport_id)
            claimed[(hospital_id, transport_id)] = False

        elif event.type is EventType.BED_OCCUPIED:
            hospital_id, transport_id = event.payload["hospital_id"], event.payload["transport_id"]
            bed_type = BedType(event.payload["bed_type"])
            reserved.get((hospital_id, bed_type), set()).discard(transport_id)
            occupied.setdefault((hospital_id, bed_type), set()).add(transport_id)
            claimed[(hospital_id, transport_id)] = True

        elif event.type is EventType.ARRIVED:
            hospital_id = event.payload["at"]
            if event.transport_id in ever_reserved and not claimed.get((hospital_id, event.transport_id), False):
                violations.append(
                    Violation(
                        event.transport_id, event.ts_ms, ViolationKind.ARRIVED_WITHOUT_RESERVATION,
                        {"hospital_id": hospital_id},
                    )
                )

        elif event.type is EventType.HOSPITAL_STATUS_CHANGED and event.facility_id in statuses:
            statuses[event.facility_id] = apply_status_event(statuses[event.facility_id], event)

        elif event.type is EventType.BEDS_REPORTED and event.facility_id is not None:
            hospital_id = event.facility_id
            bed_type = BedType(event.payload["bed_type"])
            beds_total[(hospital_id, bed_type)] = event.payload["total"]
            if hospital_id in statuses:
                statuses[hospital_id] = apply_status_event(statuses[hospital_id], event)
            _check_overbooked(hospital_id, bed_type, reserved, occupied, beds_total, violations, warnings, event.ts_ms, "beds_reported")

        elif event.type is EventType.CANDIDATE_DECLINED:
            reason = event.payload.get("reason") or "unknown"
            declines_by_reason[reason] = declines_by_reason.get(reason, 0) + 1

        elif (
            event.type is EventType.ACK_RECEIVED
            and event.payload.get("ack_type") == "READY"
            and event.transport_id in patients
        ):
            _check_valid_acceptance(
                event, command_hospital, hospitals, statuses, reserved, occupied, patients, saturation_limit, violations
            )

        if event.type in (EventType.BED_RESERVED, EventType.BED_RELEASED, EventType.BED_OCCUPIED, EventType.BEDS_REPORTED):
            hospital_id = event.payload.get("hospital_id") or event.facility_id
            if hospital_id in hospitals:
                load = _current_load(hospital_id, reserved, occupied, beds_total)
                max_load_per_hospital[hospital_id] = max(max_load_per_hospital.get(hospital_id, 0.0), load)

    # The breach warnings above are raised the moment capacity drops, before
    # R18's rebalance has had a chance to displace anyone — which the comment
    # in _check_overbooked acknowledges. Re-evaluate them against the final
    # state and drop the ones that did resolve: a decommission always dips
    # into breach on its way to zero beds, so leaving them in would report
    # every successful retirement as "UNRESOLVED" and make the invariant
    # panel cry wolf.
    unresolved = set()
    for warning in warnings:
        _, hospital_id, bed_type_value = warning.split(":", 2)
        key = (hospital_id, BedType(bed_type_value))
        count = len(reserved.get(key, set())) + len(occupied.get(key, set()))
        if count > beds_total.get(key, 0):
            unresolved.add(warning)
    return violations, sorted(unresolved), max_load_per_hospital, declines_by_reason


def _check_overbooked(
    hospital_id: str,
    bed_type: BedType,
    reserved: dict[tuple[str, BedType], set[str]],
    occupied: dict[tuple[str, BedType], set[str]],
    beds_total: dict[tuple[str, BedType], int],
    violations: list[Violation],
    warnings: set[str],
    ts_ms: int,
    cause: str,
) -> None:
    key = (hospital_id, bed_type)
    count = len(reserved.get(key, set())) + len(occupied.get(key, set()))
    total = beds_total.get(key, 0)
    if count <= total:
        return
    if cause == "beds_reported":
        # I2: a capacity drop with no (or insufficient) displacement — R18's
        # rebalance may still resolve this moments later; either way this is
        # a warning, never a handoff-safety violation.
        warnings.add(f"CAPACITY_BREACH_UNRESOLVED:{hospital_id}:{bed_type.value}")
    else:
        violations.append(
            Violation(
                "__hospital__", ts_ms, ViolationKind.OVERBOOKED,
                {"hospital_id": hospital_id, "bed_type": bed_type.value, "count": count, "total": total},
            )
        )


def _current_load(
    hospital_id: str,
    reserved: dict[tuple[str, BedType], set[str]],
    occupied: dict[tuple[str, BedType], set[str]],
    beds_total: dict[tuple[str, BedType], int],
) -> float:
    total = sum(total for (hid, _bt), total in beds_total.items() if hid == hospital_id)
    if total <= 0:
        return 0.0
    used = sum(
        len(reserved.get((hid, bt), set())) + len(occupied.get((hid, bt), set()))
        for (hid, bt) in beds_total
        if hid == hospital_id
    )
    return used / total


def _check_valid_acceptance(
    event: Event,
    command_hospital: dict[str, str],
    hospitals: dict[str, Hospital],
    statuses: dict,
    reserved: dict[tuple[str, BedType], set[str]],
    occupied: dict[tuple[str, BedType], set[str]],
    patients: dict[str, Patient],
    saturation_limit: float,
    violations: list[Violation],
) -> None:
    """I4. Note: eta_minutes can't be exactly reconstructed from the event
    log alone (a transport's position at the moment of READY isn't itself
    logged) — this re-checks capabilities/beds/specialists/diversion/
    saturation exactly, and approximates the transport-time window with 0
    (never the failing reason unless the hospital is outright out of
    window for everyone), so a false ARRIVED_AT... no, INVALID_ACCEPTANCE
    from the window check specifically would be the one gap; see the phase
    report."""
    command_id = event.payload.get("command_id")
    hospital_id = command_hospital.get(command_id) if command_id else None
    if hospital_id is None or hospital_id not in hospitals:
        return
    patient = patients[event.transport_id]
    hospital = hospitals[hospital_id]
    status = statuses[hospital_id]
    view = HospitalLedgerView(
        hospital_id=hospital_id,
        reserved={bt: frozenset(ids) for (hid, bt), ids in reserved.items() if hid == hospital_id},
        occupied={bt: frozenset(ids) for (hid, bt), ids in occupied.items() if hid == hospital_id},
        ventilator_holders=frozenset(),
    )
    decision = accept(patient, hospital, status, view, 0.0, saturation_limit)
    if not decision.accepted:
        violations.append(
            Violation(
                event.transport_id, event.ts_ms, ViolationKind.INVALID_ACCEPTANCE,
                {"hospital_id": hospital_id, "reason": decision.reason},
            )
        )
