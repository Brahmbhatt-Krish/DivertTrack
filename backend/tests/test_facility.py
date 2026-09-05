"""Phase 2: the facility validation pipeline and transition table."""
from itertools import count

import pytest

from app.clock import FakeClock
from app.config import Config
from app.events import EventStore, EventType
from app.facility import Facility
from app.messages import Ack, AckType, ActionType, Command, FacilityState

FACILITY_ID = "Hospital_A"
TRANSPORT_ID = "AMB-101"

_command_ids = count(1)


def make_command(
    action: ActionType,
    epoch: int = 1,
    target_facility: str = FACILITY_ID,
    transport_id: str = TRANSPORT_ID,
    effective_at_ms: int | None = None,
    command_id: str | None = None,
    sent_at_ms: int = 0,
) -> Command:
    return Command(
        command_id=command_id or f"CMD-{next(_command_ids)}",
        transport_id=transport_id,
        target_facility=target_facility,
        epoch=epoch,
        action=action,
        effective_at_ms=effective_at_ms,
        sent_at_ms=sent_at_ms,
    )


class RecordingBus:
    """Stands in for the real Bus (Phase 3): records exactly what a Facility
    hands it, with no delay/duplication of its own."""

    def __init__(self) -> None:
        self.sent: list[Command | Ack] = []

    def send(self, message: Command | Ack) -> None:
        self.sent.append(message)


@pytest.fixture
def config() -> Config:
    return Config(
        groq_api_key="",
        d_max_ms=1500,
        guard_ms=300,
        ready_timeout_ms=4000,
        prep_ms=200,
        tick_ms=500,
        db_path=":memory:",
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def facility(fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus, config: Config) -> Facility:
    return Facility(FACILITY_ID, fake_clock, event_store, bus, config)


def acks_of_type(bus: RecordingBus, ack_type: AckType) -> list[Ack]:
    return [m for m in bus.sent if isinstance(m, Ack) and m.ack_type == ack_type]


# -- transition table: one test per row ---------------------------------


def test_prepare_from_idle_becomes_armed_and_sends_ready_after_prep_ms(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus, config: Config
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.ARMED
    assert acks_of_type(bus, AckType.READY) == []  # not yet — still preparing

    fake_clock.advance(config.prep_ms)

    ready_acks = acks_of_type(bus, AckType.READY)
    assert len(ready_acks) == 1
    assert ready_acks[0].applied_state == FacilityState.ARMED


def test_prepare_while_already_armed_sends_ready_immediately(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus, config: Config
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    fake_clock.advance(config.prep_ms)
    assert len(acks_of_type(bus, AckType.READY)) == 1

    facility.receive_command(make_command(ActionType.PREPARE))  # a resend, new command_id

    assert facility.state_of(TRANSPORT_ID) == FacilityState.ARMED
    assert len(acks_of_type(bus, AckType.READY)) == 2  # sent immediately, no second wait


def test_activate_at_in_the_future_sends_received_now_and_applied_later(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    fake_clock.advance(200)  # armed and ready

    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=fake_clock.now_ms() + 500))

    received = acks_of_type(bus, AckType.RECEIVED)
    assert len(received) == 1
    assert received[0].applied_state == FacilityState.ARMED
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ARMED  # not yet applied

    fake_clock.advance(500)

    applied = acks_of_type(bus, AckType.APPLIED)
    assert len(applied) == 1
    assert applied[0].applied_state == FacilityState.ACTIVE
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE


def test_withdraw_at_in_the_future_sends_received_now_and_applied_later(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    fake_clock.advance(200)
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=fake_clock.now_ms()))
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE

    facility.receive_command(make_command(ActionType.WITHDRAW_AT, effective_at_ms=fake_clock.now_ms() + 300))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE  # not yet
    fake_clock.advance(300)

    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    applied = acks_of_type(bus, AckType.APPLIED)
    assert applied[-1].applied_state == FacilityState.WITHDRAWN


def test_withdraw_moves_an_armed_facility_to_withdrawn_immediately(facility: Facility, bus: RecordingBus) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))

    facility.receive_command(make_command(ActionType.WITHDRAW))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert acks_of_type(bus, AckType.APPLIED)[-1].applied_state == FacilityState.WITHDRAWN


