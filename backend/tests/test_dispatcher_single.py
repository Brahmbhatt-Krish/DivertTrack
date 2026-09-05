"""Phase 4: a clean A -> B redirect on NORMAL — the textbook case R1-R12
walks through: activate before withdraw, withdraw only after proof, exactly
one facility active throughout."""
from typing import Callable

from app.checker import check
from app.dispatcher import DispatcherStatus
from app.events import EventType
from app.messages import ActionType, FacilityState
from app.presets import NORMAL
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
