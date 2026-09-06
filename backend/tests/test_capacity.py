"""Phase 19: the dispatcher's R13-R20 (reservation, candidate iteration,
policy) — via a plain recording bus, the same style as the Phase 13 smoke
check, since these tests are about the dispatcher's own logic in isolation."""
from dataclasses import replace
from typing import Optional

import pytest

from app.clock import FakeClock
from app.config import Config
from app.dispatcher import Dispatcher, DispatcherStatus, NotEligible
from app.events import EventStore
from app.messages import Ack, AckType, FacilityState
from app.models import AgeGroup, BedType, ConditionCategory, Hospital, Patient, Policy
from app.projection import free


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> None:
        self.sent.append(message)


def _config(policy: Policy = Policy.MANUAL) -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:", policy=policy)


def _hospital(hospital_id: str, beds: int = 5, x: float = 0.0) -> Hospital:
    return Hospital(id=hospital_id, name=hospital_id, location=(x, 0.0), beds_total={BedType.ICU: beds}, capabilities=frozenset(), ventilators_total=2)


def _patient(patient_id: str = "P1") -> Patient:
    return Patient(id=patient_id, acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)


def _harness(hospitals: dict[str, Hospital], policy: Policy = Policy.MANUAL):
    clock, store, bus = FakeClock(), EventStore(":memory:"), RecordingBus()
    dispatcher = Dispatcher(clock, store, bus, _config(policy), hospitals=hospitals)
    return clock, store, bus, dispatcher


def _decline(dispatcher: Dispatcher, clock: FakeClock, transport_id: str, hospital_id: str, reason: str) -> None:
    record = dispatcher._transports[transport_id]
    dispatcher.on_ack(Ack(
        command_id=record.prepare_command_id, transport_id=transport_id, facility_id=hospital_id,
        epoch=record.pending_epoch, ack_type=AckType.DECLINED, applied_state=FacilityState.IDLE,
        sent_at_ms=clock.now_ms(), reason=reason,
    ))


def test_e28_last_bed_race_second_transport_is_not_eligible() -> None:
    hospital = _hospital("H1", beds=1)
    _, _, _, dispatcher = _harness({"H1": hospital})
    dispatcher.start("T1", destination="H1", patient=_patient("T1"), position=(0.0, 0.0))
    with pytest.raises(NotEligible) as excinfo:
        dispatcher.start("T2", destination="H1", patient=_patient("T2"), position=(0.0, 0.0))
    assert excinfo.value.reason == "no_bed:ICU"


def test_e30_every_candidate_declines_aborts_with_no_accepting_facility() -> None:
    hospitals = {h.id: h for h in (_hospital("H1", x=1), _hospital("H2", x=2))}
    clock, store, bus, dispatcher = _harness(hospitals)
    dispatcher.start("T1", destination=None, patient=_patient(), position=(0.0, 0.0))
    _decline(dispatcher, clock, "T1", "H1", "no_specialist:EMERGENCY")
    _decline(dispatcher, clock, "T1", "H2", "no_specialist:EMERGENCY")
    assert dispatcher.candidates_tried_of("T1") == ["H1", "H2"]
    assert dispatcher.current_destination_of("T1") is None
    no_accepting = [e for e in store.replay("T1") if e.type.value == "NoAcceptingFacility"]
    assert no_accepting


def test_e34_candidate_iteration_moves_to_next_under_same_epoch() -> None:
    hospitals = {h.id: h for h in (_hospital("H1", x=1), _hospital("H2", x=2))}
    clock, store, bus, dispatcher = _harness(hospitals)
    dispatcher.start("T1", destination=None, patient=_patient(), position=(0.0, 0.0))
    epoch_before = dispatcher._transports["T1"].pending_epoch
    _decline(dispatcher, clock, "T1", "H1", "no_specialist:EMERGENCY")
    assert dispatcher.pending_destination_of("T1") == "H2"
    assert dispatcher._transports["T1"].pending_epoch == epoch_before  # R15: same epoch, not a fresh redirect


