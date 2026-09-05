"""Phase 13 smoke check (build-only phase — no tests here; see the phase
report). Run with:

    python scripts/phase13_smoke_check.py

Exercises the dispatcher's new R13-R20 logic in isolation, via a plain
RecordingBus rather than the real Simulation/Facility pipeline — Phase 14 is
what teaches Facility to actually decide DECLINED/accepted on its own live
status; here, DECLINED acks are constructed by hand to prove the
dispatcher's own reservation/candidate-iteration/policy logic is correct
before that real integration exists.
"""
from __future__ import annotations

from app.clock import FakeClock
from app.config import Config
from app.dispatcher import Dispatcher, NotEligible
from app.events import EventStore
from app.messages import Ack, AckType
from app.messages import FacilityState
from app.models import AgeGroup, BedType, Capability, ConditionCategory, Hospital, Patient, Policy
from app.projection import free, project_ledger


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> None:
        self.sent.append(message)


def _config(policy: Policy = Policy.MANUAL) -> Config:
    return Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100,
        db_path=":memory:", policy=policy,
    )


def last_bed_race() -> None:
    print("=== LAST_BED_RACE: 2 transports, 1 ICU bed ===")
    hospital = Hospital(
        id="Hospital_1", name="Only Hospital", location=(0.0, 0.0),
        beds_total={BedType.ICU: 1, BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=2,
    )
    clock, store, bus = FakeClock(), EventStore(":memory:"), RecordingBus()
    dispatcher = Dispatcher(clock, store, bus, _config(), hospitals={hospital.id: hospital})
    patient1 = Patient(id="P1", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)
    patient2 = Patient(id="P2", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)
    position = (0.0, 0.0)

    dispatcher.start("AMB-1", destination=hospital.id, patient=patient1, position=position)
    print(f"AMB-1 reserved the ICU bed: status={dispatcher.status_of('AMB-1')}")

    try:
        dispatcher.start("AMB-2", destination=hospital.id, patient=patient2, position=position)
        print("UNEXPECTED: AMB-2 was accepted too")
    except NotEligible as exc:
        print(f"AMB-2 correctly rejected by R13's reserve-before-prepare gate: reason={exc.reason}")

    ledger = project_ledger(store.replay(), {"AMB-1": patient1})
    view = ledger.get(hospital.id)
    status = dispatcher.hospital_status_of(hospital.id)
    print(f"free(ICU) after the race: {free(view, status, BedType.ICU)} (expected 0)")


def decline_chain() -> None:
    print("\n=== DECLINE_CHAIN: candidate 1 and 2 decline, candidate 3 accepts ===")
    h1 = Hospital(id="H1", name="H1", location=(1.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L1}), ventilators_total=2)
    h2 = Hospital(id="H2", name="H2", location=(2.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L2}), ventilators_total=2)
    h3 = Hospital(id="H3", name="H3", location=(3.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L3}), ventilators_total=2)
    hospitals = {h.id: h for h in (h1, h2, h3)}
    clock, store, bus = FakeClock(), EventStore(":memory:"), RecordingBus()
    dispatcher = Dispatcher(clock, store, bus, _config(), hospitals=hospitals)
    patient = Patient(id="P1", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT)
    position = (0.0, 0.0)  # H1 closest -> tried first, then H2, then H3

    dispatcher.start("AMB-1", destination=None, patient=patient, position=position)
    record = dispatcher._transports["AMB-1"]  # smoke-check script only: peeking at internal state to drive acks
    print(f"tried first: {record.candidates_tried} (expected ['H1'])")

    def decline(hospital_id: str, reason: str) -> None:
        record = dispatcher._transports["AMB-1"]
        ack = Ack(
            command_id=record.prepare_command_id, transport_id="AMB-1", facility_id=hospital_id,
            epoch=record.pending_epoch, ack_type=AckType.DECLINED, applied_state=FacilityState.IDLE,
            sent_at_ms=clock.now_ms(), reason=reason,
        )
        dispatcher.on_ack(ack)

    decline("H1", "no_specialist:TRAUMA_SURGERY")
    print(f"after H1 declines: pending={dispatcher.pending_destination_of('AMB-1')} tried={dispatcher.candidates_tried_of('AMB-1')}")

    decline("H2", "no_bed:GENERAL")
    print(f"after H2 declines: pending={dispatcher.pending_destination_of('AMB-1')} tried={dispatcher.candidates_tried_of('AMB-1')}")

    # H3 accepts for real: READY, then the ambulance's notice-APPLIED (R3's
    # other precondition), reaching CUTOVER_SCHEDULED.
    record = dispatcher._transports["AMB-1"]
    dispatcher.on_ack(Ack(
        command_id=record.prepare_command_id, transport_id="AMB-1", facility_id="H3",
        epoch=record.pending_epoch, ack_type=AckType.READY, applied_state=FacilityState.ARMED,
        sent_at_ms=clock.now_ms(),
    ))
    dispatcher.on_ack(Ack(
        command_id=record.notice_command_id, transport_id="AMB-1", facility_id="AMB-1",
        epoch=record.pending_epoch, ack_type=AckType.APPLIED, applied_state=FacilityState.ACTIVE,
        sent_at_ms=clock.now_ms(),
    ))
    # R10's finalize step: READY (above) made the dispatcher send ACTIVATE_AT
    # immediately (no "old facility" to coordinate against on a first
    # placement) — its own APPLIED is what flips current_destination/STABLE.
    record = dispatcher._transports["AMB-1"]
    dispatcher.on_ack(Ack(
        command_id=record.activate_command_id, transport_id="AMB-1", facility_id="H3",
        epoch=record.pending_epoch, ack_type=AckType.APPLIED, applied_state=FacilityState.ACTIVE,
        sent_at_ms=clock.now_ms(),
    ))
    print(f"after H3 accepts: status={dispatcher.status_of('AMB-1')} current={dispatcher.current_destination_of('AMB-1')}")

    ledger = project_ledger(store.replay(), {"AMB-1": patient})
    for hospital_id in ("H1", "H2", "H3"):
        view = ledger.get(hospital_id)
        status = dispatcher.hospital_status_of(hospital_id)
        print(f"  {hospital_id}: free(GENERAL)={free(view, status, BedType.GENERAL)}")


if __name__ == "__main__":
    last_bed_race()
    decline_chain()
