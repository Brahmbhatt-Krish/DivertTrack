"""Phase 19: R18 (capacity rebalance) — E29 (least-critical-first
displacement) and E32 (one hospital active for several transports;
redirecting one away leaves the others untouched)."""
from app.clock import FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import EventStore
from app.models import AgeGroup, BedType, ConditionCategory, Hospital, Patient, Policy


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> None:
        self.sent.append(message)


def _config(policy: Policy = Policy.MANUAL) -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:", policy=policy)


def _hospital(beds: int = 4) -> Hospital:
    return Hospital(id="H1", name="H1", location=(0.0, 0.0), beds_total={BedType.ICU: beds}, capabilities=frozenset(), ventilators_total=0)


def _patient(patient_id: str, acuity: int) -> Patient:
    # acuity<=2 is what required_bed_type() maps to ICU — every patient here
    # must stay in that range so they all compete for the *same* bed type;
    # R18's own ordering (acuity desc, then most-recent-reservation) is what
    # the test is about, not required_bed_type's routing.
    return Patient(id=patient_id, acuity=acuity, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)


def test_e29_beds_reported_drop_displaces_the_least_critical_first() -> None:
    hospital = _hospital(beds=3)
    clock, store, bus = FakeClock(), EventStore(":memory:"), RecordingBus()
    dispatcher = Dispatcher(clock, store, bus, _config(), hospitals={"H1": hospital})

    # T0 and T2 tie on acuity (2) — T2 reserved later, so it's the one R18
    # picks between them; T1 (acuity 1, most critical) must never be touched.
    for i, acuity in enumerate([2, 1, 2]):
        dispatcher.start(f"T{i}", destination="H1", patient=_patient(f"T{i}", acuity), position=(0.0, 0.0))

    dispatcher.on_beds_reported("H1", BedType.ICU, 2)  # deficit of 1

    displaced_events = [e for e in store.replay() if e.type.value == "CapacityRebalance"]
    assert displaced_events
    displaced = displaced_events[0].payload["displaced"]
    assert displaced == ["T2"]  # least critical of the tie, most recently reserved
    assert dispatcher.current_destination_of("T1") is None and dispatcher.pending_destination_of("T1") == "H1"  # untouched


def test_e32_one_hospital_active_for_three_transports_independently() -> None:
    hospital = _hospital(beds=5)
    clock, store, bus = FakeClock(), EventStore(":memory:"), RecordingBus()
    dispatcher = Dispatcher(clock, store, bus, _config(), hospitals={"H1": hospital})
    for i in range(3):
        dispatcher.start(f"T{i}", destination="H1", patient=_patient(f"T{i}", 2), position=(0.0, 0.0))

    assert dispatcher.pending_destination_of("T0") == "H1"
    assert dispatcher.pending_destination_of("T1") == "H1"
    assert dispatcher.pending_destination_of("T2") == "H1"

    # Redirecting T1 away (declining its only candidate) must not touch T0/T2.
    record = dispatcher._transports["T1"]
    from app.messages import Ack, AckType, FacilityState
    dispatcher.on_ack(Ack(
        command_id=record.prepare_command_id, transport_id="T1", facility_id="H1",
        epoch=record.pending_epoch, ack_type=AckType.DECLINED, applied_state=FacilityState.IDLE,
        sent_at_ms=clock.now_ms(), reason="test",
    ))
    assert dispatcher.pending_destination_of("T0") == "H1"
    assert dispatcher.pending_destination_of("T2") == "H1"
    assert dispatcher.current_destination_of("T1") is None
