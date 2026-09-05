"""Phase 19: the bed ledger — project_ledger's replay of BedReserved/
BedReleased/BedOccupied, and free()/load()/ventilators_free() (E41)."""
from app.events import Event, EventType
from app.models import AgeGroup, BedType, ConditionCategory, Diversion, HospitalStatus, Need, Patient, Specialist
from app.projection import empty_ledger_view, free, load, project_ledger, ventilators_free

HOSPITAL_ID = "H1"


def _status(beds_total: dict, ventilators_total: int = 5) -> HospitalStatus:
    return HospitalStatus(
        specialists_on_shift=frozenset(Specialist), ed_saturation=0.0, diversion=Diversion.OPEN,
        diverted_categories=frozenset(), beds_total=beds_total, ventilators_total=ventilators_total,
    )


def _reserved(transport_id: str, bed_type: BedType, hospital_id: str = HOSPITAL_ID, ts_ms: int = 0) -> Event:
    return Event(transport_id=transport_id, epoch=1, ts_ms=ts_ms, type=EventType.BED_RESERVED, facility_id=None,
                 payload={"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value})


def _released(transport_id: str, bed_type: BedType, hospital_id: str = HOSPITAL_ID, ts_ms: int = 0) -> Event:
    return Event(transport_id=transport_id, epoch=1, ts_ms=ts_ms, type=EventType.BED_RELEASED, facility_id=None,
                 payload={"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value})


def _occupied(transport_id: str, bed_type: BedType, hospital_id: str = HOSPITAL_ID, ts_ms: int = 0) -> Event:
    return Event(transport_id=transport_id, epoch=1, ts_ms=ts_ms, type=EventType.BED_OCCUPIED, facility_id=None,
                 payload={"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value})


def test_empty_ledger_view_reports_full_capacity() -> None:
    status = _status({BedType.ICU: 3})
    view = empty_ledger_view(HOSPITAL_ID)
    assert free(view, status, BedType.ICU) == 3
    assert load(view, status) == 0.0


def test_reserve_then_release_returns_the_bed():
    status = _status({BedType.ICU: 3})
    events = [_reserved("T1", BedType.ICU), _released("T1", BedType.ICU)]
    view = project_ledger(events, {})[HOSPITAL_ID]
    assert free(view, status, BedType.ICU) == 3


def test_reserve_then_occupy_keeps_the_bed_held_e41() -> None:
    status = _status({BedType.ICU: 3})
    events = [_reserved("T1", BedType.ICU), _occupied("T1", BedType.ICU)]
    view = project_ledger(events, {})[HOSPITAL_ID]
    assert free(view, status, BedType.ICU) == 2  # still held, now as occupied not reserved
    assert view.reserved.get(BedType.ICU, frozenset()) == frozenset()
    assert view.occupied.get(BedType.ICU) == frozenset({"T1"})


def test_load_is_fraction_of_all_bed_types_combined() -> None:
    status = _status({BedType.ICU: 2, BedType.GENERAL: 8})
    events = [_reserved("T1", BedType.ICU), _reserved("T2", BedType.GENERAL)]
    view = project_ledger(events, {})[HOSPITAL_ID]
    assert load(view, status) == 2 / 10


def test_ventilators_free_counts_only_patients_needing_one() -> None:
    status = _status({BedType.ICU: 5}, ventilators_total=2)
    patients = {
        "T1": Patient(id="T1", acuity=1, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT, needs=frozenset({Need.VENTILATOR})),
        "T2": Patient(id="T2", acuity=1, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT),
    }
    events = [_reserved("T1", BedType.ICU), _reserved("T2", BedType.ICU)]
    view = project_ledger(events, patients)[HOSPITAL_ID]
    assert ventilators_free(view, status) == 1


def test_two_hospitals_are_tracked_independently() -> None:
    events = [_reserved("T1", BedType.ICU, hospital_id="A"), _reserved("T2", BedType.ICU, hospital_id="B")]
    ledger = project_ledger(events, {})
    assert ledger["A"].reserved[BedType.ICU] == frozenset({"T1"})
    assert ledger["B"].reserved[BedType.ICU] == frozenset({"T2"})
