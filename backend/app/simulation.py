"""Wires one Bus, the three Facilities, an ambulance per transport, and the
Dispatcher into a single runnable system. This is the only module that
constructs all of them together — everywhere else takes its dependencies
by injection (see clock.py's docstring), and this is where that injection
actually happens.
"""
from __future__ import annotations

from typing import Optional, Sequence

from app.ambulance import Ambulance
from app.bus import Bus
from app.clock import Clock, FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import Event, EventStore
from app.facility import Facility
from app.presets import ALL_PRESETS
from app.projection import Projection, project
from app.seed import HOSPITALS, NETWORK


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

        self._ambulances: dict[str, Ambulance] = {}
        self._dispatcher = Dispatcher(clock, store, self._bus, config)
        self._bus.register_dispatcher(self._dispatcher.on_ack)

    # -- read-only access for callers that need it (tests, main.py) --------

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def facilities(self) -> dict[str, Facility]:
        return dict(self._facilities)

    @property
    def ambulances(self) -> dict[str, Ambulance]:
        return dict(self._ambulances)

    # -- orchestration -----------------------------------------------------

    def start(self, transport_id: str, destination: str, manual_confirm: bool = False) -> None:
        ambulance = Ambulance(transport_id, self._clock, self._store, self._bus, self._config, manual_confirm=manual_confirm)
        self._ambulances[transport_id] = ambulance
        self._bus.register_endpoint(transport_id, ambulance.receive_command)
        self._dispatcher.start(transport_id, destination)

    def redirect(self, transport_id: str, target: str) -> None:
        self._dispatcher.redirect(transport_id, target)

    def confirm(self, transport_id: str) -> None:
        """Manually applies whatever REDIRECT_NOTICE(s) that transport's
        ambulance is holding when running with manual_confirm=True."""
        self._ambulances[transport_id].confirm()

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
