"""Phase 4: the failure and edge-case paths — E2, E4, E6, E9, E10, E11, E12,
E13, E18, E20, E24, E25, E26. (E27 has dedicated facility-level tests in
test_facility.py; test_dispatcher_before_cutover.py exercises the same rule
end to end through the real dispatcher.)"""
from typing import Callable

from app.checker import check
from app.clock import FakeClock
from app.dispatcher import DispatcherStatus
from app.events import EventType
from app.messages import Ack, AckType, ActionType, Command, FacilityState
from app.presets import NORMAL
from conftest import DispatcherHarness, FakeAmbulance, assert_exactly_one_active

TRANSPORT_ID = "AMB-101"


def _redirect_events(harness: DispatcherHarness, epoch: int) -> list:
    return [e for e in harness.store.replay(TRANSPORT_ID) if e.epoch == epoch]


def _was_sent(events: list, label: str) -> bool:
    return any(e.type is EventType.COMMAND_SENT and e.payload.get("label") == label for e in events)


def test_stale_ack_at_the_dispatcher_is_ignored(harness_factory: Callable[..., DispatcherHarness]) -> None:
    # E2
    harness = harness_factory()
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 2

    before = len(harness.store.replay(TRANSPORT_ID))
    dispatcher.on_ack(_make_ack(harness.clock, command_id="cmd-1", epoch=1))  # cmd-1: START's own PREPARE, long stale
    after = harness.store.replay(TRANSPORT_ID)

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"  # unchanged
    new_events = after[before:]
    assert len(new_events) == 1
    assert new_events[0].type is EventType.STALE_IGNORED
    assert new_events[0].payload["reason"] == "epoch"


def test_duplicate_ack_is_processed_once(harness_factory: Callable[..., DispatcherHarness]) -> None:
    # E4
    harness = harness_factory()
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()  # cmd-1's READY was already delivered once, naturally

    before = len(harness.store.replay(TRANSPORT_ID))
    dispatcher.on_ack(_make_ack(harness.clock, command_id="cmd-1", epoch=1, ack_type=AckType.READY))
    new_events = harness.store.replay(TRANSPORT_ID)[before:]

    assert len(new_events) == 1
    assert new_events[0].type is EventType.DUPLICATE_IGNORED


def test_ack_with_unknown_command_id_is_ignored(harness_factory: Callable[..., DispatcherHarness]) -> None:
    # E6
    harness = harness_factory()
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    before = len(harness.store.replay(TRANSPORT_ID))
    dispatcher.on_ack(_make_ack(harness.clock, command_id="cmd-never-sent", epoch=1))
    new_events = harness.store.replay(TRANSPORT_ID)[before:]

    assert len(new_events) == 1
    assert new_events[0].type is EventType.STALE_IGNORED
    assert new_events[0].payload["reason"] == "unknown_command"


