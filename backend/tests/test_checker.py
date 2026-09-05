"""Phase 5: projection.py and checker.py — both pure functions of the event
log. Most scenarios here are hand-built event lists rather than a live
Bus/Dispatcher run, since these are exactly the functions meant to catch a
bug in that machinery, not just restate what it already believes."""
from typing import Callable

from app.checker import ViolationKind, check
from app.events import Event, EventType
from app.projection import TransportViewStatus, project
from conftest import DispatcherHarness

TRANSPORT_ID = "AMB-101"


def _facility_changed(ts_ms: int, epoch: int, facility_id: str, to_state: str, from_state: str = "ARMED") -> Event:
    return Event(
        transport_id=TRANSPORT_ID,
        epoch=epoch,
        ts_ms=ts_ms,
        type=EventType.FACILITY_STATE_CHANGED,
        facility_id=facility_id,
        payload={"from": from_state, "to": to_state, "command_id": "cmd-x", "action": "x"},
    )


def _cutover_applied(ts_ms: int, epoch: int, destination: str) -> Event:
    return Event(
        transport_id=TRANSPORT_ID,
        epoch=epoch,
        ts_ms=ts_ms,
        type=EventType.CUTOVER_APPLIED,
        facility_id=None,
        payload={"current_destination": destination},
    )


# -- checker.py ------------------------------------------------------------


def test_a_clean_single_facility_log_passes() -> None:
    events = [_cutover_applied(0, 1, "Hospital_A"), _facility_changed(0, 1, "Hospital_A", "ACTIVE")]
    result = check(events)
    assert result.passed
    assert result.violations == []
    assert result.transitions_checked == 1


def test_current_destination_not_active_at_the_end_of_an_instant_fails_zero_active() -> None:
    events = [_cutover_applied(0, 1, "Hospital_A")]  # Hospital_A never actually reports ACTIVE
    result = check(events)
    assert not result.passed
    assert result.violations[0].kind is ViolationKind.ZERO_ACTIVE


def test_zero_active_mid_instant_but_one_active_at_the_end_of_the_same_ms_passes() -> None:
    events = [
        _cutover_applied(100, 2, "Hospital_B"),
        _facility_changed(100, 1, "Hospital_A", "WITHDRAWN"),  # momentarily nobody is ACTIVE...
        _facility_changed(100, 2, "Hospital_B", "ACTIVE"),  # ...but by the end of ts_ms=100, B is
    ]
    result = check(events)
    assert result.passed


def test_a_second_facility_locally_active_raises_overlap_but_still_passes() -> None:
    events = [
        _cutover_applied(0, 2, "Hospital_B"),
        _facility_changed(0, 2, "Hospital_B", "ACTIVE"),
        _facility_changed(100, 1, "Hospital_A", "ACTIVE"),  # stale — still locally ACTIVE
        _facility_changed(400, 1, "Hospital_A", "WITHDRAWN"),  # finally catches up
    ]
    result = check(events)
    assert result.passed
    assert result.max_local_overlap_ms == 300


def test_arrived_during_a_pending_redirect_compares_against_current_not_pending() -> None:
    # E19
    events = [
        _cutover_applied(0, 1, "Hospital_A"),
        _facility_changed(0, 1, "Hospital_A", "ACTIVE"),
        Event(
            transport_id=TRANSPORT_ID, epoch=2, ts_ms=50, type=EventType.REDIRECT_REQUESTED,
            facility_id=None, payload={"target": "Hospital_B"},
        ),
        Event(
            transport_id=TRANSPORT_ID, epoch=1, ts_ms=100, type=EventType.ARRIVED,
            facility_id=None, payload={"at": "Hospital_A"},
        ),
    ]
    result = check(events)
    assert result.passed  # arrived at current (A), even with B pending


