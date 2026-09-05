"""The facility simulator: one Facility instance per hospital endpoint. It
owns exactly one thing — applying the validation pipeline and transition
table to incoming commands for the transports it knows about — and nothing
else. It never sends a command, never talks to another facility, and never
inspects the bus beyond calling send() on it.

Read top to bottom: receive_command() is the pipeline (spec steps 1-6, in
order); the _handle_* methods below it are the transition table, one per
row; the _log_* methods are what each pipeline step logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from app.acceptance import accept
from app.clock import Clock, Handle
from app.config import Config
from app.events import Event, EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command, FacilityState
from app.models import Hospital, HospitalStatus, Patient, initial_status
from app.projection import apply_status_event as _apply_status_event
from app.projection import empty_ledger_view, project_ledger, without_transport

# Every clock.schedule() a Facility makes for a transport is registered here
# so the epoch fence (step 4) can cancel *any* stale pending action — a
# deferred READY just as much as a scheduled ACTIVATE_AT/WITHDRAW_AT —
# without the fence needing to know which action types happen to schedule.
_ScheduledKey = tuple[int, ActionType]


class MessageSender(Protocol):
    """What a Facility needs from the bus: send it a message. Defined here
    (not imported from bus.py, which does not exist until Phase 3) so a
    Phase 2 test can hand a Facility a plain recording stub — the real Bus
    satisfies this structurally, no inheritance required."""

    def send(self, message: Command | Ack) -> None: ...


@dataclass
class _TransportRecord:
    state: FacilityState = FacilityState.IDLE
    highest_applied_epoch: int = 0
    processed: dict[str, Ack] = field(default_factory=dict)
    scheduled: dict[_ScheduledKey, Handle] = field(default_factory=dict)
    pending_ready: list[Ack] = field(default_factory=list)
    # Phase 14 (F2): the PREPARE command_id that armed this transport, so
    # decline_now() — not itself triggered by an incoming command — can
    # still send a DECLINED ack the dispatcher recognizes (it fences acks
    # by matching this same command_id against its own prepare_command_id).
    armed_command_id: Optional[str] = None


class Facility:
    def __init__(
        self,
        facility_id: str,
        clock: Clock,
        store: EventStore,
        bus: MessageSender,
        config: Config,
        manual_ready: bool = False,
        hospital: Optional[Hospital] = None,
    ) -> None:
        self.id = facility_id
        self._clock = clock
        self._store = store
        self._bus = bus
        self._config = config
        self._manual_ready = manual_ready
        self._transports: dict[str, _TransportRecord] = {}
        # Phase 14: `hospital` is None for every pre-existing caller (the
        # plain 3-hospital demo never passes it) — PREPARE always arms
        # unconditionally in that case, exactly as before. Capacity-aware
        # callers (Phase 15's Simulation) pass the Hospital this facility
        # represents; `_status` then tracks its live state, replayed purely
        # from HospitalStatusChanged/BedsReported (see on_status_event).
        self._hospital = hospital
        self._status: Optional[HospitalStatus] = initial_status(hospital) if hospital is not None else None
        self._known_patients: dict[str, Patient] = {}
        self._ledger_cache: Optional[tuple[int, object]] = None  # (store revision, this hospital's view)
        self._transitions: dict[
            tuple[FacilityState, ActionType], Callable[[_TransportRecord, Command], None]
        ] = {
            (FacilityState.IDLE, ActionType.PREPARE): self._handle_prepare,
            (FacilityState.WITHDRAWN, ActionType.PREPARE): self._handle_prepare,
            (FacilityState.ARMED, ActionType.PREPARE): self._handle_prepare,
            # A PREPARE only ever reaches here at a fresh, fenced-in epoch
            # (pipeline step 4 already rejected anything stale) — so a
            # facility still locally ACTIVE from an abandoned plan is being
            # asked to prepare for a genuinely new one. Without this row,
            # such a PREPARE is IllegalTransition and this facility is
            # orphaned ACTIVE forever: the epoch fence that let the PREPARE
            # through also cancels this facility's own still-pending
            # WITHDRAW_AT from the old plan (same fence, "cancel every
            # scheduled action with a lower epoch"), so nothing else would
            # ever move it out of ACTIVE.
            (FacilityState.ACTIVE, ActionType.PREPARE): self._handle_prepare,
            (FacilityState.ARMED, ActionType.ACTIVATE_AT): self._handle_activate_at,
            (FacilityState.ACTIVE, ActionType.WITHDRAW_AT): self._handle_withdraw_at,
            (FacilityState.ARMED, ActionType.WITHDRAW): self._handle_withdraw,
            (FacilityState.ACTIVE, ActionType.WITHDRAW): self._handle_withdraw,
            (FacilityState.ACTIVE, ActionType.REMAIN): self._handle_remain,
        }

    # -- read-only views for tests and the projection layer -----------------

    def state_of(self, transport_id: str) -> FacilityState:
        return self._record_for(transport_id).state

    def highest_applied_epoch_of(self, transport_id: str) -> int:
        return self._record_for(transport_id).highest_applied_epoch

    def status_of(self) -> Optional[HospitalStatus]:
        """None for a plain (non-capacity-aware) facility — see __init__."""
        return self._status

    def set_manual_ready(self, enabled: bool) -> None:
        """Runtime toggle for the demo's manual-mode control (Phase 9) —
        Phase 2's manual_ready was construction-time only, which the demo
        can't use since all three Facility instances are built once, before
        the operator has said anything about how they want to run this
        transport's scenario."""
        self._manual_ready = enabled

    # -- the pipeline (spec: "run in this exact order on every command") ----

    def receive_command(self, command: Command) -> None:
        if command.target_facility != self.id:
            self._log_target_mismatch(command)
            return

        record = self._record_for(command.transport_id)

        if command.command_id in record.processed:
            self._log_duplicate_ignored(command)
            self._bus.send(record.processed[command.command_id])
            return

        if command.epoch < record.highest_applied_epoch:
            self._log_stale_ignored(command, record)
            return

        if command.epoch > record.highest_applied_epoch:
            # Raise the fence even if the transition below turns out to be
            # illegal (E17): a later stale command at the old epoch must
            # still be rejected.
            self._raise_fence(record, command.epoch)

        handler = self._transitions.get((record.state, command.action))
        if handler is None:
            self._log_illegal_transition(command, record)
            return
        handler(record, command)

    # -- confirm_ready(): the manual_ready escape hatch for tests -----------

    def confirm_ready(self, transport_id: str) -> None:
        """With manual_ready=True, PREPARE never auto-sends READY — this is
        the only way it goes out, letting a test hold a facility "not ready
        yet" for as long as it wants (used to exercise E9's retry/abort path
        without waiting through PREP_MS)."""
        record = self._record_for(transport_id)
        pending, record.pending_ready = record.pending_ready, []
        for ack in pending:
            self._bus.send(ack)

    # -- F2: decline_now() — withdrawing an acceptance before commitment ----

    def decline_now(self, transport_id: str, reason: str) -> None:
        """Used by the status-drift routine (F4) when a bed or specialist a
        transport was accepted for is lost before commitment. Allowed only
        while ARMED with no ACTIVATE_AT scheduled yet — once RECEIVED for
        ACTIVATE_AT has gone out, this facility is committed (R16 mirrors
        this on the dispatcher's side: a DECLINED arriving after that point
        is LateDeclineIgnored, never processed)."""
        record = self._record_for(transport_id)
        has_scheduled_activate = any(action is ActionType.ACTIVATE_AT for _epoch, action in record.scheduled)
        if record.state is not FacilityState.ARMED or has_scheduled_activate:
            raise ValueError("committed")

        old_state = record.state
        record.state = FacilityState.WITHDRAWN
        epoch = record.highest_applied_epoch
        command_id = record.armed_command_id or f"decline-now-{transport_id}"
        self._store.append(
            Event(
                transport_id=transport_id, epoch=epoch, ts_ms=self._clock.now_ms(),
                type=EventType.FACILITY_STATE_CHANGED, facility_id=self.id,
                payload={"from": old_state.value, "to": FacilityState.WITHDRAWN.value, "command_id": command_id, "action": "decline_now"},
            )
        )
        ack = Ack(
            command_id=command_id, transport_id=transport_id, facility_id=self.id, epoch=epoch,
            ack_type=AckType.DECLINED, applied_state=FacilityState.WITHDRAWN,
            sent_at_ms=self._clock.now_ms(), reason=reason,
        )
        self._bus.send(ack)

    # -- F3: live status, replayed the same way as everything else ----------

    def set_hospital(self, hospital) -> None:
        """Phase 21: re-point this facility at an updated Hospital record.

        The live status is kept, not rebuilt from initial_status(): status is
        event-sourced and belongs to the facility's history, while the
        Hospital record is its static configuration. Re-deriving it here
        would silently wipe a diversion or an off-shift specialist every time
        an administrator renamed the hospital."""
        self._hospital = hospital

    def restore_status(self, status) -> None:
        """Replace the live status wholesale, without emitting anything.

        Used only when rebuilding from the log at startup (E21): the events
        that produced this status are already persisted, so re-applying them
        through the normal path would duplicate them."""
        self._status = status

    def on_status_event(self, event: Event) -> None:
        """Called with a HospitalStatusChanged/BedsReported event as it's
        appended (Phase 15 wires this the same way Hub subscribes to the
        store) — updates this facility's live status via the pure
        projection.apply_status_event(), so the status F1 checks against is
        always exactly what replaying the log would produce, never
        separately-mutated state that could drift from it."""
        if self._status is None:
            return  # a plain (non-capacity-aware) facility has no status to update
        self._status = _apply_status_event(self._status, event)

    # -- transition table (spec table, one method per row) -------------------

    def _handle_prepare(self, record: _TransportRecord, command: Command) -> None:
        already_armed = record.state == FacilityState.ARMED

        # F1: acceptance runs once, on the first PREPARE for a capacity-
        # aware transport — a resend while already ARMED just re-confirms
        # (the transition table's own "PREPARE (same or higher epoch) ->
        # stay ARMED" row), it doesn't re-litigate a decision already made.
        if self._hospital is not None and command.patient is not None and not already_armed:
            self._known_patients[command.transport_id] = command.patient
            eta = command.eta_minutes if command.eta_minutes is not None else 0.0
            # Excluding this transport's own reservation: the dispatcher has
            # already reserved a bed before sending this PREPARE, and counting
            # it made the hospital decline its own applicant for the last bed.
            view = without_transport(self._ledger_view(), command.transport_id)
            decision = accept(command.patient, self._hospital, self._status, view, eta, self._config.saturation_limit)
            if not decision.accepted:
                self._decline(record, command, decision.reason)
                return

        self._apply_state(record, command, FacilityState.ARMED)
        record.armed_command_id = command.command_id
        ack = self._build_ack(command, AckType.READY, FacilityState.ARMED)
        record.processed[command.command_id] = ack

        if self._manual_ready:
            record.pending_ready.append(ack)
        elif already_armed:
            # Already fully prepped (this is a resend, e.g. the dispatcher's
            # one-shot retry per R3) — no more prep work to simulate.
            self._bus.send(ack)
        else:
            key = (command.epoch, ActionType.PREPARE)
            handle = self._clock.schedule(
                self._config.prep_ms, lambda: self._send_scheduled(record, key, ack)
            )
            record.scheduled[key] = handle

    def _ledger_view(self):
        """This facility's own view of the bed ledger, derived from the log
        (the facility is not the ledger's writer — the dispatcher is — so it
        can't mirror it incrementally the way dispatcher.py does). Memoized
        against the store's revision so repeated PREPAREs between appends
        don't each replay the whole log."""
        revision = self._store.revision
        if self._ledger_cache is None or self._ledger_cache[0] != revision:
            ledger = project_ledger(self._store.replay(), self._known_patients)
            self._ledger_cache = (revision, ledger.get(self.id, empty_ledger_view(self.id)))
        return self._ledger_cache[1]

    def _decline(self, record: _TransportRecord, command: Command, reason: str) -> None:
        """F1's decline path: state is left unchanged (the transition table
        row this would-be transition maps to never actually fires), the
        epoch fence was already raised by the pipeline's step 4 before this
        ever ran, and DECLINED goes out in place of READY."""
        ack = Ack(
            command_id=command.command_id, transport_id=command.transport_id, facility_id=self.id,
            epoch=command.epoch, ack_type=AckType.DECLINED, applied_state=record.state,
            sent_at_ms=self._clock.now_ms(), reason=reason,
        )
        record.processed[command.command_id] = ack
        self._store.append(
            Event(
                transport_id=command.transport_id, epoch=command.epoch, ts_ms=self._clock.now_ms(),
                type=EventType.FACILITY_STATE_CHANGED, facility_id=self.id,
                payload={
                    "from": record.state.value, "to": record.state.value,
                    "command_id": command.command_id, "action": command.action.value, "declined": reason,
                },
            )
        )
        self._bus.send(ack)

    def _handle_activate_at(self, record: _TransportRecord, command: Command) -> None:
        self._handle_scheduled_transition(record, command, FacilityState.ACTIVE, ActionType.ACTIVATE_AT)

    def _handle_withdraw_at(self, record: _TransportRecord, command: Command) -> None:
        self._handle_scheduled_transition(record, command, FacilityState.WITHDRAWN, ActionType.WITHDRAW_AT)

    def _handle_scheduled_transition(
        self,
        record: _TransportRecord,
        command: Command,
        target_state: FacilityState,
        action: ActionType,
    ) -> None:
        # RECEIVED goes out immediately regardless of timing — it is what
        # R5 waits on before releasing the old facility, so it must never
        # be skipped even when effective_at is already in the past (E16).
        received = self._build_ack(command, AckType.RECEIVED, record.state)
        record.processed[command.command_id] = received
        self._bus.send(received)

        assert command.effective_at_ms is not None  # enforced by Command's validator
        key = (command.epoch, action)
        self._replace_scheduled(record, key)

        def apply() -> None:
            self._apply_state(record, command, target_state)
            applied = self._build_ack(command, AckType.APPLIED, target_state)
            self._bus.send(applied)
            record.scheduled.pop(key, None)

        now = self._clock.now_ms()
        if command.effective_at_ms <= now:
            apply()
        else:
            record.scheduled[key] = self._clock.schedule(command.effective_at_ms - now, apply)

    def _handle_withdraw(self, record: _TransportRecord, command: Command) -> None:
        # WITHDRAW is immediate and unconditional: whatever this transport
        # had scheduled (an ACTIVATE_AT or WITHDRAW_AT still pending) is
        # moot the instant this facility is told to stand down.
        for key, handle in list(record.scheduled.items()):
            self._clock.cancel(handle)
            del record.scheduled[key]
        self._apply_state(record, command, FacilityState.WITHDRAWN)
        ack = self._build_ack(command, AckType.APPLIED, FacilityState.WITHDRAWN)
        record.processed[command.command_id] = ack
        self._bus.send(ack)

    def _handle_remain(self, record: _TransportRecord, command: Command) -> None:
        # Unlike WITHDRAW, REMAIN touches nothing but the ack: any lower-
        # epoch schedule was already cleared by the fence (step 4), and a
        # same-epoch one is deliberately left alone (E27).
        self._apply_state(record, command, FacilityState.ACTIVE)
        ack = self._build_ack(command, AckType.APPLIED, FacilityState.ACTIVE)
        record.processed[command.command_id] = ack
        self._bus.send(ack)

    # -- shared mechanics -----------------------------------------------------

    def _record_for(self, transport_id: str) -> _TransportRecord:
        return self._transports.setdefault(transport_id, _TransportRecord())

    def _raise_fence(self, record: _TransportRecord, new_epoch: int) -> None:
        for key, handle in list(record.scheduled.items()):
            epoch, _action = key
            if epoch < new_epoch:
                self._clock.cancel(handle)
                del record.scheduled[key]
        record.pending_ready = [ack for ack in record.pending_ready if ack.epoch >= new_epoch]
        record.highest_applied_epoch = new_epoch

    def _replace_scheduled(self, record: _TransportRecord, key: _ScheduledKey) -> None:
        """At most one scheduled action per (epoch, action): a re-sent
        ACTIVATE_AT/WITHDRAW_AT for the same epoch replaces, not stacks on
        top of, whatever this facility already had scheduled for it."""
        existing = record.scheduled.pop(key, None)
        if existing is not None:
            self._clock.cancel(existing)

    def _send_scheduled(self, record: _TransportRecord, key: _ScheduledKey, ack: Ack) -> None:
        record.scheduled.pop(key, None)
        self._bus.send(ack)

    def _apply_state(self, record: _TransportRecord, command: Command, new_state: FacilityState) -> None:
        old_state = record.state
        record.state = new_state
        self._store.append(
            Event(
                transport_id=command.transport_id,
                epoch=command.epoch,
                ts_ms=self._clock.now_ms(),
                type=EventType.FACILITY_STATE_CHANGED,
                facility_id=self.id,
                payload={
                    "from": old_state.value,
                    "to": new_state.value,
                    "command_id": command.command_id,
                    "action": command.action.value,
                },
            )
        )

    def _build_ack(self, command: Command, ack_type: AckType, applied_state: FacilityState) -> Ack:
        return Ack(
            command_id=command.command_id,
            transport_id=command.transport_id,
            facility_id=self.id,
            epoch=command.epoch,
            ack_type=ack_type,
            applied_state=applied_state,
            sent_at_ms=self._clock.now_ms(),
        )

    # -- logging (spec: each rejected pipeline step logs its own event) -----

    def _log_target_mismatch(self, command: Command) -> None:
        self._append_log(command, EventType.TARGET_MISMATCH, {"target_facility": command.target_facility})

    def _log_duplicate_ignored(self, command: Command) -> None:
        self._append_log(command, EventType.DUPLICATE_IGNORED, {"action": command.action.value})

    def _log_stale_ignored(self, command: Command, record: _TransportRecord) -> None:
        self._append_log(
            command,
            EventType.STALE_IGNORED,
            {"action": command.action.value, "highest_applied_epoch": record.highest_applied_epoch},
        )

    def _log_illegal_transition(self, command: Command, record: _TransportRecord) -> None:
        self._append_log(
            command,
            EventType.ILLEGAL_TRANSITION,
            {"action": command.action.value, "state": record.state.value},
        )

    def _append_log(self, command: Command, event_type: EventType, extra: dict) -> None:
        self._store.append(
            Event(
                transport_id=command.transport_id,
                epoch=command.epoch,
                ts_ms=self._clock.now_ms(),
                type=event_type,
                facility_id=self.id,
                payload={"command_id": command.command_id, **extra},
            )
        )
