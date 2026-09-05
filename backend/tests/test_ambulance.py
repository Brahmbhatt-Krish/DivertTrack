"""Phase 7: the ambulance's fence (spec: "same fence as facilities, steps
2-3"), progress ticking, manual_confirm, and an end-to-end Arrived/checker
scenario (E19 also has a dedicated hand-built test in test_checker.py)."""
from itertools import count
from typing import Optional

import pytest

from app.ambulance import Ambulance
from app.checker import check
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore, EventType
from app.messages import Ack, ActionType, Command
from app.simulation import Simulation

TRANSPORT_ID = "AMB-101"
_command_ids = count(1)


def make_notice(
    target: str,
    epoch: int = 1,
    command_id: Optional[str] = None,
    transport_id: str = TRANSPORT_ID,
) -> Command:
    return Command(
        command_id=command_id or f"CMD-{next(_command_ids)}",
        transport_id=transport_id,
        target_facility=target,
        epoch=epoch,
        action=ActionType.REDIRECT_NOTICE,
        sent_at_ms=0,
    )


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list[Command | Ack] = []

    def send(self, message: Command | Ack) -> None:
        self.sent.append(message)


@pytest.fixture
def config() -> Config:
    return Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500,
        prep_ms=50, tick_ms=50, db_path=":memory:",
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def ambulance(fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus, config: Config) -> Ambulance:
    return Ambulance(TRANSPORT_ID, fake_clock, event_store, bus, config)


def acks(bus: RecordingBus) -> list[Ack]:
    return [m for m in bus.sent if isinstance(m, Ack)]


def test_redirect_notice_updates_known_destination_and_sends_applied(ambulance: Ambulance, bus: RecordingBus) -> None:
    ambulance.receive_command(make_notice("Hospital_A"))

    assert ambulance.known_destination == "Hospital_A"
    assert len(acks(bus)) == 1
    assert acks(bus)[0].ack_type.value == "APPLIED"


def test_duplicate_command_id_resends_the_same_ack_without_reprocessing(
    ambulance: Ambulance, event_store: EventStore, bus: RecordingBus
) -> None:
    notice = make_notice("Hospital_A", command_id="CMD-DUP")
    ambulance.receive_command(notice)
    ambulance.receive_command(notice)  # exact same command_id, resent by the bus

    assert len(acks(bus)) == 2
    assert acks(bus)[0] == acks(bus)[1]
    duplicate_events = [e for e in event_store.replay() if e.type is EventType.DUPLICATE_IGNORED]
    assert len(duplicate_events) == 1


def test_a_notice_with_a_lower_epoch_is_ignored_as_stale(
    ambulance: Ambulance, event_store: EventStore, bus: RecordingBus
) -> None:
    ambulance.receive_command(make_notice("Hospital_B", epoch=2))
    bus.sent.clear()

    ambulance.receive_command(make_notice("Hospital_A", epoch=1))

    assert ambulance.known_destination == "Hospital_B"  # unchanged
    assert bus.sent == []
    stale_events = [e for e in event_store.replay() if e.type is EventType.STALE_IGNORED]
    assert len(stale_events) == 1


def test_manual_confirm_withholds_applied_until_confirm_is_called(
    fake_clock: FakeClock, event_store: EventStore, bus: RecordingBus, config: Config
) -> None:
    ambulance = Ambulance(TRANSPORT_ID, fake_clock, event_store, bus, config, manual_confirm=True)

    ambulance.receive_command(make_notice("Hospital_A"))
    assert ambulance.known_destination == "Hospital_A"  # updated immediately regardless of confirm
    assert acks(bus) == []

    ambulance.confirm()

    assert len(acks(bus)) == 1


def test_arrived_is_logged_once_progress_reaches_one(
    ambulance: Ambulance, fake_clock: FakeClock, event_store: EventStore
) -> None:
    ambulance.receive_command(make_notice("Hospital_A"))
    assert ambulance.progress == 0.0

    fake_clock.run_until_quiet()

    assert ambulance.progress == 1.0
    arrived = [e for e in event_store.replay() if e.type is EventType.ARRIVED]
    assert len(arrived) == 1
    assert arrived[0].payload["at"] == "Hospital_A"


def test_a_redirect_mid_journey_resets_progress_toward_the_new_destination(
    ambulance: Ambulance, fake_clock: FakeClock
) -> None:
    ambulance.receive_command(make_notice("Hospital_A"))
    fake_clock.advance(ambulance._ticks_per_leg * 50 // 2)  # halfway there
    assert 0.0 < ambulance.progress < 1.0

    ambulance.receive_command(make_notice("Hospital_B", epoch=2))

    assert ambulance.known_destination == "Hospital_B"
    assert ambulance.progress == 0.0  # restarted toward the new destination


def test_journey_takes_longer_than_a_redirects_worst_case_completion_time(config: Config) -> None:
    # The whole point of deriving _ticks_per_leg from config: an ambulance
    # that "arrives" before the dispatcher has actually finished a handover
    # would be flagged by the checker as ARRIVED_AT_WRONG_FACILITY even
    # though nothing unsafe happened.
    worst_case_redirect_ms = config.prep_ms + 5 * config.d_max_ms + config.guard_ms
    clock = FakeClock()
    ambulance = Ambulance(TRANSPORT_ID, clock, EventStore(":memory:"), RecordingBus(), config)
    leg_ms = ambulance._ticks_per_leg * config.tick_ms
    assert leg_ms > worst_case_redirect_ms


def test_a_real_redirect_scenario_ends_with_the_ambulance_arriving_where_the_checker_says_current_is() -> None:
    # Integration: the real Ambulance, Dispatcher, Bus and Facilities
    # together, checked by the real checker — E19's hand-built proof lives
    # in test_checker.py; this exercises the same property for real.
    clock = FakeClock()
    store = EventStore(":memory:")
    config = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500,
        prep_ms=50, tick_ms=50, db_path=":memory:",
    )
    sim = Simulation(clock, store, config)
    sim.load_delays([15] * 40, 0.0)
    sim.start(TRANSPORT_ID, "Hospital_A")
    sim.run_until_quiet()
    sim.redirect(TRANSPORT_ID, "Hospital_B")
    sim.run_until_quiet()

    result = check(sim.events(TRANSPORT_ID))
    assert result.passed
    assert result.violations == []

    arrived = [e for e in sim.events(TRANSPORT_ID) if e.type is EventType.ARRIVED]
    assert [e.payload["at"] for e in arrived] == ["Hospital_A", "Hospital_B"]
    assert sim.dispatcher.current_destination_of(TRANSPORT_ID) == "Hospital_B"
