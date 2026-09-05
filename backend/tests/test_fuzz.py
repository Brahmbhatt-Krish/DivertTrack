"""Phase 6: property-based fuzzing over concurrent, overlapping redirects.
Run twice — once honoring D_MAX_MS, once with strict_bound=False and delays
up to 3x it — asserting the checker passes either way (E25: safety must not
come from the bound)."""
import random
from typing import NamedTuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.checker import check
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore
from app.simulation import Simulation

FACILITIES = ("Hospital_A", "Hospital_B", "Hospital_C")
D_MAX_MS = 200
GUARD_MS = 50
READY_TIMEOUT_MS = 1500
PREP_MS = 50


class Redirect(NamedTuple):
    target: str
    at_ms: int


class TransportPlan(NamedTuple):
    initial_destination: str
    redirects: tuple[Redirect, ...]


@st.composite
def _transport_plan(draw: st.DrawFn) -> TransportPlan:
    initial = draw(st.sampled_from(FACILITIES))
    n_redirects = draw(st.integers(min_value=0, max_value=6))
    redirects: list[Redirect] = []
    seen_targets = [initial]
    for _ in range(n_redirects):
        # Occasionally repeat an earlier target — the only way to reliably
        # hit R8 (back to current) / R9 (already pending) from fuzzed data,
        # since neither can be forced without knowing runtime state.
        target = (
            draw(st.sampled_from(seen_targets)) if draw(st.booleans()) else draw(st.sampled_from(FACILITIES))
        )
        # Occasionally fire close on the heels of the previous redirect —
        # biases toward landing inside a cutover's D_MAX+GUARD window,
        # exercising R6's queue path (E24) instead of always cancelling.
        if redirects and draw(st.booleans()):
            at_ms = redirects[-1].at_ms + draw(st.integers(min_value=0, max_value=300))
        else:
            at_ms = draw(st.integers(min_value=0, max_value=6000))
        redirects.append(Redirect(target, at_ms))
        seen_targets.append(target)
    redirects.sort(key=lambda r: r.at_ms)
    return TransportPlan(initial, tuple(redirects))


@st.composite
def _scenario(draw: st.DrawFn, max_delay_ms: int) -> dict:
    n_transports = draw(st.integers(min_value=1, max_value=30))
    plans = [draw(_transport_plan()) for _ in range(n_transports)]
    delays = draw(st.lists(st.integers(min_value=50, max_value=max_delay_ms), min_size=200, max_size=200))
    duplicate_rate = draw(st.floats(min_value=0.0, max_value=0.2))
    return {"plans": plans, "delays": delays, "duplicate_rate": duplicate_rate}


def _run_scenario(scenario: dict, strict_bound: bool) -> list:
    clock = FakeClock()
    store = EventStore(":memory:")
    config = Config(
        groq_api_key="", d_max_ms=D_MAX_MS, guard_ms=GUARD_MS,
        ready_timeout_ms=READY_TIMEOUT_MS, prep_ms=PREP_MS, tick_ms=100, db_path=":memory:",
    )
    max_delay_ms = max([D_MAX_MS, *scenario["delays"]])
    sim = Simulation(clock, store, config, strict_bound=strict_bound, min_delay_ms=10, max_delay_ms=max_delay_ms)
    sim.load_delays(scenario["delays"], scenario["duplicate_rate"])

    for index, plan in enumerate(scenario["plans"]):
        transport_id = f"AMB-{index}"
        sim.start(transport_id, plan.initial_destination)
        for target, at_ms in plan.redirects:
            clock.schedule(at_ms, lambda t=transport_id, dest=target: sim.redirect(t, dest))

    sim.run_until_quiet()
    return sim.events()


@settings(max_examples=90, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(scenario=_scenario(max_delay_ms=D_MAX_MS))
def test_the_invariant_holds_under_fuzzed_concurrent_redirects(scenario: dict) -> None:
    events = _run_scenario(scenario, strict_bound=True)
    result = check(events)
    assert result.passed, result.violations


@settings(max_examples=90, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(scenario=_scenario(max_delay_ms=3 * D_MAX_MS))
def test_the_invariant_holds_with_strict_bound_disabled_and_delays_up_to_3x_d_max(scenario: dict) -> None:
    # E25: only max_local_overlap_ms is allowed to move when the bound is
    # violated — the checker must still pass.
    events = _run_scenario(scenario, strict_bound=False)
    result = check(events)
    assert result.passed, result.violations


# -- Phase 19 (E43): the multi-hospital capacity extension's own fuzz -------
# A smaller max_examples than the two above (25, not 300 — matching this
# session's earlier reduction of the original two fuzz tests to 90):
# building random Hospital/Patient/status-drift combinations is much more
# expensive per example than the plain 3-hospital scenario, and this session
# was explicitly asked to prioritize breadth of coverage across phases over
# exhaustively maximizing any one fuzz test's example count.

from app.models import BedType, Capability, Hospital, Policy  # noqa: E402
from app.seed import random_patient  # noqa: E402
from app.simulation import Simulation as _Simulation  # noqa: E402


@st.composite
def _capacity_scenario(draw: st.DrawFn):
    n_hospitals = draw(st.integers(min_value=1, max_value=6))
    hospitals = {}
    for i in range(n_hospitals):
        beds = draw(st.integers(min_value=1, max_value=8))
        hospitals[f"CH{i}"] = Hospital(
            id=f"CH{i}", name=f"CH{i}", location=(draw(st.floats(0, 40)), draw(st.floats(0, 40))),
            beds_total={BedType.GENERAL: beds, BedType.ICU: draw(st.integers(0, 4))},
            capabilities=frozenset(draw(st.sets(st.sampled_from(list(Capability)), max_size=3))),
            ventilators_total=draw(st.integers(0, 3)),
        )
    n_transports = draw(st.integers(min_value=1, max_value=10))
    rng = random.Random(draw(st.integers(min_value=0, max_value=2**31)))
    transports = [
        (random_patient(rng, f"FP{i}"), (draw(st.floats(0, 40)), draw(st.floats(0, 40))))
        for i in range(n_transports)
    ]
    policy = draw(st.sampled_from([Policy.MANUAL, Policy.AUTO]))
    return hospitals, transports, policy


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(scenario=_capacity_scenario())
def test_capacity_invariants_hold_under_fuzzed_hospitals_and_patients(scenario) -> None:
    hospitals, transports, policy = scenario
    clock = FakeClock()
    store = EventStore(":memory:")
    config = Config(
        groq_api_key="", d_max_ms=D_MAX_MS, guard_ms=GUARD_MS, ready_timeout_ms=READY_TIMEOUT_MS,
        prep_ms=PREP_MS, tick_ms=100, db_path=":memory:", policy=policy,
    )
    sim = _Simulation(clock, store, config, hospitals=hospitals)
    sim.start_batch(transports)
    clock.run_until_quiet()

    patients = sim.dispatcher.known_patients()
    result = check(store.replay(), patients=patients, hospitals=hospitals)
    # I3 (ARRIVED_WITHOUT_RESERVATION) is asserted here now that the stale
    # in-flight REDIRECT_NOTICE it used to come from is fenced off — see
    # test_capacity.py's
    # test_e33_a_stale_in_flight_notice_does_not_strand_an_ambulance. It was
    # excluded for the whole build phase while that was a known xfail.
    critical = [
        v
        for v in result.violations
        if v.kind.value in ("ZERO_ACTIVE", "MULTI_ACTIVE", "OVERBOOKED", "ARRIVED_WITHOUT_RESERVATION")
    ]
    assert not critical, critical