def test_withdraw_moves_an_active_facility_to_withdrawn_immediately(
    facility: Facility, fake_clock: FakeClock
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=fake_clock.now_ms()))
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE

    facility.receive_command(make_command(ActionType.WITHDRAW))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN


def test_remain_keeps_an_active_facility_active(facility: Facility, fake_clock: FakeClock, bus: RecordingBus) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=fake_clock.now_ms()))

    facility.receive_command(make_command(ActionType.REMAIN))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    assert acks_of_type(bus, AckType.APPLIED)[-1].applied_state == FacilityState.ACTIVE


@pytest.mark.parametrize(
    "start_action,start_effective,illegal_action",
    [
        (ActionType.PREPARE, None, ActionType.WITHDRAW_AT),  # ARMED + WITHDRAW_AT
        (ActionType.PREPARE, None, ActionType.REMAIN),  # ARMED + REMAIN
    ],
)
def test_illegal_transitions_are_logged_and_leave_state_unchanged(
    facility: Facility,
    fake_clock: FakeClock,
    event_store: EventStore,
    start_action: ActionType,
    start_effective: int | None,
    illegal_action: ActionType,
) -> None:
    facility.receive_command(make_command(start_action, effective_at_ms=start_effective))
    state_before = facility.state_of(TRANSPORT_ID)

    effective_at = fake_clock.now_ms() + 100 if illegal_action in (ActionType.ACTIVATE_AT, ActionType.WITHDRAW_AT) else None
    facility.receive_command(make_command(illegal_action, effective_at_ms=effective_at))

    assert facility.state_of(TRANSPORT_ID) == state_before
    illegal_events = [e for e in event_store.replay() if e.type is EventType.ILLEGAL_TRANSITION]
    assert len(illegal_events) == 1


# -- pipeline steps / edge cases ------------------------------------------


def test_command_with_a_lower_epoch_is_ignored_as_stale(
    facility: Facility, fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus
) -> None:
    # E1
    facility.receive_command(make_command(ActionType.PREPARE, epoch=2))
    bus.sent.clear()

    facility.receive_command(make_command(ActionType.PREPARE, epoch=1))

    assert bus.sent == []
    stale_events = [e for e in event_store.replay() if e.type is EventType.STALE_IGNORED]
    assert len(stale_events) == 1


def test_duplicate_command_id_causes_no_state_change_and_resends_the_same_ack(
    facility: Facility, fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus
) -> None:
    # E3
    original = make_command(ActionType.PREPARE, command_id="CMD-DUP")
    facility.receive_command(original)
    fake_clock.advance(200)
    first_ready = acks_of_type(bus, AckType.READY)[0]

    facility.receive_command(original)  # exact same command_id, resent by the bus

    ready_acks = acks_of_type(bus, AckType.READY)
    assert len(ready_acks) == 2
    assert ready_acks[1] == first_ready  # identical ack re-sent, unchanged
    duplicate_events = [e for e in event_store.replay() if e.type is EventType.DUPLICATE_IGNORED]
    assert len(duplicate_events) == 1


def test_command_targeting_a_different_facility_is_rejected(
    facility: Facility, event_store: EventStore, bus: RecordingBus
) -> None:
    # E5
    facility.receive_command(make_command(ActionType.PREPARE, target_facility="Hospital_B"))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.IDLE
    assert bus.sent == []
    mismatch_events = [e for e in event_store.replay() if e.type is EventType.TARGET_MISMATCH]
    assert len(mismatch_events) == 1


def test_activate_at_with_effective_at_in_the_past_applies_immediately(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus
) -> None:
    # E16
    facility.receive_command(make_command(ActionType.PREPARE))
    fake_clock.advance(50)

    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=fake_clock.now_ms() - 10))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE
    assert acks_of_type(bus, AckType.RECEIVED)
    assert acks_of_type(bus, AckType.APPLIED)


