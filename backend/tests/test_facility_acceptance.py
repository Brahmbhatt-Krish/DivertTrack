"""Phase 19: F1 (acceptance on PREPARE) and F2 (decline_now)."""
import pytest

from app.bus import Bus
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore
from app.facility import Facility
from app.messages import ActionType, Command
from app.models import AgeGroup, BedType, Capability, ConditionCategory, Hospital, Patient, Specialist
from dataclasses import replace


def _config() -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:")


def _hospital() -> Hospital:
    return Hospital(id="H1", name="H1", location=(0.0, 0.0), beds_total={BedType.GENERAL: 2},
                     capabilities=frozenset({Capability.TRAUMA_L1}), ventilators_total=1)


def _patient(**overrides) -> Patient:
    defaults = dict(id="T1", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT)
    defaults.update(overrides)
    return Patient(**defaults)


def _prepare(transport_id: str, patient: Patient, eta: float = 5.0, epoch: int = 1) -> Command:
    return Command(
        command_id=f"cmd-{transport_id}", transport_id=transport_id, target_facility="H1", epoch=epoch,
        action=ActionType.PREPARE, sent_at_ms=0, eta_minutes=eta, patient=patient,
    )


def _facility():
    clock = FakeClock()
    store = EventStore(":memory:")
    bus = Bus(clock, store, d_max_ms=200, min_delay_ms=10, max_delay_ms=200)
    facility = Facility("H1", clock, store, bus, _config(), hospital=_hospital())
    bus.register_endpoint("H1", facility.receive_command)
    bus.register_dispatcher(lambda ack: None)  # acks aren't under test here, just mustn't crash delivery
    return clock, store, bus, facility


def test_prepare_accepted_arms_and_sends_ready() -> None:
    clock, store, bus, facility = _facility()
    facility.receive_command(_prepare("T1", _patient()))
    clock.advance(60)
    assert facility.state_of("T1").value == "ARMED"


def test_e34_prepare_declined_leaves_state_unchanged_and_logs_reason() -> None:
    clock, store, bus, facility = _facility()
    facility.receive_command(_prepare("T1", _patient(condition=ConditionCategory.CARDIAC)))  # H1 has no cath lab
    assert facility.state_of("T1").value == "IDLE"
    declined = [e for e in store.replay("T1") if e.payload.get("declined")]
    assert declined and declined[0].payload["declined"] == "no_capability:CATH_LAB"


def test_e40_declines_with_no_ventilator_even_with_a_free_bed() -> None:
    clock, store, bus, facility = _facility()
    from app.models import Need
    patient = _patient(condition=ConditionCategory.GENERAL, needs=frozenset({Need.VENTILATOR}))
    facility._status = replace(facility._status, ventilators_total=0)
    facility.receive_command(_prepare("T1", patient))
    assert facility.state_of("T1").value == "IDLE"


def test_f2_decline_now_before_commitment_succeeds() -> None:
    clock, store, bus, facility = _facility()
    facility.receive_command(_prepare("T1", _patient()))
    clock.advance(60)
    assert facility.state_of("T1").value == "ARMED"
    facility.decline_now("T1", "specialist called away")
    assert facility.state_of("T1").value == "WITHDRAWN"


def test_e35_decline_now_after_commitment_raises_committed() -> None:
    clock, store, bus, facility = _facility()
    facility.receive_command(_prepare("T1", _patient()))
    clock.advance(60)
    facility.receive_command(
        Command(command_id="cmd-activate", transport_id="T1", target_facility="H1", epoch=1,
                action=ActionType.ACTIVATE_AT, effective_at_ms=clock.now_ms() + 1000, sent_at_ms=clock.now_ms())
    )
    with pytest.raises(ValueError, match="committed"):
        facility.decline_now("T1", "too late")


def test_plain_facility_ignores_acceptance_entirely() -> None:
    # No `hospital=` given — must behave exactly as before Phase 12.
    clock = FakeClock()
    store = EventStore(":memory:")
    bus = Bus(clock, store, d_max_ms=200, min_delay_ms=10, max_delay_ms=200)
    facility = Facility("Hospital_A", clock, store, bus, _config())
    bus.register_endpoint("Hospital_A", facility.receive_command)
    bus.register_dispatcher(lambda ack: None)
    facility.receive_command(
        Command(command_id="cmd-1", transport_id="T1", target_facility="Hospital_A", epoch=1,
                action=ActionType.PREPARE, sent_at_ms=0)
    )
    clock.advance(60)
    assert facility.state_of("T1").value == "ARMED"
