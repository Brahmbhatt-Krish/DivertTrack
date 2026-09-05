"""Phase 19: F4 — status drift emits its timeline deterministically and
updates the matching Facility's live status."""
from app.bus import Bus
from app.clock import FakeClock
from app.config import Config
from app.drift import BedDrift, StatusDrift, schedule_drift
from app.events import EventStore
from app.facility import Facility
from app.models import BedType, Diversion, Hospital, Specialist


def _config() -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:")


def _hospital() -> Hospital:
    return Hospital(id="H1", name="H1", location=(0.0, 0.0), beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=2)


def test_status_drift_updates_the_facilitys_live_status_at_the_scheduled_time() -> None:
    clock, store = FakeClock(), EventStore(":memory:")
    bus = Bus(clock, store, d_max_ms=200, min_delay_ms=10, max_delay_ms=200)
    facility = Facility("H1", clock, store, bus, _config(), hospital=_hospital())

    schedule_drift(clock, store, {"H1": facility}, status_drifts=[StatusDrift(at_ms=500, hospital_id="H1", changes={"diversion": "FULL"})])
    assert facility.status_of().diversion is Diversion.OPEN  # not yet
    clock.advance(500)
    assert facility.status_of().diversion is Diversion.FULL


def test_bed_drift_updates_beds_total_deterministically() -> None:
    clock, store = FakeClock(), EventStore(":memory:")
    bus = Bus(clock, store, d_max_ms=200, min_delay_ms=10, max_delay_ms=200)
    facility = Facility("H1", clock, store, bus, _config(), hospital=_hospital())

    schedule_drift(clock, store, {"H1": facility}, bed_drifts=[BedDrift(at_ms=300, hospital_id="H1", bed_type=BedType.GENERAL, total=1)])
    clock.advance(300)
    assert facility.status_of().beds_total[BedType.GENERAL] == 1
    events = [e for e in store.replay() if e.type.value == "BedsReported"]
    assert len(events) == 1  # deterministic: exactly the one scripted drop, nothing random


def test_specialist_off_shift_drift_removes_exactly_that_specialist() -> None:
    clock, store = FakeClock(), EventStore(":memory:")
    bus = Bus(clock, store, d_max_ms=200, min_delay_ms=10, max_delay_ms=200)
    facility = Facility("H1", clock, store, bus, _config(), hospital=_hospital())
    remaining = frozenset(Specialist) - {Specialist.CARDIOLOGY}

    schedule_drift(
        clock, store, {"H1": facility},
        status_drifts=[StatusDrift(at_ms=100, hospital_id="H1", changes={"specialists_on_shift": [s.value for s in remaining]})],
    )
    clock.advance(100)
    assert Specialist.CARDIOLOGY not in facility.status_of().specialists_on_shift
