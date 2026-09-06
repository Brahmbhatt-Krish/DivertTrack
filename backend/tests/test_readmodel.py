"""Phase 22: the hospital read model.

These tables are a projection, not a source of truth. The only tests that
really matter here are the two equivalence ones: the incremental projector must
agree with a from-scratch rebuild, and both must agree with the pure replay
functions the checker uses. If either drifts, the tables have quietly become a
second source of truth and everything downstream of them is suspect.
"""
import random

from app.clock import FakeClock
from app.config import Config
from app.events import EventStore
from app.models import BedType, Policy
from app.projection import free as ledger_free
from app.projection import load as ledger_load
from app.projection import project_hospitals, project_ledger
from app.readmodel import HospitalReadModel
from app.seed import MULTI_HOSPITALS, random_patient
from app.simulation import Simulation


def _config(**overrides) -> Config:
    base = dict(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:", policy=Policy.AUTO,
    )
    base.update(overrides)
    return Config(**base)


def _busy_simulation(seed: int = 0, waves: int = 3):
    """A log with the full spread of hospital events: registrations, bed
    reports, diversions, reservations, releases, occupancies, a retirement."""
    rng = random.Random(seed)
    clock, store = FakeClock(), EventStore(":memory:")
    sim = Simulation(clock, store, _config(), hospitals=None)
    for hospital in MULTI_HOSPITALS:
        sim.register_hospital(hospital)

    for wave in range(waves):
        sim.start_batch(
            [(random_patient(rng, f"W{wave}P{i}"), (rng.uniform(0, 40), rng.uniform(0, 40)))
             for i in range(6)]
        )
        clock.run_until_quiet()
        sim.report_beds(f"Hospital_{rng.randint(1, 6)}",
                        rng.choice([BedType.GENERAL, BedType.ICU]), rng.randint(0, 5))
        sim.report_hospital_status(
            f"Hospital_{rng.randint(1, 6)}",
            {"diversion": rng.choice(["OPEN", "PARTIAL", "FULL"]),
             "ed_saturation": round(rng.random(), 2)},
        )
        clock.run_until_quiet()
    sim.decommission_hospital("Hospital_3")
    clock.run_until_quiet()
    return sim, store


def test_incremental_projection_matches_a_full_rebuild() -> None:
    """The two write paths must be interchangeable. If they aren't, a restart
    silently changes what the API reports."""
    sim, store = _busy_simulation(seed=1)

    incremental = HospitalReadModel(store)
    for event in store.replay():
        incremental.apply(event)
    live = incremental.fetch_all(include_retired=True)

    rebuilt = HospitalReadModel(store)
    rebuilt.rebuild()
    assert rebuilt.fetch_all(include_retired=True) == live
    assert rebuilt.last_seq() == store.replay()[-1].seq


def test_the_tables_agree_with_the_pure_replay() -> None:
    """The read model exists to avoid replaying the whole log on every read.
    It has to produce the same answer the replay would, or it is just a faster
    way to be wrong."""
    sim, store = _busy_simulation(seed=2)
    model = HospitalReadModel(store)
    model.rebuild()

    events = store.replay()
    roster = project_hospitals(events)
    ledger = project_ledger(events, sim.dispatcher.known_patients())

    from_db = {view["id"]: view for view in model.fetch_all(include_retired=True)}
    assert set(from_db) == set(roster.known)
    active_ids = {view["id"] for view in model.fetch_all()}
    assert active_ids == set(roster.active)

    for hospital_id, hospital in roster.known.items():
        view = from_db[hospital_id]
        assert view["name"] == hospital.name
        assert view["location"] == [hospital.location[0], hospital.location[1]]

        status = sim.dispatcher.hospital_status_of(hospital_id)
        assert view["diversion"] == status.diversion.value
        assert view["ed_saturation"] == status.ed_saturation
        assert view["beds_total"] == {bt.value: n for bt, n in status.beds_total.items()}

        ledger_view = ledger.get(hospital_id)
        if ledger_view is None:
            continue
        for bed_type, total in status.beds_total.items():
            assert view["free"][bed_type.value] == ledger_free(ledger_view, status, bed_type), (
                f"{hospital_id} {bed_type.value}"
            )
        assert abs(view["load"] - ledger_load(ledger_view, status)) < 1e-9


def test_a_retired_hospital_is_kept_but_hidden_by_default() -> None:
    """I4 re-runs accept() over historical events, so a hospital that has since
    closed must still resolve — deleting the row would turn every valid
    decision it ever made into a phantom violation."""
    _, store = _busy_simulation(seed=3, waves=1)
    model = HospitalReadModel(store)
    model.rebuild()

    active = {view["id"] for view in model.fetch_all()}
    known = {view["id"] for view in model.fetch_all(include_retired=True)}
    assert "Hospital_3" not in active
    assert "Hospital_3" in known
    assert model.fetch("Hospital_3")["retired"] is True


def test_a_released_bed_stops_being_counted() -> None:
    """BedReleased means this transport holds nothing here any more —
    reservation or occupancy alike. Dropping only the reservation was a real
    bug in the ledger, and the tables must not reintroduce it."""
    from app.events import Event, EventType

    store = EventStore(":memory:")
    model = HospitalReadModel(store)
    store.subscribe(model.apply)
    store.append(Event(
        transport_id="H1", epoch=0, ts_ms=0, type=EventType.HOSPITAL_REGISTERED, facility_id="H1",
        payload={"id": "H1", "name": "H1", "location": [0.0, 0.0],
                 "beds_total": {"ICU": 2}, "capabilities": [], "ventilators_total": 0,
                 "max_eta_minutes": {}},
    ))
    for event_type in (EventType.BED_RESERVED, EventType.BED_OCCUPIED, EventType.BED_RELEASED):
        store.append(Event(
            transport_id="T1", epoch=1, ts_ms=0, type=event_type, facility_id="H1",
            payload={"hospital_id": "H1", "transport_id": "T1", "bed_type": "ICU"},
        ))

    view = model.fetch("H1")
    assert view["free"]["ICU"] == 2
    assert view["holders"]["ICU"] == {"reserved": [], "occupied": []}
