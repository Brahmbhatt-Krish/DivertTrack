"""Phase 4: E8 on REDIRECT_BEFORE_CUTOVER — a second redirect (A -> C)
arrives after the first (A -> B) has scheduled its cutover but before
withdraw_sent, so it's still cancellable (R6). The cutover is cancelled, A
REMAINs at the new epoch, B is WITHDRAWN, and the ACTIVATE_AT(v2) already in
flight to B is rejected there as stale once it finally arrives."""
from typing import Callable

from app.checker import check
from app.dispatcher import DispatcherStatus
from app.events import EventType
from app.messages import FacilityState
from conftest import DispatcherHarness, assert_exactly_one_active

TRANSPORT_ID = "AMB-101"

# Position 5 (ACTIVATE_AT -> the pending facility) is held back near the
# delay bound so it is still travelling to B when the second redirect
# cancels the first — this is what lets the facility-side fence (Phase 2)
# reject it as stale once it does arrive.
_DELAYS = [15, 15, 15, 15, 400, 15, 15, 15, 15, 15]


def test_redirect_before_cutover_cancels_and_c_becomes_current(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    harness = harness_factory(d_max_ms=500, guard_ms=50, ready_timeout_ms=1000)
    clock, dispatcher = harness.clock, harness.dispatcher

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    clock.run_until_quiet()

    harness.bus.load_preset(_DELAYS, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    _advance_until_cutover_scheduled(harness, TRANSPORT_ID)
    assert_exactly_one_active(harness, TRANSPORT_ID)  # A: the cutover hasn't fired yet

    dispatcher.redirect(TRANSPORT_ID, "Hospital_C")

    # R6's cancel branch is synchronous: REMAIN/WITHDRAW go out immediately.
    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_A"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 3
    assert dispatcher.pending_destination_of(TRANSPORT_ID) == "Hospital_C"
    assert dispatcher.status_of(TRANSPORT_ID) == DispatcherStatus.PREPARING
    assert_exactly_one_active(harness, TRANSPORT_ID)

    clock.run_until_quiet()

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_C"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 3
    assert harness.facilities["Hospital_A"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert harness.facilities["Hospital_C"].state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    assert_exactly_one_active(harness, TRANSPORT_ID)

    events = harness.store.replay(TRANSPORT_ID)
    assert any(e.type is EventType.CUTOVER_CANCELLED and e.payload["old_pending_epoch"] == 2 for e in events)
    remain_at_a = next(
        e for e in events
        if e.type is EventType.FACILITY_STATE_CHANGED and e.facility_id == "Hospital_A" and e.payload["action"] == "REMAIN"
    )
    assert remain_at_a.epoch == 3
    b_withdrawn = next(
        e for e in events
        if e.type is EventType.FACILITY_STATE_CHANGED and e.facility_id == "Hospital_B" and e.payload["to"] == "WITHDRAWN"
    )
    assert b_withdrawn.payload["action"] == "WITHDRAW"

    stale_activate_at_b = [
        e for e in events
        if e.type is EventType.STALE_IGNORED and e.facility_id == "Hospital_B" and e.payload.get("action") == "ACTIVATE_AT"
    ]
    assert stale_activate_at_b, "the in-flight ACTIVATE_AT(v2) must be rejected as stale once it reaches B"
    assert stale_activate_at_b[0].seq > b_withdrawn.seq  # it really did arrive after B was already withdrawn

    result = check(events)
    assert result.passed
    assert result.violations == []


def _advance_until_cutover_scheduled(harness: DispatcherHarness, transport_id: str) -> None:
    for _ in range(200):
        harness.clock.advance(5)
        if harness.dispatcher.status_of(transport_id) == DispatcherStatus.CUTOVER_SCHEDULED:
            return
    raise AssertionError("cutover was never scheduled")
