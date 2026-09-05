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

from typing import Callable, Optional, Protocol

from app.clock import Clock, Handle
from app.config import Config
from app.events import Event, EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command, FacilityState
from app.models import Hospital


def _distance_km(position: tuple[float, float], location: tuple[float, float]) -> float:
    dx, dy = location[0] - position[0], location[1] - position[1]
    return (dx * dx + dy * dy) ** 0.5


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
        hospitals: Optional[dict[str, Hospital]] = None,
        position: Optional[tuple[float, float]] = None,
        on_arrived: Optional[Callable[[str, str], None]] = None,
        on_position_changed: Optional[Callable[[str, tuple[float, float]], None]] = None,
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
        # Every REDIRECT_NOTICE with notice_seq <= this belongs to an attempt
        # the dispatcher has already abandoned (see stand_down). Notices are
        # sequenced per transport, so this is the only thing that can tell a
        # notice still in flight from the one that replaced it — the epoch
        # fence can't, because R15 reuses one epoch for a whole candidate walk.
        self._stale_notice_through: int = 0
        self._awaiting_confirm: list[Command] = []
        self._tick_handle: Optional[Handle] = None
        self._arrived = False

        # Phase 15 (M1/M2): `hospitals`/`position` are None for every
        # pre-existing caller (the plain 3-hospital demo never passes them)
        # — movement stays the fixed-tick-count model below unchanged.
        # Capacity-aware callers (Simulation's batch start) pass both, and
        # get real 2D movement toward the current destination's location
        # instead — see _tick().
        self._hospitals = hospitals
        self.position = position
        self._on_arrived = on_arrived
        self._on_position_changed = on_position_changed
        self._remaining_km: Optional[float] = None
        self._initial_distance_km: Optional[float] = None
        # Ticks elapsed on the current leg, and the minimum a leg may take.
        # Without a floor the nearest-hospital rule makes most journeys
        # almost instant (and a patient generated on a hospital's own
        # coordinates arrives on the first tick), which leaves no window to
        # redirect a transport while it is actually moving.
        self._leg_ticks = 0
        self._min_leg_ticks = max(1, round(config.min_travel_ms / config.tick_ms))
        # A leg must take meaningfully longer than the dispatcher's own
        # worst case for getting this transport a *correct* notice again —
        # otherwise the ambulance could physically "arrive" before that,
        # which the checker correctly flags as ARRIVED_AT_WRONG_FACILITY
        # even though nothing unsafe happened. That worst case isn't just
        # one happy-path redirect (PREP_MS + 2*D_MAX + 3*D_MAX + GUARD from
        # redirect to cutover) — R9's own retry-then-abort path (one READY
        # timeout, one retry, a second READY timeout, then the abort's own
        # message round trip) can legitimately take far longer, and it ends
        # with a fresh REDIRECT_NOTICE the same as any other outcome, so the
        # ambulance must not finish a leg before that could have happened.
        worst_case_redirect_ms = config.prep_ms + 5 * config.d_max_ms + config.guard_ms
        worst_case_retry_abort_ms = 2 * config.ready_timeout_ms + config.d_max_ms + config.guard_ms
        worst_case_ms = worst_case_redirect_ms + worst_case_retry_abort_ms
        self._ticks_per_leg = max(1, round(2 * worst_case_ms / config.tick_ms))

    def set_manual_confirm(self, enabled: bool) -> None:
        """Runtime toggle for the demo's manual-mode control (Phase 9),
        mirroring Facility.set_manual_ready — this ambulance is constructed
        once per transport, before the operator has chosen a mode for it."""
        self._manual_confirm = enabled

    # -- the fence (spec: "same fence as facilities, steps 2-3") -------------

    def receive_command(self, command: Command) -> None:
        if command.action is not ActionType.REDIRECT_NOTICE:
            return  # the ambulance only ever receives this one action
        if command.notice_seq is not None and command.notice_seq <= self._stale_notice_through:
            # Issued before the dispatcher stood this transport down, and only
            # now arriving. Applying it would point the ambulance back at a
            # hospital that has already declined and released its bed, which
            # is exactly the ARRIVED_WITHOUT_RESERVATION (I3) that E33
            # forbids. No ack: that attempt is over, and the dispatcher has
            # already cleared the command id it would have matched.
            self._log(
                EventType.STALE_IGNORED,
                command.epoch,
                {
                    "command_id": command.command_id,
                    "notice_seq": command.notice_seq,
                    "stale_notice_through": self._stale_notice_through,
                },
            )
            return
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
            self._leg_ticks = 0
            if self._hospitals is not None and self.position is not None:
                hospital = self._hospitals.get(command.target_facility)
                distance_km = _distance_km(self.position, hospital.location) if hospital is not None else 0.0
                self._initial_distance_km = distance_km
                self._remaining_km = distance_km
            if self._tick_handle is None:
                self._schedule_next_tick()

        ack = self._build_ack(command)
        self._processed[command.command_id] = ack
        if self._manual_confirm:
            self._awaiting_confirm.append(command)
        else:
            self._bus.send(ack)

    def stand_down(self, through_seq: int = 0) -> None:
        """The dispatcher has nowhere to send this transport — every
        candidate declined and there is no current destination to fall back
        to (R15's NoAcceptingFacility during an initial placement).

        Without this the ambulance keeps driving toward the hospital named
        in the notice it was already given, and eventually "arrives" at a
        hospital that declined it and released its bed — which checker.py
        flags as I3 ARRIVED_WITHOUT_RESERVATION, and which E33 says the
        dispatcher must never actually produce. Standing down leaves the
        transport parked with no destination (status NO_ACCEPTING_FACILITY)
        until an operator replans it; the next REDIRECT_NOTICE restarts
        ticking on its own.

        `through_seq` is the highest notice the dispatcher had issued for
        this transport when it gave up. Clearing known_destination alone is
        not enough — a notice already on the wire lands afterwards and
        re-points the ambulance at the refused hospital — so anything up to
        that seq is fenced off for good."""
        self._stale_notice_through = max(self._stale_notice_through, through_seq)
        if self._tick_handle is not None:
            self._clock.cancel(self._tick_handle)
            self._tick_handle = None
        self.known_destination = None
        self._remaining_km = None
        self._initial_distance_km = None
        self.progress = 0.0
        self._leg_ticks = 0

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

        if self._hospitals is not None and self.position is not None:
            self._tick_toward_position()
        else:
            self.progress = min(1.0, self.progress + 1.0 / self._ticks_per_leg)

        self._leg_ticks += 1
        if self.progress >= 1.0 and self._leg_ticks >= self._min_leg_ticks:
            self._arrived = True
            self._log(EventType.ARRIVED, self.highest_applied_epoch, {"at": self.known_destination})
            if self._on_arrived is not None and self.known_destination is not None:
                self._on_arrived(self.transport_id, self.known_destination)
        else:
            self._schedule_next_tick()

    def _tick_toward_position(self) -> None:
        # M1: real 2D movement — the fixed-leg-length model above is only
        # for the plain (non-capacity-aware) demo; here, "arrival" is
        # genuinely tied to distance covered, not an arbitrary tick count.
        hospital = self._hospitals.get(self.known_destination) if self.known_destination else None
        if hospital is None or self._remaining_km is None:
            return
        # sim_time_scale compresses wall-clock only — the ETA the dispatcher
        # scored this hospital on still uses the true speed. See config.py.
        step_km = (
            self._config.speed_km_per_min
            * self._config.tick_ms
            / 60000.0
            * self._config.sim_time_scale
        )
        if self._initial_distance_km:
            # Never cover the whole trip faster than the floor allows: the
            # ambulance should be visibly in transit for the whole leg, not
            # parked at the door waiting for the clock. Only ever slows a
            # step down, so a long transport is untouched.
            step_km = min(step_km, self._initial_distance_km / self._min_leg_ticks)
        dx, dy = hospital.location[0] - self.position[0], hospital.location[1] - self.position[1]
        distance = (dx * dx + dy * dy) ** 0.5
        if distance <= 1e-9 or step_km >= distance:
            self.position = hospital.location
            self._remaining_km = 0.0
        else:
            self.position = (self.position[0] + dx / distance * step_km, self.position[1] + dy / distance * step_km)
            self._remaining_km = distance - step_km
        initial = self._initial_distance_km or 1.0
        self.progress = 1.0 if self._remaining_km <= 0.1 else min(1.0, 1.0 - self._remaining_km / initial)
        if self._on_position_changed is not None:
            self._on_position_changed(self.transport_id, self.position)

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