def test_e35_decline_after_commitment_is_late_ignored() -> None:
    hospital = _hospital("H1")
    clock, store, bus, dispatcher = _harness({"H1": hospital})
    dispatcher.start("T1", destination="H1", patient=_patient(), position=(0.0, 0.0))
    record = dispatcher._transports["T1"]
    # Drive READY then RECEIVED-for-ACTIVATE_AT to reach "committed".
    dispatcher.on_ack(Ack(command_id=record.prepare_command_id, transport_id="T1", facility_id="H1", epoch=record.pending_epoch, ack_type=AckType.READY, applied_state=FacilityState.ARMED, sent_at_ms=clock.now_ms()))
    dispatcher.on_ack(Ack(command_id=record.activate_command_id, transport_id="T1", facility_id="H1", epoch=record.pending_epoch, ack_type=AckType.RECEIVED, applied_state=FacilityState.ARMED, sent_at_ms=clock.now_ms()))
    assert record.activate_command_id is not None

    # A forged/late DECLINED for the original PREPARE command_id now:
    dispatcher.on_ack(Ack(command_id=record.prepare_command_id, transport_id="T1", facility_id="H1", epoch=record.pending_epoch, ack_type=AckType.DECLINED, applied_state=FacilityState.ARMED, sent_at_ms=clock.now_ms(), reason="too_late"))
    late = [e for e in store.replay("T1") if e.type.value == "LateDeclineIgnored"]
    assert late
    assert dispatcher.pending_destination_of("T1") == "H1"  # unaffected


def test_r14_bed_is_released_on_abort() -> None:
    hospital = _hospital("H1")
    clock, store, bus, dispatcher = _harness({"H1": hospital})
    dispatcher.start("T1", destination="H1", patient=_patient(), position=(0.0, 0.0))
    record = dispatcher._transports["T1"]
    _decline(dispatcher, clock, "T1", "H1", "no_specialist:EMERGENCY")  # only candidate -> abort
    status = dispatcher.hospital_status_of("H1")
    view = dispatcher.ledger_view("H1")
    assert free(view, status, BedType.ICU) == 5  # released back to full capacity


def test_r17_auto_redirect_with_no_target_uses_choose() -> None:
    hospitals = {h.id: h for h in (_hospital("H1", x=1), _hospital("H2", x=2))}
    clock, store, bus, dispatcher = _harness(hospitals, policy=Policy.AUTO)
    dispatcher.start("T1", destination="H1", patient=_patient(), position=(0.0, 0.0))
    dispatcher.redirect("T1")  # no target: auto-chooses
    assert dispatcher.pending_destination_of("T1") in ("H1", "H2")


def test_manual_policy_rejects_a_redirect_with_no_target() -> None:
    hospital = _hospital("H1")
    clock, store, bus, dispatcher = _harness({"H1": hospital}, policy=Policy.MANUAL)
    dispatcher.start("T1", destination="H1", patient=_patient(), position=(0.0, 0.0))
    with pytest.raises(ValueError):
        dispatcher.redirect("T1")


