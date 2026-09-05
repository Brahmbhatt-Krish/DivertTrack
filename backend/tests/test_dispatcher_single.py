"""Phase 4: a clean A -> B redirect on NORMAL — the textbook case R1-R12
walks through: activate before withdraw, withdraw only after proof, exactly
one facility active throughout."""
import asyncio
from typing import Callable

from app.checker import check
from app.clock import RealClock
from app.config import Config
from app.dispatcher import DispatcherStatus
from app.events import EventStore, EventType
from app.messages import ActionType, FacilityState
from app.presets import NORMAL
from app.simulation import Simulation
from conftest import DispatcherHarness, assert_exactly_one_active


def test_a_to_b_redirect_on_normal_activates_before_it_withdraws(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    harness = harness_factory()
    clock, dispatcher, transport_id = harness.clock, harness.dispatcher, "AMB-101"

    dispatcher.start(transport_id, "Hospital_A")
    clock.run_until_quiet()
    assert dispatcher.current_destination_of(transport_id) == "Hospital_A"
    assert_exactly_one_active(harness, transport_id)

    harness.bus.load_preset(NORMAL.delays, NORMAL.duplicate_rate)
    dispatcher.redirect(transport_id, "Hospital_B")
    assert_exactly_one_active(harness, transport_id)  # A is still active right after the request

    clock.run_until_quiet()

    assert dispatcher.current_destination_of(transport_id) == "Hospital_B"
    assert dispatcher.current_epoch_of(transport_id) == 2
    assert dispatcher.status_of(transport_id) == DispatcherStatus.STABLE
    assert harness.facilities["Hospital_A"].state_of(transport_id) == FacilityState.WITHDRAWN
    assert harness.facilities["Hospital_B"].state_of(transport_id) == FacilityState.ACTIVE
    assert_exactly_one_active(harness, transport_id)

    events = harness.store.replay(transport_id)
    redirect_events = [e for e in events if e.epoch == 2]

    assert len([e for e in redirect_events if e.type is EventType.CUTOVER_APPLIED]) == 1

    commands_sent = [
        e for e in redirect_events if e.type is EventType.COMMAND_SENT and e.payload.get("kind") == "command"
    ]
    activate_at_seq = next(e.seq for e in commands_sent if e.payload["label"] == ActionType.ACTIVATE_AT.value)
    withdraw_at_seq = next(e.seq for e in commands_sent if e.payload["label"] == ActionType.WITHDRAW_AT.value)
    assert activate_at_seq < withdraw_at_seq  # R4: activate first

    withdraw_sent = [e for e in redirect_events if e.type is EventType.WITHDRAW_SENT]
    assert len(withdraw_sent) == 1
    received_acks = [
        e for e in redirect_events if e.type is EventType.ACK_RECEIVED and e.payload.get("ack_type") == "RECEIVED"
    ]
    assert received_acks  # R5: WithdrawSent is never logged before a RECEIVED ack arrives
    assert withdraw_sent[0].seq > received_acks[0].seq

    # Phase 5's real checker, replayed over this scenario's full log, not
    # just the live-state snapshots taken above.
    result = check(events)
    assert result.passed
    assert result.violations == []


def test_withdraw_is_never_sent_before_activate_is_received(
    harness_factory: Callable[..., DispatcherHarness]
) -> None:
    harness = harness_factory()
    clock, dispatcher, transport_id = harness.clock, harness.dispatcher, "AMB-101"
    dispatcher.start(transport_id, "Hospital_A")
    clock.run_until_quiet()

    # Delay B's RECEIVED ack for ACTIVATE_AT well past everything else (send
    # index 6 in the canonical redirect sequence — see presets.py), opening
    # a clear window where the dispatcher has heard nothing back yet from
    # the pending facility.
    harness.bus.load_preset([10, 10, 10, 10, 10, 190, 10, 10, 10, 10], duplicate_rate=0.0)
    dispatcher.redirect(transport_id, "Hospital_B")

    clock.advance(260)  # past ACTIVATE_AT's delivery, short of RECEIVED's arrival at t=270
    assert not _withdraw_at_was_sent(harness, transport_id)

    clock.advance(20)  # now past RECEIVED's arrival
    assert _withdraw_at_was_sent(harness, transport_id)


def _withdraw_at_was_sent(harness: DispatcherHarness, transport_id: str) -> bool:
    return any(
        e.type is EventType.COMMAND_SENT and e.payload.get("label") == ActionType.WITHDRAW_AT.value
        for e in harness.store.replay(transport_id)
    )


def test_the_invariant_holds_under_a_real_clock_not_just_fakeclock() -> None:
    # Not a pytest-asyncio test (none of our core logic needs an event
    # loop) — this specifically exercises what FakeClock's deterministic
    # tie-breaking (E23) can't: two independently-scheduled real asyncio
    # timers aimed at the same nominal instant (the dispatcher's own
    # cutover-apply timer and the pending facility's own activation timer)
    # have no ordering guarantee against each other, and Event.ts_ms's
    # integer-millisecond rounding means their *computed* targets can
    # differ by a millisecond or two even when both sides are correct.
    # _DISPATCHER_SETTLE_MS exists precisely so this never produces a
    # ZERO_ACTIVE reading; this test would have caught its absence.
    async def run_once(transport_id: str) -> tuple[bool, list, int]:
        clock = RealClock(asyncio.get_event_loop())
        store = EventStore(":memory:")
        config = Config(
            groq_api_key="", d_max_ms=100, guard_ms=30, ready_timeout_ms=1500,
            prep_ms=30, tick_ms=100, db_path=":memory:",
        )
        sim = Simulation(clock, store, config, min_delay_ms=5, max_delay_ms=100)
        sim.start(transport_id, "Hospital_A")
        await asyncio.sleep(1.0)
        sim.redirect(transport_id, "Hospital_B")
        await asyncio.sleep(1.2)
        sim.redirect(transport_id, "Hospital_C")
        await asyncio.sleep(1.2)
        result = check(sim.events(transport_id))
        return result.passed, result.violations, result.max_local_overlap_ms

    for i in range(3):
        passed, violations, _overlap = asyncio.run(run_once(f"AMB-REAL-{i}"))
        assert passed, violations
