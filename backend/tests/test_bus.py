"""Phase 3: Bus timing, duplication, hold/release, and the strict_bound
contract (E22, and the strict_bound=false flip side E25 depends on)."""
import random
from itertools import count

import pytest

from app.bus import Bus
from app.clock import FakeClock
from app.events import EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command, FacilityState

D_MAX_MS = 1500
TRANSPORT_ID = "AMB-101"
_ids = count(1)


def make_command(target_facility: str = "Hospital_A", command_id: str | None = None, epoch: int = 1) -> Command:
    return Command(
        command_id=command_id or f"CMD-{next(_ids)}",
        transport_id=TRANSPORT_ID,
        target_facility=target_facility,
        epoch=epoch,
        action=ActionType.PREPARE,
        sent_at_ms=0,
    )


def make_ack(command_id: str = "CMD-ACK", epoch: int = 1) -> Ack:
    return Ack(
        command_id=command_id,
        transport_id=TRANSPORT_ID,
        facility_id="Hospital_A",
        epoch=epoch,
        ack_type=AckType.READY,
        applied_state=FacilityState.ARMED,
        sent_at_ms=0,
    )


def make_bus(fake_clock: FakeClock, event_store: EventStore, **overrides) -> Bus:
    params = dict(
        clock=fake_clock,
        store=event_store,
        d_max_ms=D_MAX_MS,
        min_delay_ms=50,
        max_delay_ms=D_MAX_MS,
        rng=random.Random(0),
    )
    params.update(overrides)
    return Bus(**params)


def test_two_sends_with_delays_800_and_100_deliver_in_reverse_order(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    bus.load_preset([800, 100], duplicate_rate=0.0)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    bus.send(make_command(command_id="SLOW"))
    bus.send(make_command(command_id="FAST"))
    fake_clock.run_until_quiet()

    assert delivered == ["FAST", "SLOW"]


def test_duplicate_rate_of_one_schedules_two_deliveries_for_one_send(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store, duplicate_rate=1.0)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    bus.send(make_command(command_id="DUP"))
    fake_clock.run_until_quiet()

    assert delivered == ["DUP", "DUP"]


def test_zero_duplicate_rate_never_duplicates(fake_clock: FakeClock, event_store: EventStore) -> None:
    bus = make_bus(fake_clock, event_store, duplicate_rate=0.0)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    for _ in range(20):
        bus.send(make_command(command_id=f"CMD-{_}"))
    fake_clock.run_until_quiet()

    assert len(delivered) == 20


def test_held_command_is_parked_and_never_delivered_without_release(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    bus.hold("HELD")
    bus.send(make_command(command_id="HELD"))
    fake_clock.run_until_quiet()

    assert delivered == []


def test_release_delivers_a_parked_message_immediately(fake_clock: FakeClock, event_store: EventStore) -> None:
    bus = make_bus(fake_clock, event_store)
    delivered: list[tuple[str, int]] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append((cmd.command_id, fake_clock.now_ms())))

    bus.hold("HELD")
    bus.send(make_command(command_id="HELD"))
    fake_clock.advance(1000)  # long past any ordinary delay — still parked
    assert delivered == []

    bus.release("HELD")

    assert delivered == [("HELD", 1000)]


def test_hold_after_a_message_is_already_scheduled_does_not_intercept_it(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    # hold() is checked at send() time only (spec: "If command_id is held,
    # park it" is part of send()'s own steps) — it cannot reach back into an
    # already-scheduled delivery.
    bus = make_bus(fake_clock, event_store)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    bus.send(make_command(command_id="IN-FLIGHT"))
    bus.hold("IN-FLIGHT")
    fake_clock.run_until_quiet()

    assert delivered == ["IN-FLIGHT"]


def test_strict_bound_rejects_a_preset_delay_above_d_max_ms(fake_clock: FakeClock, event_store: EventStore) -> None:
    # E22
    bus = make_bus(fake_clock, event_store, strict_bound=True)
    with pytest.raises(ValueError):
        bus.load_preset([D_MAX_MS + 1], duplicate_rate=0.0)


def test_strict_bound_rejects_a_send_delay_above_d_max_ms_via_construction(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    # E22 — the random-range side: a misconfigured bus whose own max_delay_ms
    # exceeds the bound is rejected immediately, not on the first unlucky draw.
    with pytest.raises(ValueError):
        make_bus(fake_clock, event_store, strict_bound=True, max_delay_ms=D_MAX_MS + 1)


def test_non_strict_bound_accepts_a_preset_delay_above_d_max_ms(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    # The flip side E25 depends on: safety must not come from the bound.
    bus = make_bus(fake_clock, event_store, strict_bound=False, max_delay_ms=D_MAX_MS * 3)
    bus.load_preset([D_MAX_MS + 200], duplicate_rate=0.0)
    delivered: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: delivered.append(cmd.command_id))

    bus.send(make_command(command_id="OVER-BOUND"))
    fake_clock.run_until_quiet()

    assert delivered == ["OVER-BOUND"]


def test_command_sent_and_message_delivered_are_logged_with_the_expected_arrival(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    bus.load_preset([300], duplicate_rate=0.0)
    bus.register_endpoint("Hospital_A", lambda cmd: None)

    bus.send(make_command(command_id="LOGGED"))
    sent_events = [e for e in event_store.replay() if e.type is EventType.COMMAND_SENT]
    assert len(sent_events) == 1
    assert sent_events[0].payload["expected_arrival_ms"] == 300

    fake_clock.advance(300)
    delivered_events = [e for e in event_store.replay() if e.type is EventType.MESSAGE_DELIVERED]
    assert len(delivered_events) == 1


def test_a_command_is_routed_to_its_target_facilitys_registered_endpoint(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    received_by_a: list[str] = []
    received_by_b: list[str] = []
    bus.register_endpoint("Hospital_A", lambda cmd: received_by_a.append(cmd.command_id))
    bus.register_endpoint("Hospital_B", lambda cmd: received_by_b.append(cmd.command_id))

    bus.send(make_command(target_facility="Hospital_B", command_id="FOR-B"))
    fake_clock.run_until_quiet()

    assert received_by_a == []
    assert received_by_b == ["FOR-B"]


def test_an_ack_is_routed_to_the_registered_dispatcher_handler(fake_clock: FakeClock, event_store: EventStore) -> None:
    bus = make_bus(fake_clock, event_store)
    received: list[str] = []
    bus.register_dispatcher(lambda ack: received.append(ack.command_id))

    bus.send(make_ack(command_id="ACK-1"))
    fake_clock.run_until_quiet()

    assert received == ["ACK-1"]


def test_sending_to_an_unregistered_endpoint_raises_a_clear_error(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    bus.send(make_command(target_facility="Hospital_Z"))

    with pytest.raises(KeyError):
        fake_clock.run_until_quiet()


def test_sending_an_ack_without_a_registered_dispatcher_raises_a_clear_error(
    fake_clock: FakeClock, event_store: EventStore
) -> None:
    bus = make_bus(fake_clock, event_store)
    bus.send(make_ack())

    with pytest.raises(RuntimeError):
        fake_clock.run_until_quiet()