def test_e33_an_ambulance_with_no_accepting_facility_stops_instead_of_arriving() -> None:
    """The dispatcher must never produce I3 (ARRIVED_WITHOUT_RESERVATION).

    Regression: the first candidate gets a REDIRECT_NOTICE before it has
    accepted, so the ambulance starts driving immediately. When that
    candidate then declines and no fallback exists, the transport is left
    with no destination — but the ambulance kept its known_destination,
    drove the whole way, and logged Arrived at the hospital that had
    already released its bed.
    """
    from app.checker import check
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.messages import FacilityState as _FacilityState
    from app.models import BedType as _BedType
    from app.simulation import Simulation

    hospital = Hospital(
        id="H1", name="H1", location=(0.0, 0.0), beds_total={_BedType.ICU: 1},
        capabilities=frozenset(), ventilators_total=0,
    )
    clock, store = _FakeClock(), _EventStore(":memory:")
    config = _config()
    sim = Simulation(clock, store, config, hospitals={"H1": hospital})

    patient = _patient("P1")  # acuity 2 -> needs an ICU bed
    sim.start_capacity_aware("T1", patient, position=(0.2, 0.0), destination="H1")

    # The facility's own live status has no ICU bed at all, so it declines
    # the PREPARE the dispatcher already optimistically reserved against.
    sim.facilities["H1"]._status = replace(
        sim.facilities["H1"].status_of(), beds_total={_BedType.ICU: 0}
    )
    clock.run_until_quiet()

    assert sim.dispatcher.status_of("T1") is DispatcherStatus.NO_ACCEPTING_FACILITY
    assert sim.ambulances["T1"].known_destination is None  # stood down, not still driving
    arrived = [e for e in store.replay("T1") if e.type.value == "Arrived"]
    assert arrived == []

    result = check(store.replay(), patients={"T1": patient}, hospitals={"H1": hospital})
    assert not [v for v in result.violations if v.kind.value == "ARRIVED_WITHOUT_RESERVATION"], result.violations


def test_a_capacity_aware_redirect_prepares_the_hospital_it_actually_named() -> None:
    """Regression: the redirect paths seeded the fallback queue with
    candidates[1:] and then popped index 0, so a redirect quietly prepared
    the *second*-ranked hospital while logging the first — and raised
    IndexError outright when there was only one candidate (every explicit
    single-target redirect, and R17's auto-redirect)."""
    hospitals = {h.id: h for h in (_hospital("H1", x=1), _hospital("H2", x=2))}
    clock, store, bus, dispatcher = _harness(hospitals)
    dispatcher.start("T1", destination="H1", patient=_patient(), position=(0.0, 0.0))

    # Finish the start so pending clears and a fresh redirect is possible.
    record = dispatcher._transports["T1"]
    dispatcher.on_ack(Ack(command_id=record.prepare_command_id, transport_id="T1", facility_id="H1", epoch=record.pending_epoch, ack_type=AckType.READY, applied_state=FacilityState.ARMED, sent_at_ms=clock.now_ms()))
    record = dispatcher._transports["T1"]
    dispatcher.on_ack(Ack(command_id=record.activate_command_id, transport_id="T1", facility_id="H1", epoch=record.pending_epoch, ack_type=AckType.APPLIED, applied_state=FacilityState.ACTIVE, sent_at_ms=clock.now_ms()))
    assert dispatcher.current_destination_of("T1") == "H1"

    # A single explicit target must not raise, and must target H2 itself.
    dispatcher.redirect("T1", "H2")
    assert dispatcher.pending_destination_of("T1") == "H2"
    assert dispatcher.candidates_tried_of("T1")[-1] == "H2"


def test_e33_a_stale_in_flight_notice_does_not_strand_an_ambulance() -> None:
    """Co-located hospitals that all decline: each candidate's REDIRECT_NOTICE
    is still crossing the bus when that candidate declines.

    Regression. This was an xfail for the whole capacity extension: R15 walks
    a candidate list under a single epoch, so the epoch fence cannot tell a
    notice for an already-declined candidate from the current one, and the
    late arrival re-pointed the ambulance at a hospital whose bed had been
    released — arriving there with no reservation (I3 / E33). Fixed by
    stamping a per-transport notice_seq on every REDIRECT_NOTICE and having
    stand_down() fence everything issued up to that point (see
    Ambulance.stand_down and Command.notice_seq).

    Co-location is what makes this bite reliably: with every hospital at the
    same point, the drive is short enough to finish before the protocol
    settles, so a stranded ambulance actually reaches its wrong destination
    instead of merely pointing at it."""
    import random as _random

    from app.checker import check
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.seed import random_patient
    from app.simulation import Simulation

    hospitals = {
        f"CH{i}": Hospital(
            id=f"CH{i}", name=f"CH{i}", location=(0.0, 0.0),
            beds_total={BedType.GENERAL: 1, BedType.ICU: 0},
            capabilities=frozenset(), ventilators_total=0,
        )
        for i in range(4)
    }
    config = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:", policy=Policy.MANUAL,
    )

    for trial in range(60):
        rng = _random.Random(trial)
        clock, store = _FakeClock(), _EventStore(":memory:")
        sim = Simulation(clock, store, config, hospitals=hospitals)
        sim.start_batch([(random_patient(rng, f"FP{i}"), (0.0, 0.0)) for i in range(3)])
        clock.run_until_quiet()
        result = check(store.replay(), patients=sim.dispatcher.known_patients(), hospitals=hospitals)
        stranded = [v for v in result.violations if v.kind.value == "ARRIVED_WITHOUT_RESERVATION"]
        assert not stranded, stranded