def test_arriving_at_a_facility_that_is_not_current_is_a_violation() -> None:
    events = [
        _cutover_applied(0, 1, "Hospital_A"),
        _facility_changed(0, 1, "Hospital_A", "ACTIVE"),
        Event(
            transport_id=TRANSPORT_ID, epoch=1, ts_ms=100, type=EventType.ARRIVED,
            facility_id=None, payload={"at": "Hospital_C"},
        ),
    ]
    result = check(events)
    assert not result.passed
    assert result.violations[0].kind is ViolationKind.ARRIVED_AT_WRONG_FACILITY


def test_check_ignores_the_period_before_current_destination_is_ever_set() -> None:
    events = [
        Event(
            transport_id=TRANSPORT_ID, epoch=1, ts_ms=0, type=EventType.TRANSPORT_STARTED,
            facility_id=None, payload={"destination": "Hospital_A"},
        ),
        _facility_changed(0, 1, "Hospital_A", "ARMED", from_state="IDLE"),
    ]
    result = check(events)
    assert result.passed
    assert result.transitions_checked == 0  # current_destination never became non-None


def test_check_is_a_pure_function_of_its_input() -> None:
    events = [_cutover_applied(0, 1, "Hospital_A"), _facility_changed(0, 1, "Hospital_A", "ACTIVE")]
    snapshot = list(events)
    check(events)
    assert events == snapshot  # not mutated
    assert check(events) == check(events)  # same input, same output


def test_a_real_redirect_scenario_from_the_dispatcher_harness_passes(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    harness = harness_factory()
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    harness.dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    harness.dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()

    result = check(harness.store.replay(TRANSPORT_ID))
    assert result.passed
    assert result.transitions_checked > 0


# -- projection.py -----------------------------------------------------------


def test_project_reconstructs_current_and_pending_destinations() -> None:
    events = [
        Event(
            transport_id=TRANSPORT_ID, epoch=1, ts_ms=0, type=EventType.TRANSPORT_STARTED,
            facility_id=None, payload={"destination": "Hospital_A"},
        ),
        _cutover_applied(50, 1, "Hospital_A"),
        Event(
            transport_id=TRANSPORT_ID, epoch=2, ts_ms=100, type=EventType.REDIRECT_REQUESTED,
            facility_id=None, payload={"target": "Hospital_B"},
        ),
    ]
    result = project(events)
    view = result.transports[TRANSPORT_ID]
    assert view.current_destination == "Hospital_A"
    assert view.pending_destination == "Hospital_B"
    assert view.status is TransportViewStatus.PREPARING


def test_project_marks_a_stale_facility() -> None:
    events = [
        _cutover_applied(0, 2, "Hospital_B"),
        _facility_changed(0, 2, "Hospital_B", "ACTIVE"),
        _facility_changed(100, 1, "Hospital_A", "ACTIVE"),  # abandoned, still locally active
    ]
    result = project(events)
    assert result.facilities[("Hospital_A", TRANSPORT_ID)].stale is True
    assert result.facilities[("Hospital_B", TRANSPORT_ID)].stale is False


def test_project_marks_a_mid_transition_transport_interrupted_after_a_restart() -> None:
    # E21
    events = [
        Event(
            transport_id=TRANSPORT_ID, epoch=2, ts_ms=0, type=EventType.REDIRECT_REQUESTED,
            facility_id=None, payload={"target": "Hospital_B"},
        ),
    ]
    live = project(events, mark_interrupted=False)
    after_restart = project(events, mark_interrupted=True)

    assert live.transports[TRANSPORT_ID].status is TransportViewStatus.PREPARING
    assert after_restart.transports[TRANSPORT_ID].status is TransportViewStatus.INTERRUPTED


def test_project_does_not_mark_a_settled_transport_interrupted() -> None:
    events = [_cutover_applied(0, 1, "Hospital_A")]
    result = project(events, mark_interrupted=True)
    assert result.transports[TRANSPORT_ID].status is TransportViewStatus.STABLE
