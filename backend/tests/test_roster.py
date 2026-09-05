"""Phase 21: the hospital roster is event-sourced.

Until this phase, *which* hospitals existed was static config read at import
time — the one part of the system state that could not be reconstructed by
replaying the log. These tests pin down that it now can be, and that a
hospital can be added or retired while transports are in flight without
breaking the one-active-facility invariant.
"""
from app.checker import check
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore
from app.models import (
    AgeGroup,
    BedType,
    Capability,
    ConditionCategory,
    Hospital,
    Patient,
    Policy,
    hospital_from_payload,
    hospital_to_payload,
)
from app.projection import project_hospitals
from app.simulation import Simulation


def _config(**overrides) -> Config:
    base = dict(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:",
    )
    base.update(overrides)
    return Config(**base)


def _hospital(hospital_id: str, location: tuple[float, float]) -> Hospital:
    return Hospital(
        id=hospital_id, name=hospital_id, location=location,
        beds_total={BedType.GENERAL: 4, BedType.ICU: 2},
        capabilities=frozenset({Capability.CT_SCAN}), ventilators_total=1,
    )


def _patient(patient_id: str = "P1") -> Patient:
    return Patient(
        id=patient_id, acuity=3, condition=ConditionCategory.GENERAL,
        age_group=AgeGroup.ADULT, needs=frozenset(), override="none",
    )


# -- serialization ----------------------------------------------------------


def test_a_hospital_round_trips_through_an_event_payload() -> None:
    """Payloads are persisted as JSON text, so the frozenset of capabilities
    has to serialize in a stable order — otherwise replaying the same log
    twice would not rebuild an identical Hospital."""
    original = _hospital("H1", (3.0, 4.0))
    payload = hospital_to_payload(original)
    assert hospital_to_payload(original) == payload  # stable across calls
    assert hospital_from_payload(payload) == original


def test_an_unknown_capability_is_rejected_rather_than_silently_dropped() -> None:
    payload = hospital_to_payload(_hospital("H1", (0.0, 0.0)))
    payload["capabilities"] = ["NOT_A_CAPABILITY"]
    try:
        hospital_from_payload(payload)
    except ValueError:
        return
    raise AssertionError("a hospital must not come back quietly less capable than its own event")


# -- the projection ---------------------------------------------------------


def test_project_hospitals_folds_register_update_and_decommission() -> None:
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config())
    sim.register_hospital(_hospital("H1", (0.0, 0.0)))
    sim.register_hospital(_hospital("H2", (10.0, 0.0)))
    sim.update_hospital(
        Hospital(
            id="H1", name="Renamed", location=(1.0, 1.0),
            beds_total={BedType.GENERAL: 9}, capabilities=frozenset(), ventilators_total=0,
        )
    )
    sim.decommission_hospital("H2")

    roster = project_hospitals(store.replay())
    assert set(roster.active) == {"H1"}
    assert roster.active["H1"].name == "Renamed"
    assert roster.active["H1"].location == (1.0, 1.0)
    # A decommissioned hospital stays in `known` at its last configuration —
    # the checker needs it to audit the events from while it was open.
    assert set(roster.known) == {"H1", "H2"}
    assert roster.known["H2"].beds_total[BedType.GENERAL] == 4


def test_the_roster_survives_a_rebuild_from_the_log_alone() -> None:
    """E21: rebuilding from nothing but the log must reproduce the network."""
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config())
    for index in range(3):
        sim.register_hospital(_hospital("H%d" % index, (float(index) * 5, 0.0)))
    sim.decommission_hospital("H1")

    rebuilt = Simulation(clock, EventStore(":memory:"), _config())
    for hospital in project_hospitals(store.replay()).active.values():
        rebuilt.register_hospital(hospital, log=False)
    assert set(rebuilt.hospitals) == {"H0", "H2"}


def test_registering_from_the_log_does_not_re_append_history() -> None:
    """Bootstrap replays an existing roster; re-logging it would double every
    hospital's registration on each restart."""
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config())
    sim.register_hospital(_hospital("H1", (0.0, 0.0)), log=False)
    assert store.replay() == []
    assert "H1" in sim.hospitals


# -- a hospital added at runtime is a real member of the network ------------