def test_an_abandoned_candidate_is_withdrawn_not_orphaned() -> None:
    """Moving to the next candidate must stand the previous one down.

    Regression. A candidate whose READY came back during a start has an
    ACTIVATE_AT already crossing the bus. If capacity moves and displaces the
    transport before that lands, the candidate still goes ACTIVE — and with no
    WITHDRAW it stays ACTIVE for a transport whose dispatcher has since
    committed elsewhere. The checker then sees the wrong facility active for
    that transport: I1 ZERO_ACTIVE.

    It also left activate_command_id set, so that candidate's late RECEIVED
    ack was routed as a live redirect and crashed on a cutover_at of None.
    """
    import random as _random

    from app.checker import check
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.models import BedType as _BedType
    from app.models import Policy as _Policy
    from app.seed import MULTI_HOSPITALS, random_patient
    from app.simulation import Simulation

    hospitals = {hospital.id: hospital for hospital in MULTI_HOSPITALS}
    config = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:", policy=_Policy.AUTO,
    )

    for trial in range(12):
        rng = _random.Random(trial)
        clock, store = _FakeClock(), _EventStore(":memory:")
        sim = Simulation(clock, store, config, hospitals=hospitals)
        sim.start_batch(
            [(random_patient(rng, f"P{i}"), (rng.uniform(0, 40), rng.uniform(0, 40))) for i in range(20)]
        )
        # Capacity churn *while transports are mid-handshake* is what makes a
        # candidate get abandoned after its ACTIVATE_AT has already been sent.
        for step in range(6):
            hospital_id = f"Hospital_{rng.randint(1, 6)}"
            bed_type = rng.choice([_BedType.GENERAL, _BedType.ICU])
            total = rng.randint(0, 3)
            clock.schedule(
                400 * (step + 1),
                lambda h=hospital_id, b=bed_type, t=total: sim.report_beds(h, b, t),
            )
        clock.run_until_quiet()

        result = check(store.replay(), patients=sim.dispatcher.known_patients())
        handoff = [
            v for v in result.violations if v.kind.value in ("ZERO_ACTIVE", "MULTI_ACTIVE")
        ]
        assert not handoff, f"trial {trial}: {handoff[:3]}"


def test_a_hospitals_last_bed_can_actually_be_used() -> None:
    """The dispatcher reserves optimistically *before* sending PREPARE, so by
    the time the facility evaluates that request its own reservation is
    already in the ledger. Counting it made free() return 0 and the hospital
    declined its own applicant — meaning no transport could ever be given a
    hospital's last bed of any type, and every hospital behaved as though it
    were one bed smaller than it reported.
    """
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.models import AgeGroup as _AgeGroup
    from app.models import BedType as _BedType
    from app.models import Capability as _Capability
    from app.models import ConditionCategory as _ConditionCategory
    from app.simulation import Simulation

    hospital = Hospital(
        id="H1", name="H1", location=(0.0, 0.0),
        beds_total={_BedType.ICU: 1},  # exactly one, and it is free
        capabilities=frozenset({_Capability.CT_SCAN}), ventilators_total=1,
    )
    patient = Patient(
        id="P1", acuity=2,  # acuity <= 2 -> needs that ICU bed
        condition=_ConditionCategory.GENERAL, age_group=_AgeGroup.ADULT,
        needs=frozenset(), override="none",
    )

    clock, store = _FakeClock(), _EventStore(":memory:")
    sim = Simulation(clock, store, _config(), hospitals={"H1": hospital})
    sim.start_batch([(patient, (0.0, 0.0))])
    clock.run_until_quiet()

    transport_id = next(iter(sim.dispatcher.known_patients()))
    declines = [
        event for event in store.replay(transport_id)
        if event.type.value == "CandidateDeclined"
    ]
    assert not declines, f"the only free bed was refused: {declines}"
    assert sim.dispatcher.current_destination_of(transport_id) == "H1"


