"""Phase 14 smoke check (build-only phase — no tests here; see the phase
report). Run with:

    python scripts/phase14_smoke_check.py

Unlike Phase 13's smoke check (which injected DECLINED acks by hand), this
one runs DECLINE_CHAIN through real Facility instances: F1 makes them decide
DECLINED/accepted for themselves, on their own live (possibly stale-relative-
to-the-dispatcher) status.
"""
from __future__ import annotations

from app.bus import Bus
from app.clock import FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import EventStore
from app.facility import Facility
from app.models import AgeGroup, BedType, Capability, ConditionCategory, Hospital, Patient, Specialist


def _config() -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:")


def decline_chain() -> None:
    print("=== DECLINE_CHAIN (real facilities): H1 no_specialist, H2 no_bed, H3 accepts ===")
    h1 = Hospital(id="H1", name="H1", location=(1.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L1}), ventilators_total=2)
    h2 = Hospital(id="H2", name="H2", location=(2.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L2}), ventilators_total=2)
    h3 = Hospital(id="H3", name="H3", location=(3.0, 0.0), beds_total={BedType.GENERAL: 5},
                  capabilities=frozenset({Capability.TRAUMA_L3}), ventilators_total=2)
    hospitals = {h.id: h for h in (h1, h2, h3)}

    clock, store, config = FakeClock(), EventStore(":memory:"), _config()
    bus = Bus(clock, store, d_max_ms=config.d_max_ms, min_delay_ms=10, max_delay_ms=config.d_max_ms)

    facilities = {h.id: Facility(h.id, clock, store, bus, config, hospital=h) for h in (h1, h2, h3)}
    for facility in facilities.values():
        bus.register_endpoint(facility.id, facility.receive_command)

    dispatcher = Dispatcher(clock, store, bus, config, hospitals=hospitals)
    bus.register_dispatcher(dispatcher.on_ack)
    # A real ambulance would ack REDIRECT_NOTICE (R3's other cutover
    # precondition) — irrelevant to this smoke check (candidate iteration,
    # not cutover), so a no-op stub just satisfies the bus's routing.
    bus.register_endpoint("AMB-1", lambda command: None)

    # Make the dispatcher's own (optimistic) view see all three as fine —
    # its cached statuses default to fully staffed/open/full-capacity — but
    # give H1 and H2's *real* facility-side status a deficiency the
    # dispatcher doesn't know about yet, matching DECLINE_CHAIN's story: the
    # facility's live status is authoritative and can be stricter than the
    # dispatcher's last-known view.
    facilities["H1"]._status = _without_specialist(facilities["H1"].status_of(), Specialist.TRAUMA_SURGERY)
    facilities["H2"]._status = _without_beds(facilities["H2"].status_of(), BedType.GENERAL)

    patient = Patient(id="P1", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT)
    dispatcher.start("AMB-1", destination=None, patient=patient, position=(0.0, 0.0))
    clock.run_until_quiet()

    print(f"candidates tried, in order: {dispatcher.candidates_tried_of('AMB-1')} (expected ['H1', 'H2', 'H3'])")
    print(f"final status={dispatcher.status_of('AMB-1')} current={dispatcher.current_destination_of('AMB-1')}")
    for event in store.replay("AMB-1"):
        if event.type.value in ("CandidateDeclined", "FacilityStateChanged") and event.payload.get("declined"):
            print(f"  t={event.ts_ms} {event.facility_id}: declined:{event.payload.get('declined') or event.payload.get('reason')}")
        elif event.type.value == "CandidateDeclined":
            print(f"  t={event.ts_ms} CandidateDeclined {event.payload}")

    print("\n=== F2: decline_now() after commitment raises ValueError('committed') ===")
    h3_facility = facilities["H3"]
    try:
        h3_facility.decline_now("AMB-1", "changed our mind")
        print("UNEXPECTED: decline_now() did not raise")
    except ValueError as exc:
        print(f"Correctly raised: {exc}")


def _without_specialist(status, specialist):
    from dataclasses import replace
    return replace(status, specialists_on_shift=status.specialists_on_shift - {specialist})


def _without_beds(status, bed_type):
    from dataclasses import replace
    beds = dict(status.beds_total)
    beds[bed_type] = 0
    return replace(status, beds_total=beds)


if __name__ == "__main__":
    decline_chain()
