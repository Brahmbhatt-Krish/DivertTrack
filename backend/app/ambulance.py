"""The ambulance: one instance per transport. It receives REDIRECT_NOTICE
commands from the dispatcher and answers APPLIED once it has registered the
destination — R3's second cutover precondition, proving the crew knows
where they're headed before the old facility is ever released. Separately,
it ticks its own progress toward known_destination and logs Arrived on
completion.

Its fence is a deliberately smaller cut of the facility pipeline (spec:
"same fence as facilities, steps 2-3") — duplicate and stale-epoch checks
only. There is no target-mismatch check (the bus already routes
REDIRECT_NOTICE by transport_id, so a wrong-address command can't arrive —
see bus.py), no transition table (there is exactly one thing to do:
register the new destination), and no scheduled actions to fence.
"""
from __future__ import annotations

from typing import Optional, Protocol

from app.clock import Clock, Handle
from app.config import Config
from app.events import Event, EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command, FacilityState


class MessageSender(Protocol):
    """What an Ambulance needs from the bus: send it a message. Mirrors
    facility.py's and dispatcher.py's identically-shaped Protocol — the
    real Bus satisfies all three structurally, no inheritance required."""

    def send(self, message: Command | Ack) -> None: ...


class Ambulance:
    def __init__(
        self,
        transport_id: str,
        clock: Clock,
        store: EventStore,
        bus: MessageSender,
        config: Config,
        manual_confirm: bool = False,
    ) -> None:
        self.transport_id = transport_id
        self._clock = clock
        self._store = store
        self._bus = bus
        self._config = config
        self._manual_confirm = manual_confirm

        self.known_destination: Optional[str] = None
        self.progress: float = 0.0
        self.highest_applied_epoch: int = 0

        self._processed: dict[str, Ack] = {}
        self._awaiting_confirm: list[Command] = []
        self._tick_handle: Optional[Handle] = None
        self._arrived = False
        # A leg must take meaningfully longer than a redirect's own
        # worst-case completion time (the spec's timing summary:
        # PREP_MS + 2*D_MAX + 3*D_MAX + GUARD from redirect to cutover) —
        # otherwise the ambulance could physically "arrive" before the
        # dispatcher has actually finished handing it over, which the
        # checker correctly flags as ARRIVED_AT_WRONG_FACILITY even though
        # nothing unsafe happened. Doubling that worst case gives comfortable
        # room under any D_MAX_MS/GUARD_MS/PREP_MS/TICK_MS combination.
        worst_case_redirect_ms = config.prep_ms + 5 * config.d_max_ms + config.guard_ms
        self._ticks_per_leg = max(1, round(2 * worst_case_redirect_ms / config.tick_ms))

    # -- the fence (spec: "same fence as facilities, steps 2-3") -------------

    def receive_command(self, command: Command) -> None:
        if command.action is not ActionType.REDIRECT_NOTICE:
            return  # the ambulance only ever receives this one action
        if command.command_id in self._processed:
            self._log(EventType.DUPLICATE_IGNORED, command.epoch, {"command_id": command.command_id})
            self._bus.send(self._processed[command.command_id])
            return
        if command.epoch < self.highest_applied_epoch:
            self._log(
                EventType.STALE_IGNORED,
                command.epoch,
                {"command_id": command.command_id, "highest_applied_epoch": self.highest_applied_epoch},
            )
            return
        self.highest_applied_epoch = max(self.highest_applied_epoch, command.epoch)

        if command.target_facility != self.known_destination:
            self.known_destination = command.target_facility
            self.progress = 0.0
            self._arrived = False
            if self._tick_handle is None:
                self._schedule_next_tick()

        ack = self._build_ack(command)
        self._processed[command.command_id] = ack
        if self._manual_confirm:
            self._awaiting_confirm.append(command)
        else:
            self._bus.send(ack)

    def confirm(self) -> None:
        """With manual_confirm=True, APPLIED is withheld until this is
        called — simulating a slow crew that delays (and can cause the
        dispatcher to abort) the handover."""
        pending, self._awaiting_confirm = self._awaiting_confirm, []
        for command in pending:
            self._bus.send(self._processed[command.command_id])

    # -- progress ticking ----------------------------------------------------

    def _schedule_next_tick(self) -> None:
        self._tick_handle = self._clock.schedule(self._config.tick_ms, self._tick)

    def _tick(self) -> None:
        self._tick_handle = None
        if self._arrived:
            return  # redirected after already arriving would restart this
        self.progress = min(1.0, self.progress + 1.0 / self._ticks_per_leg)
        if self.progress >= 1.0:
            self._arrived = True
            self._log(EventType.ARRIVED, self.highest_applied_epoch, {"at": self.known_destination})
        else:
            self._schedule_next_tick()

    # -- mechanics -----------------------------------------------------------

    def _build_ack(self, command: Command) -> Ack:
        return Ack(
            command_id=command.command_id,
            transport_id=command.transport_id,
            facility_id=self.transport_id,
            epoch=command.epoch,
            ack_type=AckType.APPLIED,
            applied_state=FacilityState.ACTIVE,  # placeholder: an ambulance has no facility state
            sent_at_ms=self._clock.now_ms(),
        )

    def _log(self, event_type: EventType, epoch: int, extra: dict) -> None:
        self._store.append(
            Event(
                transport_id=self.transport_id,
                epoch=epoch,
                ts_ms=self._clock.now_ms(),
                type=event_type,
                facility_id=self.transport_id,
                payload=extra,
            )
        )