def test_the_last_bed_is_still_only_given_to_one_transport() -> None:
    """The counterpart to the test above: excluding a transport's own
    reservation must not let two of them hold the same last bed."""
    from app.checker import check
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.models import AgeGroup as _AgeGroup
    from app.models import BedType as _BedType
    from app.models import Capability as _Capability
    from app.models import ConditionCategory as _ConditionCategory
    from app.simulation import Simulation

    hospital = Hospital(
        id="H1", name="H1", location=(0.0, 0.0), beds_total={_BedType.ICU: 1},
        capabilities=frozenset({_Capability.CT_SCAN}), ventilators_total=2,
    )

    def _icu_patient(patient_id: str) -> Patient:
        return Patient(
            id=patient_id, acuity=2, condition=_ConditionCategory.GENERAL,
            age_group=_AgeGroup.ADULT, needs=frozenset(), override="none",
        )

    clock, store = _FakeClock(), _EventStore(":memory:")
    sim = Simulation(clock, store, _config(), hospitals={"H1": hospital})
    sim.start_batch([(_icu_patient("P1"), (0.0, 0.0)), (_icu_patient("P2"), (0.0, 0.0))])
    clock.run_until_quiet()

    placed = [
        transport_id for transport_id in sim.dispatcher.known_patients()
        if sim.dispatcher.current_destination_of(transport_id) == "H1"
    ]
    assert len(placed) == 1, f"one bed, but {len(placed)} transports placed: {placed}"
    result = check(store.replay(), patients=sim.dispatcher.known_patients())
    assert not [v for v in result.violations if v.kind.value == "OVERBOOKED"], result.violations


def test_releasing_a_bed_gives_back_an_occupied_one_too() -> None:
    """BedReleased means "this transport no longer holds a bed here". Both the
    dispatcher's live mirror and project_ledger only ever discarded the
    *reservation*, so a released occupied bed stayed counted against the
    hospital for the rest of the log — the old hospital kept showing a bed in
    use after the patient had gone."""
    from app.events import Event as _Event
    from app.events import EventStore as _EventStore
    from app.events import EventType as _EventType
    from app.models import BedType as _BedType
    from app.projection import project_ledger

    store = _EventStore(":memory:")
    for event_type in (_EventType.BED_RESERVED, _EventType.BED_OCCUPIED, _EventType.BED_RELEASED):
        store.append(
            _Event(
                transport_id="T1", epoch=1, ts_ms=0, type=event_type, facility_id="H1",
                payload={"hospital_id": "H1", "transport_id": "T1", "bed_type": _BedType.ICU.value},
            )
        )

    view = project_ledger(store.replay(), {})["H1"]
    assert "T1" not in view.occupied.get(_BedType.ICU, frozenset()), "released bed still counted as occupied"
    assert "T1" not in view.reserved.get(_BedType.ICU, frozenset())


