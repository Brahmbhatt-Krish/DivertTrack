"""Shared pytest fixtures. fake_clock/event_store are used from Phase 1
onward. make_harness (Phase 4+) wires a real Bus and three real Facilities
around a Dispatcher, plus a FakeAmbulance stand-in for Phase 7's ambulance —
so dispatcher tests exercise the genuine R1-R12 pipeline end to end rather
than a hand-rolled substitute for anything but the not-yet-built ambulance.
"""
from dataclasses import dataclass
from typing import Optional

import pytest

from app.bus import Bus
from app.clock import FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import EventStore
from app.facility import Facility
from app.messages import Ack, AckType, ActionType, Command, FacilityState

FACILITY_IDS = ("Hospital_A", "Hospital_B", "Hospital_C")


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def event_store() -> EventStore:
    return EventStore(":memory:")


class FakeAmbulance:
    """Stands in for ambulance.py (Phase 7) — just enough to satisfy R3/R11's
    ambulance-notice precondition. With manual_confirm=True, APPLIED is
    withheld until confirm() is called, so a test can hold a redirect open
    on purpose (used for E26)."""

    def __init__(self, transport_id: str, bus: Bus, clock: FakeClock, manual_confirm: bool = False) -> None:
        self.transport_id = transport_id
        self._bus = bus
        self._clock = clock
        self.manual_confirm = manual_confirm
        self.known_destination: Optional[str] = None
        self._awaiting_confirm: list[Command] = []

    def receive_command(self, command: Command) -> None:
        if command.action is not ActionType.REDIRECT_NOTICE:
            return
        self.known_destination = command.target_facility
        if self.manual_confirm:
            self._awaiting_confirm.append(command)
        else:
            self._send_applied(command)

    def confirm(self) -> None:
        pending, self._awaiting_confirm = self._awaiting_confirm, []
        for command in pending:
            self._send_applied(command)

    def _send_applied(self, command: Command) -> None:
        self._bus.send(
            Ack(
                command_id=command.command_id,
                transport_id=command.transport_id,
                facility_id=self.transport_id,
                epoch=command.epoch,
                ack_type=AckType.APPLIED,
                applied_state=FacilityState.ACTIVE,  # placeholder: an ambulance has no facility state
                sent_at_ms=self._clock.now_ms(),
            )
        )


@dataclass
class DispatcherHarness:
    clock: FakeClock
    store: EventStore
    bus: Bus
    config: Config
    facilities: dict[str, Facility]
    ambulance: FakeAmbulance
    dispatcher: Dispatcher


def make_harness(
    fake_clock: FakeClock,
    event_store: EventStore,
    transport_id: str = "AMB-101",
    manual_confirm: bool = False,
    manual_ready_facilities: tuple[str, ...] = (),
    d_max_ms: int = 200,
    guard_ms: int = 50,
    ready_timeout_ms: int = 500,
    prep_ms: int = 50,
    strict_bound: bool = True,
    max_delay_ms: Optional[int] = None,
) -> DispatcherHarness:
    config = Config(
        groq_api_key="",
        d_max_ms=d_max_ms,
        guard_ms=guard_ms,
        ready_timeout_ms=ready_timeout_ms,
        prep_ms=prep_ms,
        tick_ms=100,
        db_path=":memory:",
    )
    bus = Bus(
        fake_clock,
        event_store,
        d_max_ms=d_max_ms,
        min_delay_ms=10,
        max_delay_ms=max_delay_ms if max_delay_ms is not None else d_max_ms,
        strict_bound=strict_bound,
    )

    facilities: dict[str, Facility] = {}
    for facility_id in FACILITY_IDS:
        facility = Facility(
            facility_id, fake_clock, event_store, bus, config,
            manual_ready=facility_id in manual_ready_facilities,
        )
        facilities[facility_id] = facility
        bus.register_endpoint(facility_id, facility.receive_command)

    ambulance = FakeAmbulance(transport_id, bus, fake_clock, manual_confirm=manual_confirm)
    bus.register_endpoint(transport_id, ambulance.receive_command)

    dispatcher = Dispatcher(fake_clock, event_store, bus, config)
    bus.register_dispatcher(dispatcher.on_ack)

    return DispatcherHarness(fake_clock, event_store, bus, config, facilities, ambulance, dispatcher)


@pytest.fixture
def harness_factory(fake_clock: FakeClock, event_store: EventStore):
    def _make(**kwargs) -> DispatcherHarness:
        return make_harness(fake_clock, event_store, **kwargs)

    return _make


def assert_exactly_one_active(harness: DispatcherHarness, transport_id: str) -> None:
    """A lightweight, live-state stand-in for Phase 5's real checker.py:
    checks the invariant holds *right now*, from the dispatcher's and
    facilities' live state, rather than replaying the full event log for
    every instant since transport start. Phase 4's tests call this at
    several checkpoints through each scenario; Phase 5 adds the thorough,
    pure, event-log-replaying version and test_dispatcher_*.py are
    strengthened to use it in place of this helper.
    """
    current = harness.dispatcher.current_destination_of(transport_id)
    if current is None:
        return
    active = [
        facility_id
        for facility_id, facility in harness.facilities.items()
        if facility.state_of(transport_id) == FacilityState.ACTIVE
    ]
    assert active == [current], f"Expected exactly [{current}] ACTIVE, found {active}"
