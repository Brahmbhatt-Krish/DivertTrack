"""The simulated network between the dispatcher and every endpoint (facility
or ambulance). Bus owns exactly one thing: timing and delivery mechanics —
delay, duplication, holding. It never looks at what a message says, only at
where it's going (Command.target_facility) and who sent it (an Ack always
goes back to the dispatcher, the system's one authority).
"""
from __future__ import annotations

import random
from typing import Callable, Optional, Sequence

from app.clock import Clock
from app.events import Event, EventStore, EventType
from app.messages import Ack, ActionType, Command


class Bus:
    def __init__(
        self,
        clock: Clock,
        store: EventStore,
        d_max_ms: int,
        min_delay_ms: int,
        max_delay_ms: int,
        duplicate_rate: float = 0.0,
        strict_bound: bool = True,
        rng: Optional[random.Random] = None,
    ) -> None:
        if min_delay_ms < 0 or max_delay_ms < min_delay_ms:
            raise ValueError(
                f"Expected 0 <= min_delay_ms <= max_delay_ms, received "
                f"min={min_delay_ms}, max={max_delay_ms}"
            )
        self._clock = clock
        self._store = store
        self._d_max_ms = d_max_ms
        self._min_delay_ms = min_delay_ms
        self._max_delay_ms = max_delay_ms
        self._duplicate_rate = duplicate_rate
        self._strict_bound = strict_bound
        self._rng = rng or random.Random()
        self._preset_delays: list[int] = []
        self._endpoints: dict[str, Callable[[Command], None]] = {}
        self._dispatcher_handler: Optional[Callable[[Ack], None]] = None
        self._held: set[str] = set()
        self._parked: dict[str, list[Command | Ack]] = {}
        self._in_flight: dict[str, dict] = {}

        if strict_bound and max_delay_ms > d_max_ms:
            raise ValueError(
                f"Expected max_delay_ms <= D_MAX_MS ({d_max_ms}) under strict_bound, "
                f"received {max_delay_ms}"
            )

    # -- wiring: who receives what --------------------------------------

    def register_endpoint(self, endpoint_id: str, handler: Callable[[Command], None]) -> None:
        """A facility registers under its own id, matching what
        Command.target_facility will name for every action except
        REDIRECT_NOTICE; an ambulance registers under its transport_id,
        matching where a REDIRECT_NOTICE is actually routed (see _deliver) —
        target_facility on that action carries the new destination's name,
        not a routing address, since it is content the ambulance needs to
        learn, not an endpoint to reach."""
        self._endpoints[endpoint_id] = handler

    def register_dispatcher(self, handler: Callable[[Ack], None]) -> None:
        """Every Ack, from any endpoint, goes back to the one dispatcher."""
        self._dispatcher_handler = handler

    # -- presets -----------------------------------------------------------

    def load_preset(self, delays: Sequence[int], duplicate_rate: float) -> None:
        if self._strict_bound:
            for delay in delays:
                self._check_bound(delay)
        self._preset_delays = list(delays)
        self._duplicate_rate = duplicate_rate

    # -- hold / release ------------------------------------------------------

    def hold(self, command_id: str) -> None:
        """Marks command_id so its *next* send() is parked, not delivered.
        This only affects sends that happen after hold() is called — a
        message already scheduled for delivery keeps going (see send())."""
        self._held.add(command_id)

    def release(self, command_id: str) -> None:
        self._held.discard(command_id)
        parked = self._parked.pop(command_id, [])
        for message in parked:
            self._deliver(message)

    # -- send / deliver ------------------------------------------------------

    def send(self, message: Command | Ack) -> None:
        self._schedule_one_delivery(message)
        if self._rng.random() < self._duplicate_rate:
            self._schedule_one_delivery(message)

    def _schedule_one_delivery(self, message: Command | Ack) -> None:
        delay = self._next_delay()
        expected_arrival_ms = self._clock.now_ms() + delay
        self._log_command_sent(message, expected_arrival_ms)
        kind = "command" if isinstance(message, Command) else "ack"
        label = message.action.value if isinstance(message, Command) else message.ack_type.value
        if message.command_id in self._held:
            self._parked.setdefault(message.command_id, []).append(message)
            self._in_flight[message.command_id] = {
                "command_id": message.command_id, "kind": kind, "label": label,
                "held": True, "expected_arrival_ms": None,
            }
        else:
            self._in_flight[message.command_id] = {
                "command_id": message.command_id, "kind": kind, "label": label,
                "held": False, "expected_arrival_ms": expected_arrival_ms,
            }
            self._clock.schedule(delay, lambda: self._deliver(message))

    def in_flight(self) -> list[dict]:
        """A live snapshot for the demo UI's in-flight list (Phase 9) — not
        derivable from the event log alone, which only shows the past. A
        message sent twice via duplicate_rate collapses to one entry here
        (both share a command_id); that's a display nuance, not a
        correctness issue — the log still records both deliveries."""
        return list(self._in_flight.values())

    def _deliver(self, message: Command | Ack) -> None:
        self._in_flight.pop(message.command_id, None)
        self._log_message_delivered(message)
        if isinstance(message, Command):
            # REDIRECT_NOTICE always goes to the ambulance for this
            # transport, addressed by transport_id — its target_facility is
            # the destination it's being told about, not where to send it.
            endpoint_id = message.transport_id if message.action == ActionType.REDIRECT_NOTICE else message.target_facility
            endpoint = self._endpoints.get(endpoint_id)
            if endpoint is None:
                raise KeyError(f"Expected a registered endpoint for {endpoint_id!r}, found none")
            endpoint(message)
        else:
            if self._dispatcher_handler is None:
                raise RuntimeError("Expected register_dispatcher() to have been called before delivering an Ack")
            self._dispatcher_handler(message)

    def _next_delay(self) -> int:
        if self._preset_delays:
            delay = self._preset_delays.pop(0)
        else:
            delay = self._rng.randint(self._min_delay_ms, self._max_delay_ms)
        self._check_bound(delay)
        return delay

    def _check_bound(self, delay: int) -> None:
        if self._strict_bound and delay > self._d_max_ms:
            raise ValueError(
                f"Expected a delay <= D_MAX_MS ({self._d_max_ms}) under strict_bound, received {delay}"
            )

    # -- logging (spec: "Log CommandSent (or ack send)...; on delivery log
    # MessageDelivered") -----------------------------------------------------

    def _log_command_sent(self, message: Command | Ack, expected_arrival_ms: int) -> None:
        self._append(
            message,
            EventType.COMMAND_SENT,
            {
                "kind": "command" if isinstance(message, Command) else "ack",
                "label": message.action.value if isinstance(message, Command) else message.ack_type.value,
                "expected_arrival_ms": expected_arrival_ms,
            },
        )

    def _log_message_delivered(self, message: Command | Ack) -> None:
        self._append(message, EventType.MESSAGE_DELIVERED, {})

    def _append(self, message: Command | Ack, event_type: EventType, extra: dict) -> None:
        facility_id = message.target_facility if isinstance(message, Command) else message.facility_id
        self._store.append(
            Event(
                transport_id=message.transport_id,
                epoch=message.epoch,
                ts_ms=self._clock.now_ms(),
                type=event_type,
                facility_id=facility_id,
                payload={"command_id": message.command_id, **extra},
            )
        )
