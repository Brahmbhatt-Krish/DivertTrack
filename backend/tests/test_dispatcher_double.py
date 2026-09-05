"""Phase 4: E7 on OUT_OF_ORDER_ACK — a second redirect (B -> C) starts right
after the first (A -> B) cutover applies, while B's own APPLIED ack for its
ACTIVATE_AT(v2) is still crawling back to the dispatcher. It must arrive
*after* C's READY(v3) and be fenced as stale on arrival — C, not B, ends up
current at v3."""
from typing import Callable

from app.checker import check
from app.events import EventType
from conftest import DispatcherHarness, assert_exactly_one_active

TRANSPORT_ID = "AMB-101"

# Every send gets 15ms except B's activate-APPLIED (position 9 of the A->B
# redirect's own send sequence — see presets.py's canonical ordering),
# which is held back near the delay bound so it arrives long after the
# second redirect is already under way.
_OUT_OF_ORDER_DELAYS = [15, 15, 15, 15, 15, 15, 15, 15, 190, 15]


def test_out_of_order_activate_applied_is_fenced_as_stale_and_c_becomes_current(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    harness = harness_factory()
    clock, dispatcher = harness.clock, harness.dispatcher

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.start(TRANSPORT_ID, "Hospital_A")
    clock.run_until_quiet()

    harness.bus.load_preset(_OUT_OF_ORDER_DELAYS, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_B")
    # Comfortably past cutover_at (t0 + 3*D_MAX_MS + GUARD_MS <= ~710ms after
    # this call, given the fast delays above) but well short of B's delayed
    # APPLIED ack, which is still in flight.
    clock.advance(800)
    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 2
    assert_exactly_one_active(harness, TRANSPORT_ID)

    harness.bus.load_preset([15] * 20, duplicate_rate=0.0)
    dispatcher.redirect(TRANSPORT_ID, "Hospital_C")
    clock.run_until_quiet()  # lets both the v3 flow and the stale v2 ack land

    assert dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_C"
    assert dispatcher.current_epoch_of(TRANSPORT_ID) == 3
    assert harness.facilities["Hospital_B"].state_of(TRANSPORT_ID).value == "WITHDRAWN"
    assert_exactly_one_active(harness, TRANSPORT_ID)

    v2_events = [e for e in harness.store.replay(TRANSPORT_ID) if e.epoch == 2]
    stale = [e for e in v2_events if e.type is EventType.STALE_IGNORED]
    assert stale, "B's late APPLIED(v2) should have been fenced as stale"
    assert all(e.payload.get("reason") == "epoch" for e in stale)

    v3_ready = next(
        e for e in harness.store.replay(TRANSPORT_ID)
        if e.epoch == 3 and e.type is EventType.ACK_RECEIVED and e.payload.get("ack_type") == "READY"
    )
    late_v2_applied = stale[-1]
    assert late_v2_applied.seq > v3_ready.seq  # the stale ack really did arrive after C's READY

    result = check(harness.store.replay(TRANSPORT_ID))
    assert result.passed
    assert result.violations == []
