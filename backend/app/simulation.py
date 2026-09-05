"""Wires one Bus, the three Facilities, an ambulance per transport, and the
Dispatcher into a single runnable system. This is the only module that
constructs all of them together — everywhere else takes its dependencies
by injection (see clock.py's docstring), and this is where that injection
actually happens.

The ambulance used here is a minimal stand-in (auto-applies every
REDIRECT_NOTICE it receives, nothing else) — just enough to satisfy R3/R11's
precondition. Phase 7 replaces it with the real Ambulance from ambulance.py
(progress ticking, manual_confirm, Arrived); this module will import that
instead once it exists.
"""
from __future__ import annotations

from typing import Optional, Sequence

from app.bus import Bus
from app.clock import Clock, FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import Event, EventStore
from app.facility import Facility
from app.messages import Ack, AckType, ActionType, Command, FacilityState
from app.presets import ALL_PRESETS
from app.projection import Projection, project
from app.seed import HOSPITALS, NETWORK


class _AutoApplyAmbulance:
    """Stand-in for Phase 7's Ambulance: applies every REDIRECT_NOTICE it
    receives immediately, with no progress ticking and no manual_confirm."""

    def __init__(self, transport_id: str, clock: Clock, bus: Bus) -> None:
        self.transport_id = transport_id
        self._clock = clock
        self._bus = bus
        self.known_destination: Optional[str] = None

    def receive_command(self, command: Command) -> None:
        if command.action is not ActionType.REDIRECT_NOTICE:
            return
        self.known_destination = command.target_facility
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


class Simulation:
    def __init__(
        self,
        clock: Clock,
        store: EventStore,
        config: Config,
        strict_bound: bool = True,
        min_delay_ms: Optional[int] = None,
        max_delay_ms: Optional[int] = None,
    ) -> None:
        self._clock = clock
        self._store = store
        self._config = config
        self._bus = Bus(
            clock,
            store,
            d_max_ms=config.d_max_ms,
            min_delay_ms=min_delay_ms if min_delay_ms is not None else NETWORK.min_delay_ms,
            # NETWORK.max_delay_ms bakes in the *global* settings.d_max_ms at
            # import time; the bound that actually matters is this instance's
            # own config.d_max_ms, so that's the default here instead.
            max_delay_ms=max_delay_ms if max_delay_ms is not None else config.d_max_ms,
            duplicate_rate=NETWORK.duplicate_rate,
            strict_bound=strict_bound,
        )

        self._facilities: dict[str, Facility] = {}
        for hospital in HOSPITALS:
            facility = Facility(hospital.facility_id, clock, store, self._bus, config)
            self._facilities[hospital.facility_id] = facility
            self._bus.register_endpoint(hospital.facility_id, facility.receive_command)

        self._ambulances: dict[str, _AutoApplyAmbulance] = {}
        self._dispatcher = Dispatcher(clock, store, self._bus, config)
        self._bus.register_dispatcher(self._dispatcher.on_ack)

    # -- read-only access for callers that need it (tests, main.py) --------

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def facilities(self) -> dict[str, Facility]:
        return dict(self._facilities)

    # -- orchestration -----------------------------------------------------

    def start(self, transport_id: str, destination: str) -> None:
        ambulance = _AutoApplyAmbulance(transport_id, self._clock, self._bus)
        self._ambulances[transport_id] = ambulance
        self._bus.register_endpoint(transport_id, ambulance.receive_command)
        self._dispatcher.start(transport_id, destination)

    def redirect(self, transport_id: str, target: str) -> None:
        self._dispatcher.redirect(transport_id, target)

    def load_delays(self, delays: Sequence[int], duplicate_rate: float) -> None:
        self._bus.load_preset(delays, duplicate_rate)

    def hold(self, command_id: str) -> None:
        self._bus.hold(command_id)

    def release(self, command_id: str) -> None:
        self._bus.release(command_id)

    def run_preset(self, transport_id: str, name: str) -> None:
        preset = ALL_PRESETS[name]
        self._bus.load_preset(preset.delays, preset.duplicate_rate)
        for scheduled in preset.redirects:
            target = scheduled.target_facility
            self._clock.schedule(scheduled.at_ms, lambda t=target: self.redirect(transport_id, t))

    def run_until_quiet(self) -> None:
        if not isinstance(self._clock, FakeClock):
            raise TypeError("run_until_quiet() requires a FakeClock; RealClock runs on its own event loop")
        self._clock.run_until_quiet()

    # -- reading the result --------------------------------------------------

    def events(self, transport_id: Optional[str] = None) -> list[Event]:
        return self._store.replay(transport_id)

    def views(self) -> Projection:
        return project(self._store.replay())