def test_a_hospital_added_at_runtime_is_immediately_dispatchable() -> None:
    """Registration used to be construction-only. Three things must happen
    together or the new hospital is a ghost that wins rankings and never
    answers: a Facility, its bus endpoint, and a dispatcher status entry."""
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config(), hospitals={"H1": _hospital("H1", (40.0, 40.0))})

    sim.register_hospital(_hospital("H2", (0.0, 0.0)))
    assert "H2" in sim.facilities, "no Facility was built"
    # Dispatcher._statuses used to be eagerly keyed from the startup roster,
    # and scoring.rank indexes it unguarded — a KeyError waiting to happen.
    assert sim.dispatcher.hospital_status_of("H2") is not None

    sim.start_batch([(_patient(), (0.0, 0.0))])
    clock.run_until_quiet()
    transport_id = next(iter(sim.dispatcher.known_patients()))
    assert sim.dispatcher.current_destination_of(transport_id) == "H2", "the nearer new hospital should win"


# -- the demo: decommission with transports in flight -----------------------


def test_decommissioning_reroutes_transports_and_keeps_the_invariant() -> None:
    """Closing a hospital is expressed as its capacity going to zero, so the
    existing R18 rebalance re-routes the transports holding its beds through
    the ordinary redirect protocol — the invariant is preserved by the same
    machinery that preserves it everywhere else."""
    clock, store = FakeClock(), EventStore(":memory:")
    hospitals = {"H1": _hospital("H1", (0.0, 0.0)), "H2": _hospital("H2", (2.0, 0.0))}
    sim = Simulation(clock, store, _config(policy=Policy.AUTO), hospitals=hospitals)

    sim.start_batch([(_patient("P%d" % i), (0.0, 0.0)) for i in range(2)])
    clock.run_until_quiet()
    heading_to_h1 = [
        transport_id
        for transport_id in sim.dispatcher.known_patients()
        if sim.dispatcher.current_destination_of(transport_id) == "H1"
    ]
    assert heading_to_h1, "expected the nearer hospital to be chosen first"

    sim.decommission_hospital("H1")
    clock.run_until_quiet()

    assert "H1" not in sim.hospitals
    still_there = [
        transport_id
        for transport_id in heading_to_h1
        if sim.dispatcher.current_destination_of(transport_id) == "H1"
    ]
    assert not still_there, "a decommissioned hospital must not remain anyone's destination"

    result = check(store.replay(), patients=sim.dispatcher.known_patients())
    critical = [
        v for v in result.violations
        if v.kind.value in ("ZERO_ACTIVE", "MULTI_ACTIVE", "OVERBOOKED", "ARRIVED_WITHOUT_RESERVATION")
    ]
    assert not critical, critical


def test_the_checker_audits_history_against_the_roster_as_it_was() -> None:
    """I4 re-runs accept() over historical events. A hospital decommissioned
    since must still resolve, or every valid acceptance it ever made becomes a
    phantom violation and every bed it held a phantom overbooking."""
    clock, store = FakeClock(), EventStore(":memory:")
    hospitals = {"H1": _hospital("H1", (0.0, 0.0)), "H2": _hospital("H2", (2.0, 0.0))}
    sim = Simulation(clock, store, _config(policy=Policy.AUTO), hospitals=hospitals)
    sim.start_batch([(_patient(), (0.0, 0.0))])
    clock.run_until_quiet()
    sim.decommission_hospital("H1")
    clock.run_until_quiet()

    # check() with no `hospitals=` derives the roster from the log itself.
    result = check(store.replay(), patients=sim.dispatcher.known_patients())
    assert not [
        v for v in result.violations if v.kind.value == "INVALID_ACCEPTANCE"
    ], result.violations


# -- restart fidelity -------------------------------------------------------


def test_hospital_status_is_restored_from_the_log_not_just_the_roster() -> None:
    """Registration restores *which* hospitals exist; this restores what state
    they are in. Without it the running system believed every hospital was
    wide open after a restart while checker.py — which does replay the whole
    log — knew better, and the two disagreed about whether an acceptance was
    legal (a live INVALID_ACCEPTANCE storm)."""
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config())
    sim.register_hospital(_hospital("H1", (0.0, 0.0)))
    sim.report_hospital_status("H1", {"diversion": "FULL"})
    sim.report_beds("H1", BedType.GENERAL, 1)

    # Rebuild the way main.py's bootstrap does: same store, fresh Simulation.
    rebuilt = Simulation(clock, store, _config())
    for hospital in project_hospitals(store.replay()).active.values():
        rebuilt.register_hospital(hospital, log=False)
    rebuilt.restore_hospital_status_from_log()

    status = rebuilt.dispatcher.hospital_status_of("H1")
    assert status.diversion.value == "FULL", "diversion must survive a restart"
    assert status.beds_total[BedType.GENERAL] == 1, "reported beds must survive a restart"
