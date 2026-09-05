"""The dispatcher: the single authority that may change a transport's
destination. Everything else in this file exists to make one guarantee true
regardless of delay, duplication or reordering: the old facility is only
ever told to withdraw after the new one has proven its activation is
scheduled (R5) — so a cutover can never pass through a moment with nobody
active. Read top to bottom: start, redirect, then the ack pipeline and the
rules it drives, in the same order they appear in the spec (R1-R12).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Optional, Protocol

from app.clock import Clock, Handle
from app.config import Config
from app.events import Event, EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command


class DispatcherStatus(str, Enum):
    STARTING = "STARTING"
    STABLE = "STABLE"
    PREPARING = "PREPARING"
    CUTOVER_SCHEDULED = "CUTOVER_SCHEDULED"
    NOT_READY = "NOT_READY"


class MessageSender(Protocol):
    """What the dispatcher needs from the bus: send a command. Defined here,
    not imported from bus.py, for the same reason facility.py defines its
    own — the real Bus satisfies it structurally, no inheritance required."""

    def send(self, message: Command | Ack) -> None: ...


@dataclass
class _TransportState:
    current_destination: Optional[str] = None
    current_epoch: int = 0
    pending_destination: Optional[str] = None
    pending_epoch: Optional[int] = None
    highest_epoch_seen: int = 0
    cutover_at: Optional[int] = None
    activate_command_id: Optional[str] = None
    withdraw_sent: bool = False
    queued_redirect: Optional[str] = None
    status: DispatcherStatus = DispatcherStatus.STARTING

    # Bookkeeping the spec names conceptually (R2's ack fence, R3's two
    # preconditions) but doesn't give a field name to:
    prepare_command_id: Optional[str] = None
    notice_command_id: Optional[str] = None
    ready_received: bool = False
    notice_applied: bool = False
    ready_retried: bool = False
    known_command_ids: set[str] = field(default_factory=set)
    seen_acks: set[tuple[str, AckType]] = field(default_factory=set)
    ready_timer: Optional[Handle] = None
    cutover_timer: Optional[Handle] = None
    receipt_deadline_timer: Optional[Handle] = None


class Dispatcher:
    def __init__(self, clock: Clock, store: EventStore, bus: MessageSender, config: Config) -> None:
        self._clock = clock
        self._store = store
        self._bus = bus
        self._config = config
        self._transports: dict[str, _TransportState] = {}
        self._command_seq = count(1)

    # -- read-only views for tests and the projection layer -----------------

    def status_of(self, transport_id: str) -> DispatcherStatus:
        return self._require_transport(transport_id).status

    def current_destination_of(self, transport_id: str) -> Optional[str]:
        return self._require_transport(transport_id).current_destination

    def pending_destination_of(self, transport_id: str) -> Optional[str]:
        return self._require_transport(transport_id).pending_destination

    def current_epoch_of(self, transport_id: str) -> int:
        return self._require_transport(transport_id).current_epoch

    def queued_redirect_of(self, transport_id: str) -> Optional[str]:
        return self._require_transport(transport_id).queued_redirect

    # -- R10: start ------------------------------------------------------

    def start(self, transport_id: str, destination: str) -> None:
        if transport_id in self._transports:
            raise ValueError(f"Expected transport {transport_id!r} to be new, it was already started")
        record = _TransportState(
            pending_destination=destination,
            pending_epoch=1,
            highest_epoch_seen=1,
            status=DispatcherStatus.STARTING,
        )
        self._transports[transport_id] = record
        self._log(transport_id, 1, EventType.TRANSPORT_STARTED, {"destination": destination})
        record.prepare_command_id = self._send(transport_id, record, destination, 1, ActionType.PREPARE)
        record.notice_command_id = self._send(transport_id, record, destination, 1, ActionType.REDIRECT_NOTICE)
        # No READY-timeout/retry/abort here: R10 doesn't describe one, and
        # there is no "current" facility yet for an abort to fall back to.

    # -- R1, R6, R8, R9: redirect -----------------------------------------

    def redirect(self, transport_id: str, target: str) -> None:
        record = self._require_transport(transport_id)

        if target == record.pending_destination:
            return  # R9: already pending this exact target

        if record.current_destination is None:
            # Still inside start()'s own flow: there is no "current" facility
            # yet to protect or fall back to, so R6/R7/R8's machinery (which
            # all assume one exists) doesn't apply — see
            # _redirect_before_first_activation for what happens instead.
            self._redirect_before_first_activation(transport_id, record, target)
            return

        if record.pending_destination is None:
            if target == record.current_destination:
                return  # R8, no pending transition: no-op
            epoch = self._allocate_epoch(record)
            self._begin_redirect(transport_id, record, target, epoch)
            return

        # A transition is already pending — R6/R8's cancellable-or-queue rule.
        if target == record.current_destination:
            # R8: redirect back to current while pending.
            if self._cancellable(record):
                epoch = self._cancel_pending(transport_id, record)
                self._send(transport_id, record, record.current_destination, epoch, ActionType.REDIRECT_NOTICE)
            else:
                self._queue_redirect(transport_id, record, target)
            return

        # R6: redirect to a third target while pending.
        if self._cancellable(record):
            epoch = self._cancel_pending(transport_id, record)
            self._begin_redirect(transport_id, record, target, epoch)
        else:
            self._queue_redirect(transport_id, record, target)

    # -- R2: on_ack, fence first -----------------------------------------

    def on_ack(self, ack: Ack) -> None:
        record = self._transports.get(ack.transport_id)
        if record is None:
            return  # an ack for a transport this dispatcher never started

        if ack.epoch < record.highest_epoch_seen:
            self._log(ack.transport_id, ack.epoch, EventType.STALE_IGNORED, {"command_id": ack.command_id, "reason": "epoch"})
            return
        if ack.command_id not in record.known_command_ids:
            self._log(ack.transport_id, ack.epoch, EventType.STALE_IGNORED, {"command_id": ack.command_id, "reason": "unknown_command"})
            return
        if (ack.command_id, ack.ack_type) in record.seen_acks:
            self._log(ack.transport_id, ack.epoch, EventType.DUPLICATE_IGNORED, {"command_id": ack.command_id})
            return

        record.seen_acks.add((ack.command_id, ack.ack_type))
        self._log(ack.transport_id, ack.epoch, EventType.ACK_RECEIVED, {"command_id": ack.command_id, "ack_type": ack.ack_type.value})
        self._route_ack(ack, record)

    def _route_ack(self, ack: Ack, record: _TransportState) -> None:
        transport_id = ack.transport_id
        is_starting = record.status == DispatcherStatus.STARTING
        if ack.command_id == record.prepare_command_id and ack.ack_type is AckType.READY:
            self._on_ready(transport_id, record)
        elif ack.command_id == record.notice_command_id and ack.ack_type is AckType.APPLIED:
            self._on_notice_applied(transport_id, record)
        elif ack.command_id == record.activate_command_id and ack.ack_type is AckType.RECEIVED and not is_starting:
            # R5 only: start() has no "current" facility to withdraw, so its
            # own RECEIVED needs no follow-up (R10 only acts on READY/APPLIED).
            self._on_activate_received(transport_id, record)
        elif ack.command_id == record.activate_command_id and ack.ack_type is AckType.APPLIED and is_starting:
            # start()'s finalize step (R10). A redirect's cutover_timer is
            # scheduled before its ACTIVATE_AT is even sent, so it always
            # clears activate_command_id before this ack could arrive for
            # that flow — this branch is start()'s alone.
            self._on_activate_applied(transport_id, record)
        # WITHDRAW_AT's RECEIVED/APPLIED, WITHDRAW's APPLIED and REMAIN's
        # APPLIED need no further routing: the generic AckReceived log above
        # is the whole story for them.

    def _on_ready(self, transport_id: str, record: _TransportState) -> None:
        record.ready_received = True
        if record.status == DispatcherStatus.STARTING:
            record.activate_command_id = self._send(
                transport_id, record, record.pending_destination, record.pending_epoch,
                ActionType.ACTIVATE_AT, effective_at_ms=self._clock.now_ms(),
            )
            return
        self._schedule_cutover(transport_id, record)

    # -- R4: schedule_cutover (activate only) -----------------------------

    def _schedule_cutover(self, transport_id: str, record: _TransportState) -> None:
        if not (record.ready_received and record.notice_applied):
            return  # both preconditions required, in either order (R3)
        if record.status == DispatcherStatus.CUTOVER_SCHEDULED:
            return  # already scheduled — a second precondition can't re-fire this

        self._cancel_timer(record, "ready_timer")
        t0 = self._clock.now_ms()
        cutover_at = t0 + 3 * self._config.d_max_ms + self._config.guard_ms
        record.cutover_at = cutover_at
        self._log(transport_id, record.pending_epoch, EventType.CUTOVER_SCHEDULED, {"cutover_at": cutover_at})

        record.activate_command_id = self._send(
            transport_id, record, record.pending_destination, record.pending_epoch,
            ActionType.ACTIVATE_AT, effective_at_ms=cutover_at,
        )
        record.cutover_timer = self._clock.schedule(cutover_at - t0, lambda: self._apply_cutover(transport_id, record))
        receipt_deadline_ms = t0 + 2 * self._config.d_max_ms + self._config.guard_ms
        record.receipt_deadline_timer = self._clock.schedule(
            receipt_deadline_ms - t0, lambda: self._on_receipt_deadline(transport_id, record)
        )
        record.status = DispatcherStatus.CUTOVER_SCHEDULED

    # -- R5: on_received -> send withdraw -----------------------------------

    def _on_activate_received(self, transport_id: str, record: _TransportState) -> None:
        if record.withdraw_sent:
            return  # ack dedup already guards this, but never send it twice
        self._cancel_timer(record, "receipt_deadline_timer")
        command_id = self._send(
            transport_id, record, record.current_destination, record.pending_epoch,
            ActionType.WITHDRAW_AT, effective_at_ms=record.cutover_at,
        )
        record.withdraw_sent = True
        self._log(transport_id, record.pending_epoch, EventType.WITHDRAW_SENT, {"command_id": command_id})

    # -- R5: receipt deadline -> abort ---------------------------------------

    def _on_receipt_deadline(self, transport_id: str, record: _TransportState) -> None:
        if record.withdraw_sent:
            return  # RECEIVED arrived in time; this timer should already be cancelled
        self._log(transport_id, record.pending_epoch, EventType.BOUND_EXCEEDED, {"reason": "activate_received_timeout"})
        self._abort(transport_id, record)

    # -- R12: apply_cutover --------------------------------------------------

    def _apply_cutover(self, transport_id: str, record: _TransportState) -> None:
        record.current_destination = record.pending_destination
        record.current_epoch = record.pending_epoch
        self._clear_pending(record)
        record.status = DispatcherStatus.STABLE
        self._log(transport_id, record.current_epoch, EventType.CUTOVER_APPLIED, {"current_destination": record.current_destination})
        self._run_queued_redirect(transport_id, record)

    def _on_activate_applied(self, transport_id: str, record: _TransportState) -> None:
        # start()'s finalize step (R10): there is no cutover_timer during
        # STARTING, so this ack — not a timer — is what flips current.
        record.current_destination = record.pending_destination
        record.current_epoch = record.pending_epoch
        self._clear_pending(record)  # also retires notice/prepare tracking — see its docstring
        record.status = DispatcherStatus.STABLE
        self._log(transport_id, record.current_epoch, EventType.CUTOVER_APPLIED, {"current_destination": record.current_destination})

    # -- R3: ready timeout, retry once, then abort ---------------------------

    def _start_ready_timer(self, transport_id: str, record: _TransportState) -> None:
        record.ready_timer = self._clock.schedule(
            self._config.ready_timeout_ms, lambda: self._on_ready_timeout(transport_id, record)
        )

    def _on_ready_timeout(self, transport_id: str, record: _TransportState) -> None:
        if record.ready_received and record.notice_applied:
            return  # defensive: this timer should already be cancelled by then
        if not record.ready_received and not record.ready_retried:
            record.ready_retried = True
            record.prepare_command_id = self._send(
                transport_id, record, record.pending_destination, record.pending_epoch, ActionType.PREPARE
            )
            self._start_ready_timer(transport_id, record)
            return
        # Either READY never came back even after one retry, or READY is
        # fine and the ambulance notice is the one still missing (E26) —
        # both give up the same way.
        self._abort(transport_id, record)

    def _abort(self, transport_id: str, record: _TransportState) -> None:
        epoch = self._allocate_epoch(record)
        self._cancel_timers(record)
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REMAIN)
        self._send(transport_id, record, record.pending_destination, epoch, ActionType.WITHDRAW)
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REDIRECT_NOTICE)
        record.current_epoch = epoch
        self._clear_pending(record)
        record.status = DispatcherStatus.NOT_READY
        self._log(transport_id, epoch, EventType.REDIRECT_ABORTED, {})
        self._run_queued_redirect(transport_id, record)

    # -- R6, R8, R9, E24: redirect-while-pending mechanics -------------------

    def _cancellable(self, record: _TransportState) -> bool:
        if not record.withdraw_sent:
            return True
        if record.cutover_at is None:
            return True  # defensive; withdraw_sent implies cutover_at is set
        return self._clock.now_ms() + self._config.d_max_ms + self._config.guard_ms < record.cutover_at

    def _cancel_pending(self, transport_id: str, record: _TransportState) -> int:
        old_pending_epoch = record.pending_epoch
        old_pending = record.pending_destination
        self._cancel_timers(record)
        epoch = self._allocate_epoch(record)
        self._log(transport_id, epoch, EventType.CUTOVER_CANCELLED, {"old_pending_epoch": old_pending_epoch})
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REMAIN)
        self._send(transport_id, record, old_pending, epoch, ActionType.WITHDRAW)
        record.current_epoch = epoch
        self._clear_pending(record)
        record.status = DispatcherStatus.STABLE
        return epoch

    def _queue_redirect(self, transport_id: str, record: _TransportState, target: str) -> None:
        # Only one queued redirect is kept — a newer one simply replaces it.
        record.queued_redirect = target
        self._log(transport_id, record.pending_epoch, EventType.REDIRECT_QUEUED, {"target": target})

    def _run_queued_redirect(self, transport_id: str, record: _TransportState) -> None:
        if record.queued_redirect is None:
            return
        target, record.queued_redirect = record.queued_redirect, None
        self.redirect(transport_id, target)

    # -- R11: ambulance notice as a cutover precondition ---------------------

    def _on_notice_applied(self, transport_id: str, record: _TransportState) -> None:
        record.notice_applied = True
        if record.status == DispatcherStatus.STARTING:
            return  # informational only during start() — no cutover to gate
        self._schedule_cutover(transport_id, record)

    # -- shared mechanics -----------------------------------------------------

    def _begin_redirect(self, transport_id: str, record: _TransportState, target: str, epoch: int) -> None:
        record.pending_destination = target
        record.pending_epoch = epoch
        record.ready_received = False
        record.notice_applied = False
        record.ready_retried = False
        record.status = DispatcherStatus.PREPARING
        self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": target})
        record.prepare_command_id = self._send(transport_id, record, target, epoch, ActionType.PREPARE)
        record.notice_command_id = self._send(transport_id, record, target, epoch, ActionType.REDIRECT_NOTICE)
        self._start_ready_timer(transport_id, record)

    def _redirect_before_first_activation(self, transport_id: str, record: _TransportState, target: str) -> None:
        """A redirect() arriving before start()'s own cutover has ever
        applied — an interaction R1-R12's text doesn't cover, since R6/R7/R8
        all assume a current facility exists to REMAIN at or fall back to.
        Fail-safe here means simply retargeting what start() is waiting on,
        at a fresh epoch, and telling the abandoned target to stand down —
        never touching a "current" that doesn't exist yet."""
        old_target = record.pending_destination
        epoch = self._allocate_epoch(record)
        self._send(transport_id, record, old_target, epoch, ActionType.WITHDRAW)
        record.pending_destination = target
        record.pending_epoch = epoch
        record.ready_received = False
        record.notice_applied = False
        record.status = DispatcherStatus.STARTING
        self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": target})
        record.prepare_command_id = self._send(transport_id, record, target, epoch, ActionType.PREPARE)
        record.notice_command_id = self._send(transport_id, record, target, epoch, ActionType.REDIRECT_NOTICE)

    def _clear_pending(self, record: _TransportState) -> None:
        record.pending_destination = None
        record.pending_epoch = None
        record.cutover_at = None
        record.activate_command_id = None
        record.withdraw_sent = False
        record.cutover_timer = None
        record.receipt_deadline_timer = None
        record.ready_timer = None
        # Also retire this concluded transition's own precondition tracking:
        # a late APPLIED for its notice (or READY for its PREPARE) is *not*
        # caught by the epoch fence — its epoch equals highest_epoch_seen,
        # not less than it — so without this, it would still match here and
        # re-trigger _schedule_cutover against a pending_epoch that's now
        # None. Once cleared, such a late ack matches nothing and is a
        # harmless no-op, the same way a cleared activate_command_id already
        # protects the redirect flow's activate-APPLIED.
        record.prepare_command_id = None
        record.notice_command_id = None
        record.ready_received = False
        record.notice_applied = False
        record.ready_retried = False

    def _allocate_epoch(self, record: _TransportState) -> int:
        epoch = record.highest_epoch_seen + 1
        record.highest_epoch_seen = epoch
        return epoch

    def _cancel_timers(self, record: _TransportState) -> None:
        self._cancel_timer(record, "ready_timer")
        self._cancel_timer(record, "cutover_timer")
        self._cancel_timer(record, "receipt_deadline_timer")

    def _cancel_timer(self, record: _TransportState, attr: str) -> None:
        handle = getattr(record, attr)
        if handle is not None:
            self._clock.cancel(handle)
            setattr(record, attr, None)

    def _send(
        self,
        transport_id: str,
        record: _TransportState,
        target: str,
        epoch: int,
        action: ActionType,
        effective_at_ms: Optional[int] = None,
    ) -> str:
        command_id = f"cmd-{next(self._command_seq)}"
        command = Command(
            command_id=command_id,
            transport_id=transport_id,
            target_facility=target,
            epoch=epoch,
            action=action,
            effective_at_ms=effective_at_ms,
            sent_at_ms=self._clock.now_ms(),
        )
        record.known_command_ids.add(command_id)
        self._bus.send(command)
        return command_id

    def _log(self, transport_id: str, epoch: int, event_type: EventType, payload: dict) -> None:
        self._store.append(
            Event(
                transport_id=transport_id,
                epoch=epoch,
                ts_ms=self._clock.now_ms(),
                type=event_type,
                facility_id=None,
                payload=payload,
            )
        )

    def _require_transport(self, transport_id: str) -> _TransportState:
        record = self._transports.get(transport_id)
        if record is None:
            raise KeyError(f"Expected transport {transport_id!r} to exist, it was never started")
        return record