def test_withdraw_on_an_idle_facility_is_illegal_but_still_raises_the_epoch_fence(
    facility: Facility, event_store: EventStore
) -> None:
    # E17
    facility.receive_command(make_command(ActionType.WITHDRAW, epoch=5))

    assert facility.highest_applied_epoch_of(TRANSPORT_ID) == 5
    illegal_events = [e for e in event_store.replay() if e.type is EventType.ILLEGAL_TRANSITION]
    assert len(illegal_events) == 1

    # A later command at the abandoned lower epoch is now stale.
    facility.receive_command(make_command(ActionType.PREPARE, epoch=3))
    assert facility.state_of(TRANSPORT_ID) == FacilityState.IDLE


def test_a_higher_epoch_command_cancels_a_pending_lower_epoch_activate_at(
    facility: Facility, fake_clock: FakeClock, bus: RecordingBus
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE, epoch=2))
    fake_clock.advance(200)
    facility.receive_command(
        make_command(ActionType.ACTIVATE_AT, epoch=2, effective_at_ms=fake_clock.now_ms() + 1000)
    )
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ARMED

    facility.receive_command(make_command(ActionType.WITHDRAW, epoch=3))

    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    fake_clock.advance(2000)  # the cancelled ACTIVATE_AT(epoch=2) must never fire
    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN
    assert acks_of_type(bus, AckType.APPLIED)[-1].epoch == 3


def test_remain_at_a_higher_epoch_cancels_a_lower_epoch_scheduled_withdraw_at(
    facility: Facility, fake_clock: FakeClock
) -> None:
    # E27 (the half that *is* cancelled)
    facility.receive_command(make_command(ActionType.PREPARE, epoch=2))
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, epoch=2, effective_at_ms=fake_clock.now_ms()))
    facility.receive_command(
        make_command(ActionType.WITHDRAW_AT, epoch=2, effective_at_ms=fake_clock.now_ms() + 1000)
    )

    facility.receive_command(make_command(ActionType.REMAIN, epoch=3))

    fake_clock.advance(2000)
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE  # withdraw never fired


def test_remain_at_the_same_epoch_does_not_cancel_its_own_scheduled_withdraw_at(
    facility: Facility, fake_clock: FakeClock
) -> None:
    # E27 (the half that survives): deliver WITHDRAW_AT(v2, T) then REMAIN(v2)
    facility.receive_command(make_command(ActionType.PREPARE, epoch=2))
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, epoch=2, effective_at_ms=fake_clock.now_ms()))
    facility.receive_command(
        make_command(ActionType.WITHDRAW_AT, epoch=2, effective_at_ms=fake_clock.now_ms() + 1000)
    )

    facility.receive_command(make_command(ActionType.REMAIN, epoch=2))
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE

    fake_clock.advance(1000)
    assert facility.state_of(TRANSPORT_ID) == FacilityState.WITHDRAWN  # same-epoch withdraw still fires


def test_a_repeated_activate_at_for_the_same_epoch_replaces_the_previous_schedule(
    facility: Facility, fake_clock: FakeClock
) -> None:
    facility.receive_command(make_command(ActionType.PREPARE))
    first_target = fake_clock.now_ms() + 1000
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=first_target))

    later_target = fake_clock.now_ms() + 2000
    facility.receive_command(make_command(ActionType.ACTIVATE_AT, effective_at_ms=later_target))

    fake_clock.advance(1000)
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ARMED  # the first schedule was replaced, not stacked

    fake_clock.advance(1000)
    assert facility.state_of(TRANSPORT_ID) == FacilityState.ACTIVE


def test_manual_ready_blocks_ready_until_confirm_ready_is_called(
    fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus, config: Config
) -> None:
    facility = Facility(FACILITY_ID, fake_clock, event_store, bus, config, manual_ready=True)

    facility.receive_command(make_command(ActionType.PREPARE))
    fake_clock.advance(10_000)  # well past prep_ms — still nothing without confirm

    assert acks_of_type(bus, AckType.READY) == []

    facility.confirm_ready(TRANSPORT_ID)

    ready_acks = acks_of_type(bus, AckType.READY)
    assert len(ready_acks) == 1
    assert ready_acks[0].applied_state == FacilityState.ARMED