def test_ready_never_arriving_retries_once_then_aborts_leaving_current_active(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E9
    harness = harness_factory(manual_ready_facilities=("Hospital_B",), ready_timeout_ms=300)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_A"
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.NOT_READY
    v2_events = _redirect_events(harness, 2)
    prepares = [e for e in v2_events if e.type is EventType.COMMAND_SENT and e.payload.get("label") == "PREPARE"]
    assert len(prepares) == 2  # the original PREPARE, plus exactly one retry
    assert not _was_sent(v2_events, "WITHDRAW_AT")
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE


def test_received_never_arriving_by_the_receipt_deadline_aborts_without_sending_withdraw(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E10
    harness = harness_factory(ready_timeout_ms=300)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    # Black-hole ACTIVATE_AT so B never sends a RECEIVED ack for it.
    facility_b = harness.facilities["Hospital_B"]

    def drop_activate_at(command) -> None:
        if command.action is not ActionType.ACTIVATE_AT:
            facility_b.receive_command(command)

    harness.bus.register_endpoint("Hospital_B", drop_activate_at)

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_A"
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.NOT_READY
    v2_events = _redirect_events(harness, 2)
    assert any(e.type is EventType.BOUND_EXCEEDED for e in v2_events)
    assert not _was_sent(v2_events, "WITHDRAW_AT")
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE


def test_withdraw_at_arriving_after_cutover_still_withdraws_and_is_reported_as_overlap_not_a_violation(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E11 — requires strict_bound=False (a real bus would never allow this delay)
    harness = harness_factory(strict_bound=False, max_delay_ms=3000, d_max_ms=200, guard_ms=50)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    # Position 7 (WITHDRAW_AT -> the current/old facility) arrives long
    # after cutover_at (t0 + 3*200 + 50 = t0+650); everything else is fast.
    harness.bus.load_preset([15, 15, 15, 15, 15, 15, 1200, 15, 15, 15], duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    # B was already ACTIVE well before A's WITHDRAW_AT finally lands and
    # withdraws it — a real, but locally-overlapping, not zero/multi, window.
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN

    result = check(harness.store.replay(TRANSPORT_ID))
    assert result.passed  # overlap, not a violation
    assert result.max_local_overlap_ms > 0


def test_redirect_back_to_current_while_pending_and_cancellable_withdraws_the_pending_facility(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E12
    harness = harness_factory()
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    assert_exactly_one_active(harness, TRANSPORT_ID)

    dispatcher.redirect(TRANSPORT_ID, "Hospital_A")  # R8, still cancellable

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_A"
    assert dispatcher.pending_destination_of(TRANSPORT_ID) is None
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.STABLE
    assert_exactly_one_active(harness, TRANSPORT_ID)  # no zero-active instant

    harness.clock.run_until_quiet()
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    assert_exactly_one_active(harness, TRANSPORT_ID)


def test_redirect_to_the_already_pending_facility_is_a_no_op(harness_factory: Callable[..., DispatcherHarness]) -> None:
    # E13
    harness = harness_factory()
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    epoch_before = dispatcher.current_epoch_of(TRANSPORT_ID)
    events_before = len(harness.store.replay(TRANSPORT_ID))

    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")  # R9

    assert dispatcher.pending_destination_of(TRANSPORT_ID) == "Hospital_B"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == epoch_before
    assert len(harness.store.replay(TRANSPORT_ID)) == events_before  # nothing logged, nothing sent


def test_two_transports_are_fully_independent_even_on_the_same_facility(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E18
    harness = harness_factory()
    dispatcher = harness.dispatcher
    second_ambulance = FakeAmbulance("AMB-202", harness.bus, harness.clock)
    harness.bus.register_endpoint("AMB-202", second_ambulance.receive_command)

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start("AMB-202", "Hospital_A")  # same facility, independent transport
    harness.clock.run_until_quiet()

    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    assert harness.facilities["Hospital_A"].state_of("AMB-202") == FacilityState.ACTIVE

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"
    assert dispatcher.current_destination_of("AMB-202") == "Hospital_A"  # untouched
    assert harness.facilities["Hospital_A"].state_of("AMB-202") == FacilityState.ACTIVE
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN


def test_a_held_message_released_after_an_abort_is_ignored_as_stale(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E20
    harness = harness_factory(ready_timeout_ms=300)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    # Hold every PREPARE aimed at B (the original and its one retry), so
    # READY never arrives and the redirect aborts on its own.
    held_ids: list[str] = []
    original_send = harness.bus.send

    def intercept_and_hold(message) -> None:
        if (
            isinstance(message, Command)
            and message.action is ActionType.PREPARE
            and message.target_facility == "Hospital_B"
        ):
            harness.bus.hold(message.command_id)
            held_ids.append(message.command_id)
        original_send(message)

    harness.bus.send = intercept_and_hold
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.run_until_quiet()
    harness.bus.send = original_send

    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.NOT_READY  # aborted, epoch bumped
    assert len(held_ids) == 2

    before = len(harness.store.replay(TRANSPORT_ID))
    harness.bus.release(held_ids[0])
    harness.clock.run_until_quiet()
    new_events = harness.store.replay(TRANSPORT_ID)[before:]

    stale = [e for e in new_events if e.type is EventType.STALE_IGNORED and e.facility_id == "Hospital_B"]
    assert stale, "the released PREPARE(v2) must be fenced as stale now that B is at a higher epoch"
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID) == FacilityState.IDLE  # untouched by it


def test_a_redirect_too_late_to_cancel_is_queued_and_runs_after_the_first_cutover(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E24
    harness = harness_factory(d_max_ms=200, guard_ms=50, ready_timeout_ms=1000)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    _advance_until(harness, lambda: dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.CUTOVER_SCHEDULED)
    # withdraw_sent is now true; wait until we're inside the D_MAX+GUARD
    # window before cutover_at, where a cancel could no longer reach A in time.
    harness.clock.advance(3 * 200 + 50 - (200 + 50) + 10)

    dispatcher.redirect(TRANSPORT_ID, "Hospital_C")

    assert dispatcher.queued_redirect_of(TRANSPORT_ID) == "Hospital_C"
    assert dispatcher.pending_destination_of(TRANSPORT_ID) == "Hospital_B"  # v2's cutover still runs to completion
    assert_exactly_one_active(harness, TRANSPORT_ID)

    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_C"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 3
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert_exactly_one_active(harness, TRANSPORT_ID)
    events = harness.store.replay(TRANSPORT_ID)
    assert any(e.type is EventType.REDIRECT_QUEUED for e in events)

    result = check(events)
    assert result.passed
    assert result.violations == []


def test_bound_independence_with_strict_bound_false_and_scaled_up_delays(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E25 — D_MAX_MS itself stays at the nominal 200 (that's the bound being
    # violated); GUARD_MS and READY_TIMEOUT_MS are scaled up here only so
    # the cutover/receipt-deadline margins (which are computed from the
    # *configured* D_MAX_MS, not the actual delay) comfortably outlast the
    # inflated delays below — otherwise an unrelated timeout would fire
    # first and the test would prove nothing about bound independence.
    harness = harness_factory(strict_bound=False, max_delay_ms=3000, d_max_ms=200, guard_ms=3000, ready_timeout_ms=3000)
    dispatcher = harness.dispatcher

    harness.bus.load_preset([d * 2 for d in NORMAL.delays], duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()
    assert_exactly_one_active(harness, TRANSPORT_ID)

    harness.bus.load_preset([d * 3 for d in NORMAL.delays], duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    assert_exactly_one_active(harness, TRANSPORT_ID)
    harness.clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"
    assert_exactly_one_active(harness, TRANSPORT_ID)

    result = check(harness.store.replay(TRANSPORT_ID))
    assert result.passed  # bound-independent: only max_local_overlap_ms is allowed to move
    assert result.violations == []


def test_ambulance_notice_never_applied_aborts_after_one_ready_timeout(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    # E26
    harness = harness_factory(manual_confirm=True, ready_timeout_ms=300)
    dispatcher = harness.dispatcher
    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    harness.clock.run_until_quiet()

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    harness.clock.advance(200)
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.PREPARING  # READY arrived; notice never will

    harness.clock.advance(150)  # past ready_timeout_ms from the original PREPARE
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.NOT_READY
    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_A"
    v2_events = _redirect_events(harness, 2)
    prepares = [e for e in v2_events if e.type is EventType.COMMAND_SENT and e.payload.get("label") == "PREPARE"]
    assert len(prepares) == 1  # no retry — READY was never the problem


def _advance_until(harness: DispatcherHarness, predicate: Callable[[], bool], step_ms: int = 5, max_steps: int = 400) -> None:
    for _ in range(max_steps):
        if predicate():
            return
        harness.clock.advance(step_ms)
    raise AssertionError("condition never became true")


def _make_ack(
    clock: FakeClock,
    command_id: str,
    epoch: int,
    facility_id: str = "Hospital_A",
    ack_type: AckType = AckType.READY,
) -> Ack:
    return Ack(
        command_id=command_id,
        transport_id=TRANSPORT_ID,
        facility_id=facility_id,
        epoch=epoch,
        ack_type=ack_type,
        applied_state=FacilityState.ARMED,
        sent_at_ms=clock.now_ms(),
    )