def test_redirecting_an_arrived_transport_frees_the_bed_it_leaves() -> None:
    """An operator moving a patient on to another hospital is a real thing to
    want, so this is allowed. What must not happen is the hospital they leave
    going on counting the bed: _release_bed only ever gave back a
    *reservation*, so a redirect after arrival left the old hospital's bed
    occupied forever.
    """
    from dataclasses import replace as _replace

    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.models import AgeGroup as _AgeGroup
    from app.models import ConditionCategory as _ConditionCategory
    from app.seed import MULTI_HOSPITALS
    from app.simulation import Simulation

    clock, store = _FakeClock(), _EventStore(":memory:")
    hospitals = {hospital.id: hospital for hospital in MULTI_HOSPITALS}
    # A real journey, not a zero-distance one: a patient generated on the
    # hospital's own coordinates arrives on the first tick, before the
    # handshake has even placed them, so the dispatcher never records the
    # arrival at all. min_travel_ms is what stops that in the running app.
    sim = Simulation(clock, store, _replace(_config(), min_travel_ms=2000.0), hospitals=hospitals)
    patient = Patient(
        id="P1", acuity=2, condition=_ConditionCategory.GENERAL, age_group=_AgeGroup.ADULT,
        needs=frozenset(), override="none",
    )
    sim.start_batch([(patient, (18.0, 13.0))])
    clock.run_until_quiet()

    transport_id = next(iter(sim.dispatcher.known_patients()))
    landed = sim.dispatcher.arrived_at_of(transport_id)
    assert landed, "expected the transport to have arrived"

    def holds(hospital_id: str) -> bool:
        holders = sim.hospital_view(hospital_id)["holders"]
        return any(
            transport_id in held["reserved"] or transport_id in held["occupied"]
            for held in holders.values()
        )

    assert holds(landed)
    elsewhere = next(
        hospital_id
        for hospital_id in hospitals
        if hospital_id != landed and sim.dispatcher.hospital_status_of(hospital_id) is not None
    )
    sim.redirect(transport_id, elsewhere)
    clock.run_until_quiet()

    assert sim.dispatcher.current_destination_of(transport_id) == elsewhere
    assert not holds(landed), f"{landed} is still holding a bed for a patient that left"
    assert holds(elsewhere)


def test_discharging_a_patient_frees_the_bed_they_were_in() -> None:
    """A treated patient leaves and their bed goes back into the pool.

    Capacity only: the transport stays where it is and the facility stays
    ACTIVE. Discharging is not a handoff — routing it through one would mean a
    cured patient briefly had no hospital at all, which is the exact state I1
    exists to forbid.
    """
    from dataclasses import replace as _replace

    from app.checker import check
    from app.clock import FakeClock as _FakeClock
    from app.events import EventStore as _EventStore
    from app.models import AgeGroup as _AgeGroup
    from app.models import ConditionCategory as _ConditionCategory
    from app.seed import MULTI_HOSPITALS
    from app.simulation import Simulation

    clock, store = _FakeClock(), _EventStore(":memory:")
    hospitals = {hospital.id: hospital for hospital in MULTI_HOSPITALS}
    sim = Simulation(clock, store, _replace(_config(), min_travel_ms=2000.0), hospitals=hospitals)
    patient = Patient(
        id="P1", acuity=2, condition=_ConditionCategory.GENERAL, age_group=_AgeGroup.ADULT,
        needs=frozenset(), override="none",
    )
    sim.start_batch([(patient, (18.0, 13.0))])
    clock.run_until_quiet()

    transport_id = next(iter(sim.dispatcher.known_patients()))
    landed = sim.dispatcher.arrived_at_of(transport_id)
    assert landed, "expected the transport to have arrived"

    def occupies(hospital_id: str) -> bool:
        holders = sim.hospital_view(hospital_id)["holders"]
        return any(transport_id in held["occupied"] for held in holders.values())

    assert occupies(landed)
    free_before = sim.hospital_view(landed)["free"]

    freed = sim.discharge(transport_id)
    assert freed is not None and freed[0] == landed
    assert not occupies(landed), "the bed was not given back"
    free_after = sim.hospital_view(landed)["free"]
    assert free_after[freed[1].value] == free_before[freed[1].value] + 1

    # The transport is a record of a journey, not a live claim on a bed.
    assert sim.dispatcher.current_destination_of(transport_id) == landed
    # And nothing about the handoff invariant moved.
    result = check(store.replay(), patients=sim.dispatcher.known_patients())
    assert result.passed, result.violations

    assert sim.discharge(transport_id) is None, "discharging twice must be a no-op"
