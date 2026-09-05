"""Phase 19: checker.py's I2 (no overbooking), I3 (arrival has a
reservation), I4 (every acceptance was valid)."""
from app.checker import ViolationKind, check
from app.events import Event, EventType
from app.models import AgeGroup, BedType, Capability, ConditionCategory, Hospital, Patient

HOSPITAL = Hospital(id="H1", name="H1", location=(0.0, 0.0), beds_total={BedType.ICU: 1},
                     capabilities=frozenset({Capability.TRAUMA_L1}), ventilators_total=1)
PATIENT = Patient(id="T1", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)


def _event(transport_id, event_type, payload, ts_ms=0, facility_id=None):
    return Event(transport_id=transport_id, epoch=1, ts_ms=ts_ms, type=event_type, facility_id=facility_id, payload=payload)


def test_i2_overbooking_from_a_bad_reserve_is_a_violation() -> None:
    events = [
        _event("T1", EventType.BED_RESERVED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=0),
        _event("T2", EventType.BED_RESERVED, {"hospital_id": "H1", "transport_id": "T2", "bed_type": "ICU"}, ts_ms=1),
    ]
    result = check(events, patients={"T1": PATIENT, "T2": PATIENT}, hospitals={"H1": HOSPITAL})
    assert any(v.kind == ViolationKind.OVERBOOKED for v in result.violations)


def test_i2_beds_reported_drop_is_a_warning_not_a_violation() -> None:
    events = [
        _event("T1", EventType.BED_RESERVED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=0),
        _event("H1", EventType.BEDS_REPORTED, {"bed_type": "ICU", "total": 0}, ts_ms=1, facility_id="H1"),
    ]
    result = check(events, patients={"T1": PATIENT}, hospitals={"H1": HOSPITAL})
    assert not any(v.kind == ViolationKind.OVERBOOKED for v in result.violations)
    assert any("CAPACITY_BREACH_UNRESOLVED" in w for w in result.capacity_warnings)


def test_e33_arrived_without_reservation_is_a_violation() -> None:
    events = [
        _event("T1", EventType.BED_RESERVED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=0),
        _event("T1", EventType.BED_RELEASED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=1),
        _event("T1", EventType.ARRIVED, {"at": "H1"}, ts_ms=2),
    ]
    result = check(events, patients={"T1": PATIENT}, hospitals={"H1": HOSPITAL})
    assert any(v.kind == ViolationKind.ARRIVED_WITHOUT_RESERVATION for v in result.violations)


def test_arrived_with_a_live_reservation_is_not_a_violation() -> None:
    events = [
        _event("T1", EventType.BED_RESERVED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=0),
        _event("T1", EventType.BED_OCCUPIED, {"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"}, ts_ms=1),
        _event("T1", EventType.ARRIVED, {"at": "H1"}, ts_ms=2),
    ]
    result = check(events, patients={"T1": PATIENT}, hospitals={"H1": HOSPITAL})
    assert not any(v.kind == ViolationKind.ARRIVED_WITHOUT_RESERVATION for v in result.violations)


def test_i4_a_forged_ready_for_an_unaccepted_patient_is_invalid() -> None:
    unaccepted = Patient(id="T1", acuity=2, condition=ConditionCategory.CARDIAC, age_group=AgeGroup.ADULT)  # H1 lacks CATH_LAB
    events = [
        _event("T1", EventType.COMMAND_SENT, {"command_id": "cmd-1", "kind": "command", "label": "PREPARE"}, ts_ms=0, facility_id="H1"),
        _event("T1", EventType.ACK_RECEIVED, {"command_id": "cmd-1", "ack_type": "READY"}, ts_ms=1),
    ]
    result = check(events, patients={"T1": unaccepted}, hospitals={"H1": HOSPITAL})
    assert any(v.kind == ViolationKind.INVALID_ACCEPTANCE for v in result.violations)


def test_declines_by_reason_tally() -> None:
    events = [
        _event("T1", EventType.CANDIDATE_DECLINED, {"hospital_id": "H1", "transport_id": "T1", "reason": "no_bed:ICU"}, ts_ms=0),
        _event("T2", EventType.CANDIDATE_DECLINED, {"hospital_id": "H1", "transport_id": "T2", "reason": "no_bed:ICU"}, ts_ms=1),
    ]
    result = check(events, patients={}, hospitals={"H1": HOSPITAL})
    assert result.declines_by_reason == {"no_bed:ICU": 2}


def test_capacity_checks_are_skipped_without_hospitals() -> None:
    # The plain 3-hospital demo's own tests never pass hospitals/patients —
    # capacity_warnings/max_load_per_hospital/declines_by_reason must stay
    # empty rather than erroring.
    result = check([])
    assert result.capacity_warnings == []
    assert result.max_load_per_hospital == {}
    assert result.declines_by_reason == {}
