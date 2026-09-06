"""The dispatcher: the single authority that may change a transport's
destination. Everything else in this file exists to make one guarantee true
regardless of delay, duplication or reordering: the old facility is only
ever told to withdraw after the new one has proven its activation is
scheduled (R5) — so a cutover can never pass through a moment with nobody
active. Read top to bottom: start, redirect, then the ack pipeline and the
rules it drives, in the same order they appear in the spec (R1-R12).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import count
from typing import Callable, Optional, Protocol

from app.acceptance import accept
from app.clock import Clock, Handle
from app.config import Config
from app.dispatcher_status import DispatcherStatus
from app.events import Event, EventStore, EventType
from app.messages import Ack, AckType, ActionType, Command
from app.models import BedType, Hospital, HospitalStatus, Need, Patient, Policy, initial_status
from app.projection import HospitalLedgerView, empty_ledger_view, without_transport
from app.scoring import eta_minutes, rank

# Pure scheduling margin for the dispatcher's own cutover-apply timer — see
# _schedule_cutover's comment on record.cutover_timer for why a real clock
# needs this even though FakeClock (and every automated test) never does.
# 25ms was enough in an isolated asyncio.run() with nothing else competing
# for the loop, but not under a real uvicorn server also handling HTTP
# requests, WebSocket broadcasts and --reload's file watching — all of
# that adds real scheduling jitter well beyond floating-point noise.
_DISPATCHER_SETTLE_MS = 150


@dataclass
class _LedgerEntry:
    """The dispatcher's live mirror of one hospital's bed ledger — see
    Dispatcher._ledger_view. reserved_order records the order reservations
    were made, for R18's "most recent reservation first" tie-break, so
    rebalancing doesn't have to scan the event log for it either."""

    reserved: dict[BedType, set[str]] = field(default_factory=dict)
    occupied: dict[BedType, set[str]] = field(default_factory=dict)
    ventilator_holders: set[str] = field(default_factory=set)
    reserved_order: dict[str, int] = field(default_factory=dict)


class TargetRequired(ValueError):
    """redirect() was called with no target under MANUAL policy, where only
    the operator may choose. A distinct type because Pydantic's own
    ValidationError is also a ValueError, and an API layer that catches the
    two together reports internal bugs as if the caller had sent a bad
    request (which is exactly what happened before this existed)."""


class NotEligible(Exception):
    """Raised by start()/redirect() for a capacity-aware transport whose
    explicit target fails accept() on the dispatcher's own view (R13) — the
    caller (Phase 17's API layer) catches this and returns HTTP 409
    {"error": "not_eligible", "reason": ...}. Nothing is logged when this is
    raised, per spec ("log nothing")."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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

    # -- Phase 12+ (multi-hospital capacity extension) ----------------------
    # `patient is None` is what marks a transport as the plain, original
    # kind (no capacity awareness at all — R1-R12 above are the entire
    # story for it); every field below is only ever touched when it isn't.
    patient: Optional[Patient] = None
    position: Optional[tuple[float, float]] = None
    current_bed_type: Optional[BedType] = None
    pending_bed_type: Optional[BedType] = None
    candidate_queue: list[str] = field(default_factory=list)
    candidates_tried: list[str] = field(default_factory=list)
    # Monotonic per transport, stamped on each REDIRECT_NOTICE (see _send).
    # Never reset by _reset_transition: it has to keep rising across a whole
    # candidate walk for the ambulance's fence to mean anything.
    notice_seq: int = 0
    # Set once the ambulance reports Arrived. The journey is over: the
    # patient is in a bed, so no capacity change may redirect them.
    arrived: bool = False


class Dispatcher:
    def __init__(
        self,
        clock: Clock,
        store: EventStore,
        bus: MessageSender,
        config: Config,
        hospitals: Optional[dict[str, Hospital]] = None,
        on_no_destination: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._clock = clock
        self._store = store
        self._bus = bus
        self._config = config
        self._transports: dict[str, _TransportState] = {}
        self._command_seq = count(1)
        # Phase 12+: `hospitals` is None/empty for every pre-existing caller
        # (the plain 3-hospital demo never passes it) — nothing below this
        # point runs for a transport whose record.patient is None.
        self._hospitals: dict[str, Hospital] = dict(hospitals or {})
        self._statuses: dict[str, HospitalStatus] = {
            hospital_id: initial_status(hospital) for hospital_id, hospital in self._hospitals.items()
        }
        self._ledger: dict[str, _LedgerEntry] = {}
        self._reservation_seq = 0
        # Called when a transport ends up with no destination at all, so the
        # ambulance can stop driving toward a hospital that already released
        # its bed — see _give_up_on_placement.
        self._on_no_destination = on_no_destination

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

    def arrived_at_of(self, transport_id: str) -> Optional[str]:
        """The hospital this transport reached, or None if it is still in
        transit. Mirrors TransportView.arrived_at without a replay."""
        record = self._transports.get(transport_id)
        if record is None or not record.arrived:
            return None
        return record.current_destination

    def is_known(self, transport_id: str) -> bool:
        """Whether this dispatcher instance has this transport in its live
        cache — false for a transport that only exists in event history
        from before a restart (E21): main.py uses this to tell a genuinely
        in-progress transition apart from an orphaned, INTERRUPTED one."""
        return transport_id in self._transports

    def candidates_tried_of(self, transport_id: str) -> list[str]:
        return list(self._require_transport(transport_id).candidates_tried)

    def update_position(self, transport_id: str, position: tuple[float, float]) -> None:
        """M2: eta_minutes() must use the ambulance's *current* position,
        not its origin — Simulation calls this on every tick for a
        capacity-aware transport so later accept()/rank() calls see it."""
        record = self._transports.get(transport_id)
        if record is not None:
            record.position = position

    def add_hospital(self, hospital: Hospital) -> None:
        """Register a hospital that did not exist when this dispatcher was
        built. _statuses used to be eagerly keyed from the startup roster,
        and scoring.rank indexes it unguarded — so without this an added
        hospital was a KeyError waiting to happen mid-dispatch."""
        self._hospitals[hospital.id] = hospital
        self._statuses.setdefault(hospital.id, initial_status(hospital))

    def restore_hospital_status(self, hospital_id: str, status: HospitalStatus) -> None:
        """Seed a hospital's status from replayed history at startup, with no
        rebalance side effects — those already happened and are in the log."""
        self._statuses[hospital_id] = status

    def remove_hospital(self, hospital_id: str) -> None:
        """Take a hospital out of the candidate pool. Its status and any
        reservations it still holds are left alone deliberately: transports
        already heading there are displaced by the caller through the normal
        capacity path (R18), not by yanking the ledger out from under them."""
        self._hospitals.pop(hospital_id, None)

    def hospital_status_of(self, hospital_id: str) -> HospitalStatus:
        return self._statuses[hospital_id]

    def known_patients(self) -> dict[str, Patient]:
        return self._known_patients()

    def ledger_view(self, hospital_id: str):
        return self._ledger_view(hospital_id)

    def hospitals(self) -> dict[str, Hospital]:
        return dict(self._hospitals)

    def patient_of(self, transport_id: str) -> Optional[Patient]:
        record = self._transports.get(transport_id)
        return record.patient if record is not None else None

    def position_of(self, transport_id: str) -> Optional[tuple[float, float]]:
        record = self._transports.get(transport_id)
        return record.position if record is not None else None

    def candidates_of(self, transport_id: str):
        """GET /transports/{id}/candidates: rank() over this transport's
        patient/position against every known hospital, on the dispatcher's
        own (possibly stale) view — the same pre-filter start()/redirect()
        use, exposed read-only."""
        record = self._require_transport(transport_id)
        if record.patient is None:
            return []
        return rank(
            record.patient, record.position, list(self._hospitals.values()), self._statuses,
            self._all_ledger_views(),
            self._config.speed_km_per_min, self._config.load_weight, self._config.saturation_limit,
        )

    def set_policy(self, policy: Policy) -> None:
        self._config = replace(self._config, policy=policy)

    @property
    def policy(self) -> Policy:
        """Read back what set_policy last installed. The mode decides whether
        a redirect may omit a target (only AUTO lets the dispatcher choose),
        so a caller that offers that option has to be able to ask."""
        return self._config.policy

    # -- R19: arrival --------------------------------------------------------
    # Not yet wired to anything in Phase 13 — ambulance.py doesn't call this
    # until Phase 15 gives it a position/hospital_id-aware Arrived path.
    # Implemented now because R19 is in this phase's own rule list.

    def on_arrived(self, transport_id: str, hospital_id: str) -> None:
        record = self._transports.get(transport_id)
        if record is None or record.patient is None:
            return
        if record.current_destination == hospital_id and record.current_bed_type is not None:
            self._occupy_bed(transport_id, hospital_id, record.current_bed_type, record.current_epoch)
            record.arrived = True
        # else: no live reservation here for this transport — log nothing
        # extra, per spec; checker.py's I3 (Phase 16) is what flags this.

    # -- R17: policy — reactive re-check on a status change for the current
    # destination of a STABLE auto-policy transport. Not yet wired to
    # anything in Phase 13 — nothing emits HospitalStatusChanged/BedsReported
    # until Phase 14 (F3/F4). Implemented now because R17 is in this phase's
    # own rule list; main.py/simulation.py call this once those events exist.

    def on_hospital_status_changed(self, hospital_id: str, new_status: HospitalStatus) -> None:
        self._statuses[hospital_id] = new_status
        if self._config.policy is not Policy.AUTO:
            return
        for transport_id, record in list(self._transports.items()):
            if (
                record.patient is not None
                and record.status == DispatcherStatus.STABLE
                and record.current_destination == hospital_id
                # R17 re-checks a transport's destination when capacity moves,
                # but only while it is still on the road. Once it has arrived
                # the patient is admitted and occupying a bed — redirecting
                # them is meaningless, and it made a decommission appear to
                # send already-treated patients back out.
                and not record.arrived
            ):
                self._recheck_current_destination(transport_id, record)

    def _recheck_current_destination(self, transport_id: str, record: _TransportState) -> None:
        decision = self._accept_for(record.patient, record.current_destination, record.position, transport_id)
        if decision.accepted:
            return
        old_destination = record.current_destination
        candidates = self._ranked_candidates(record.patient, record.position, transport_id)
        if not candidates:
            self._log(transport_id, record.current_epoch, EventType.NO_ACCEPTING_FACILITY, {"tried": []})
            return  # R17: keep the current destination
        self._log(
            transport_id, record.current_epoch, EventType.AUTO_REDIRECT,
            {"transport_id": transport_id, "from": old_destination, "to": candidates[0], "reason": decision.reason},
        )
        self._redirect_capacity_aware(transport_id, record, candidates[0])

    # -- R18: capacity rebalance ---------------------------------------------

    def on_beds_reported(self, hospital_id: str, bed_type: BedType, total: int) -> None:
        status = self._statuses[hospital_id]
        beds_total = dict(status.beds_total)
        beds_total[bed_type] = total
        self._statuses[hospital_id] = replace(status, beds_total=beds_total)

        from app.projection import free as _free  # local import: avoids a module-level cycle risk with projection

        view = self._ledger_view(hospital_id)
        deficit = -_free(view, self._statuses[hospital_id], bed_type)
        if deficit <= 0:
            return

        reserved_ids = view.reserved.get(bed_type, frozenset())
        reservation_order = self._reservation_order(hospital_id, bed_type)
        displaceable = [
            transport_id for transport_id in reserved_ids
            if transport_id in self._transports and self._transports[transport_id].patient is not None
        ]
        # Least critical (highest acuity number) first; most recently
        # reserved first among ties.
        displaceable.sort(
            key=lambda tid: (-self._transports[tid].patient.acuity, -reservation_order.get(tid, 0))
        )
        displaced = displaceable[:deficit]

        self._log(
            hospital_id, 0, EventType.CAPACITY_REBALANCE,
            {"hospital_id": hospital_id, "bed_type": bed_type.value, "displaced": displaced},
        )
        for transport_id in displaced:
            record = self._transports[transport_id]
            if record.pending_destination == hospital_id:
                self._displace_pending(transport_id, record, hospital_id)
            elif record.current_destination == hospital_id:
                self._displace_current(transport_id, record)

    def _reservation_order(self, hospital_id: str, bed_type: BedType) -> dict[str, int]:
        """transport_id -> when it reserved here, for R18's "most recent
        reservation first" tie-break among equally-critical candidates.
        Read straight off the live ledger cache — this used to replay the
        whole event log on every BedsReported."""
        entry = self._ledger.get(hospital_id)
        return dict(entry.reserved_order) if entry is not None else {}

    def _displace_pending(self, transport_id: str, record: _TransportState, hospital_id: str) -> None:
        # Same shape as a candidate declining (R15) — this reservation is
        # being taken back before commitment, not after.
        if record.pending_bed_type is not None:
            self._release_bed(transport_id, hospital_id, record.pending_bed_type, record.pending_epoch)
        record.pending_bed_type = None
        self._cancel_timer(record, "ready_timer")
        self._stop_driving_to_released_candidate(transport_id, record)
        if record.candidate_queue:
            self._try_next_candidate(transport_id, record)
            return
        self._log(transport_id, record.pending_epoch, EventType.NO_ACCEPTING_FACILITY, {"tried": list(record.candidates_tried), "gave_up": True})
        if record.current_destination is None:
            self._give_up_on_placement(transport_id, record)
        else:
            self._abort(transport_id, record)
            record.status = DispatcherStatus.NO_ACCEPTING_FACILITY

    def _displace_current(self, transport_id: str, record: _TransportState) -> None:
        # R18: "a transport that is the current destination keeps its bed if
        # it cannot be moved" — the ledger may over-report here; checker.py
        # (Phase 16) records that as a warning, never a handoff violation.
        if self._config.policy is Policy.AUTO:
            candidates = self._ranked_candidates(record.patient, record.position, transport_id)
            if candidates:
                self._redirect_capacity_aware(transport_id, record, candidates[0])
                return
        record.status = DispatcherStatus.NEEDS_REPLAN

    # -- Phase 12+: shared mechanics for the capacity-aware path -------------

    def _known_patients(self) -> dict[str, Patient]:
        return {
            transport_id: record.patient
            for transport_id, record in self._transports.items()
            if record.patient is not None
        }

    def _ledger_view(self, hospital_id: str) -> HospitalLedgerView:
        """Built from this dispatcher's own incrementally-maintained cache,
        not a replay. The dispatcher is the only writer of BedReserved/
        BedReleased/BedOccupied (R13/R14/R19), so it can keep the ledger in
        step as it emits them — replaying the whole log on every accept()
        call (once per candidate, per redirect) was the single hottest path
        in the system. project_ledger() over the log remains the
        independent, authoritative derivation; this is a live mirror of it."""
        entry = self._ledger.get(hospital_id)
        if entry is None:
            return empty_ledger_view(hospital_id)
        return HospitalLedgerView(
            hospital_id=hospital_id,
            reserved={bed_type: frozenset(ids) for bed_type, ids in entry.reserved.items()},
            occupied={bed_type: frozenset(ids) for bed_type, ids in entry.occupied.items()},
            ventilator_holders=frozenset(entry.ventilator_holders),
        )

    def _all_ledger_views(self, transport_id: Optional[str] = None) -> dict[str, HospitalLedgerView]:
        views = {hospital_id: self._ledger_view(hospital_id) for hospital_id in self._hospitals}
        if transport_id is None:
            return views
        return {hid: without_transport(view, transport_id) for hid, view in views.items()}

    def _accept_for(
        self,
        patient: Patient,
        hospital_id: str,
        position: tuple[float, float],
        transport_id: Optional[str] = None,
    ):
        hospital = self._hospitals[hospital_id]
        status = self._statuses[hospital_id]
        eta = eta_minutes(position, hospital, self._config.speed_km_per_min)
        view = self._ledger_view(hospital_id)
        if transport_id is not None:
            # Asking on behalf of a transport that may already be holding a
            # bed here (a re-rank, or a re-check of its own destination): its
            # own reservation must not count against it. See
            # projection.without_transport.
            view = without_transport(view, transport_id)
        return accept(patient, hospital, status, view, eta, self._config.saturation_limit)

    def _ranked_candidates(
        self, patient: Patient, position: tuple[float, float], transport_id: Optional[str] = None
    ) -> list[str]:
        """Every accepted hospital, best first (R17's choose()/rank()) —
        used to build a fresh candidate_queue for an auto-policy start/
        redirect with no explicit target."""
        ranked = rank(
            patient, position, list(self._hospitals.values()), self._statuses,
            self._all_ledger_views(transport_id),
            self._config.speed_km_per_min, self._config.load_weight, self._config.saturation_limit,
        )
        return [candidate.hospital_id for candidate in ranked if candidate.score is not None]

    def _reserve_bed(self, transport_id: str, hospital_id: str, bed_type: BedType, epoch: int) -> None:
        entry = self._ledger.setdefault(hospital_id, _LedgerEntry())
        entry.reserved.setdefault(bed_type, set()).add(transport_id)
        patient = self._transports[transport_id].patient
        if patient is not None and Need.VENTILATOR in patient.needs:
            entry.ventilator_holders.add(transport_id)
        self._reservation_seq += 1
        entry.reserved_order[transport_id] = self._reservation_seq
        self._log(
            transport_id, epoch, EventType.BED_RESERVED,
            {"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value},
        )

    def discharge(self, transport_id: str) -> Optional[tuple[str, BedType]]:
        """The patient was treated and has left: give the bed back.

        Deliberately touches capacity bookkeeping only — the transport stays
        where it is and the facility stays ACTIVE, so the handoff invariant is
        untouched. Discharging is not a handoff, and making it one would mean
        a cured patient briefly had no hospital, which is exactly the state
        I1 exists to forbid.

        Returns the (hospital, bed type) freed, or None if this transport
        wasn't holding a bed anywhere.
        """
        record = self._transports.get(transport_id)
        if record is None:
            return None
        # Only a bed the patient is actually *in*. A merely reserved bed
        # belongs to a handshake still in flight, and taking it back is a
        # redirect or an abort — not a discharge; doing it here would strand
        # the transport mid-protocol.
        for hospital_id, entry in self._ledger.items():
            for bed_type, ids in entry.occupied.items():
                if transport_id in ids:
                    self._release_bed(
                        transport_id, hospital_id, bed_type, record.current_epoch, reason="discharged"
                    )
                    if record.current_destination == hospital_id:
                        record.current_bed_type = None
                    return hospital_id, bed_type
        return None

    def _release_bed(
        self, transport_id: str, hospital_id: str, bed_type: BedType, epoch: int, reason: Optional[str] = None
    ) -> None:
        entry = self._ledger.setdefault(hospital_id, _LedgerEntry())
        for ids in entry.reserved.values():
            ids.discard(transport_id)
        # Occupancy too. Every caller today releases a bed that was only ever
        # reserved, so this is a no-op for them — but it was the asymmetry
        # that made a released occupied bed stay held, and the mirror must
        # mean exactly what BedReleased means on replay.
        for ids in entry.occupied.values():
            ids.discard(transport_id)
        entry.ventilator_holders.discard(transport_id)
        entry.reserved_order.pop(transport_id, None)
        payload = {"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value}
        if reason is not None:
            payload["reason"] = reason
        self._log(transport_id, epoch, EventType.BED_RELEASED, payload)

    def _occupy_bed(self, transport_id: str, hospital_id: str, bed_type: BedType, epoch: int) -> None:
        entry = self._ledger.setdefault(hospital_id, _LedgerEntry())
        for ids in entry.reserved.values():
            ids.discard(transport_id)
        entry.reserved_order.pop(transport_id, None)
        entry.occupied.setdefault(bed_type, set()).add(transport_id)
        self._log(
            transport_id, epoch, EventType.BED_OCCUPIED,
            {"hospital_id": hospital_id, "transport_id": transport_id, "bed_type": bed_type.value},
        )

    # -- R10: start ------------------------------------------------------

    def start(
        self,
        transport_id: str,
        destination: Optional[str] = None,
        patient: Optional[Patient] = None,
        position: Optional[tuple[float, float]] = None,
    ) -> None:
        if transport_id in self._transports:
            raise ValueError(f"Expected transport {transport_id!r} to be new, it was already started")

        if patient is None:
            if destination is None:
                raise ValueError("destination is required when patient is not given")
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
            return

        # -- capacity-aware start (R13, R15, R20) -----------------------
        if destination is not None:
            decision = self._accept_for(patient, destination, position)
            if not decision.accepted:
                raise NotEligible(decision.reason)
            candidates = [destination]
        else:
            candidates = self._ranked_candidates(patient, position)

        record = _TransportState(
            pending_epoch=1, highest_epoch_seen=1, status=DispatcherStatus.STARTING,
            patient=patient, position=position,
        )
        self._transports[transport_id] = record

        if not candidates:
            self._log(transport_id, 1, EventType.NO_ACCEPTING_FACILITY, {"tried": [], "gave_up": True})
            record.status = DispatcherStatus.NO_ACCEPTING_FACILITY
            return

        record.candidate_queue = candidates
        self._log(transport_id, 1, EventType.TRANSPORT_STARTED, {"destination": candidates[0]})
        self._try_next_candidate(transport_id, record)
        # Same reasoning as the plain path above: no READY-timeout/retry
        # here — a declined candidate is handled by _on_declined instead
        # (an explicit ack, not a timeout), and there is still no "current"
        # to fall back an abort to during this initial placement.

    def _try_next_candidate(self, transport_id: str, record: _TransportState) -> None:
        """Pops the next hospital off record.candidate_queue, reserves a bed
        there (R13) and sends it PREPARE — used both for the first candidate
        of a fresh start/redirect and for R15's decline-triggered retry
        under the same epoch.

        The queue is populated with hospitals that passed accept() when it
        was built, but a candidate can sit in it while other transports take
        the last bed or a hospital's status drifts — so accept() is re-run
        per candidate and a no-longer-eligible one is skipped rather than
        reserved. (Reserving on a failed decision used to crash here:
        Decision.bed_type is None when it isn't accepted.)"""
        # Whatever candidate we were on is being abandoned. It has already
        # been sent a PREPARE, so it is at least ARMED — and if its READY came
        # back during a start, it has an ACTIVATE_AT in flight and will go
        # ACTIVE regardless of us moving on. Left un-withdrawn it becomes an
        # orphaned facility: active for a transport whose dispatcher now
        # believes it is somewhere else entirely, which is a direct I1
        # (ZERO_ACTIVE) failure once the new candidate's cutover applies.
        self._stand_down_abandoned_candidate(transport_id, record)

        while record.candidate_queue:
            target = record.candidate_queue.pop(0)
            record.candidates_tried.append(target)
            decision = self._accept_for(record.patient, target, record.position, transport_id)
            if decision.accepted:
                break
            self._log(
                transport_id, record.pending_epoch, EventType.CANDIDATE_DECLINED,
                {"hospital_id": target, "transport_id": transport_id, "reason": decision.reason},
            )
        else:
            # Every remaining candidate went stale before we reached it.
            self._log(
                transport_id, record.pending_epoch, EventType.NO_ACCEPTING_FACILITY,
                {"tried": list(record.candidates_tried), "gave_up": True},
            )
            if record.current_destination is None:
                self._give_up_on_placement(transport_id, record)
            else:
                self._abort(transport_id, record)
                record.status = DispatcherStatus.NO_ACCEPTING_FACILITY
            return

        record.pending_destination = target
        record.pending_bed_type = decision.bed_type
        record.ready_received = False
        record.notice_applied = False
        record.ready_retried = False
        self._reserve_bed(transport_id, target, decision.bed_type, record.pending_epoch)
        eta = eta_minutes(record.position, self._hospitals[target], self._config.speed_km_per_min)
        record.prepare_command_id = self._send(
            transport_id, record, target, record.pending_epoch, ActionType.PREPARE,
            eta_minutes=eta, patient=record.patient,
        )
        record.notice_command_id = self._send(transport_id, record, target, record.pending_epoch, ActionType.REDIRECT_NOTICE)

    # -- R1, R6, R8, R9: redirect -----------------------------------------

    def redirect(self, transport_id: str, target: Optional[str] = None) -> None:
        record = self._require_transport(transport_id)

        if record.patient is None:
            if target is None:
                raise ValueError("target is required for a plain (non-capacity-aware) redirect")
            self._redirect_plain(transport_id, record, target)
            return
        self._redirect_capacity_aware(transport_id, record, target)

    def _redirect_plain(self, transport_id: str, record: _TransportState, target: str) -> None:
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

    def _redirect_capacity_aware(self, transport_id: str, record: _TransportState, target: Optional[str]) -> None:
        # Redirecting a transport that has already arrived is allowed: an
        # operator moving a patient on to another hospital is a real thing to
        # want, and the ledger now handles it correctly (_release_bed gives
        # back an occupancy, not just a reservation, so the hospital they
        # leave stops counting the bed). Only the *automatic* paths refuse to
        # touch an arrived transport — a capacity wobble must not move a
        # patient who is already in a bed; see _recheck_current_destination.
        """R13/R15/R17/R20. Mirrors _redirect_plain's R1/R6/R8/R9 branching
        exactly (a capacity-aware transport is still bound by the same
        epoch/cancellable rules), but the "new pending target" is resolved
        via accept()/rank() first, and — for a fresh pending transition —
        the remaining ranked candidates are kept as a fallback queue for
        R15 to walk through if this first choice later declines. A redirect
        while a transition is already pending only ever targets the single
        hospital the caller/ranker names right now (see the phase report:
        that path doesn't thread a full fallback queue through the
        cancel-and-restart, a disclosed scope simplification)."""
        if target is not None:
            decision = self._accept_for(record.patient, target, record.position, transport_id)
            if not decision.accepted:
                raise NotEligible(decision.reason)
            candidates = [target]
        else:
            if self._config.policy is not Policy.AUTO:
                raise TargetRequired("target is required for a manual-policy redirect")
            candidates = self._ranked_candidates(record.patient, record.position, transport_id)
            if not candidates:
                self._log(transport_id, record.current_epoch, EventType.NO_ACCEPTING_FACILITY, {"tried": []})
                return  # R17: no candidate at all -- keep the current destination

        new_target = candidates[0]

        if new_target == record.pending_destination:
            return  # R9

        if record.current_destination is None:
            self._redirect_before_first_activation_capacity_aware(transport_id, record, candidates)
            return

        if record.pending_destination is None:
            if new_target == record.current_destination:
                return  # R8, no pending transition: no-op
            epoch = self._allocate_epoch(record)
            self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": new_target})
            record.pending_epoch = epoch
            record.candidate_queue = list(candidates)
            self._try_next_candidate(transport_id, record)
            return

        if new_target == record.current_destination:
            if self._cancellable(record):
                epoch = self._cancel_pending(transport_id, record)
                self._send(transport_id, record, record.current_destination, epoch, ActionType.REDIRECT_NOTICE)
            else:
                self._queue_redirect(transport_id, record, new_target)
            return

        if self._cancellable(record):
            epoch = self._cancel_pending(transport_id, record)
            self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": new_target})
            record.pending_epoch = epoch
            record.candidate_queue = list(candidates)
            self._try_next_candidate(transport_id, record)
        else:
            self._queue_redirect(transport_id, record, new_target)

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
        elif ack.command_id == record.prepare_command_id and ack.ack_type is AckType.DECLINED:
            self._on_declined(transport_id, record, ack)
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

    # -- R15, R16: a facility refusing a PREPARE (capacity-aware only) -----

    def _on_declined(self, transport_id: str, record: _TransportState, ack: Ack) -> None:
        if record.activate_command_id is not None:
            # R16: this candidate already proved its activation is
            # scheduled (RECEIVED for ACTIVATE_AT) — a DECLINED for it now
            # is stale/forged, not a real answer to the PREPARE that's long
            # since been superseded by that RECEIVED.
            self._log(
                transport_id, ack.epoch, EventType.LATE_DECLINE_IGNORED,
                {"command_id": ack.command_id, "hospital_id": ack.facility_id},
            )
            return

        hospital_id = ack.facility_id
        self._log(
            transport_id, ack.epoch, EventType.CANDIDATE_DECLINED,
            {"hospital_id": hospital_id, "transport_id": transport_id, "reason": ack.reason},
        )
        if record.pending_bed_type is not None:
            self._release_bed(transport_id, hospital_id, record.pending_bed_type, ack.epoch)
        record.pending_bed_type = None
        self._cancel_timer(record, "ready_timer")
        # The ambulance was pointed at this hospital by a REDIRECT_NOTICE
        # sent optimistically, before the hospital had accepted. Its bed is
        # now gone, so it must stop driving there immediately — the next
        # candidate's notice (or the abort's) re-points it a moment later,
        # and until then it correctly has nowhere to be. Without this it can
        # still "arrive" at the hospital that just refused it (I3 / E33).
        self._stop_driving_to_released_candidate(transport_id, record)

        if record.candidate_queue:
            self._try_next_candidate(transport_id, record)
            return

        # R15: candidates exhausted.
        self._log(transport_id, record.pending_epoch, EventType.NO_ACCEPTING_FACILITY, {"tried": list(record.candidates_tried), "gave_up": True})
        if record.current_destination is None:
            self._give_up_on_placement(transport_id, record)
        else:
            self._abort(transport_id, record)
            record.status = DispatcherStatus.NO_ACCEPTING_FACILITY

    def _stand_down_abandoned_candidate(self, transport_id: str, record: _TransportState) -> None:
        """Withdraw the candidate we are walking away from, and retire the
        activation bookkeeping that belonged to it.

        WITHDRAW is safe in every state the candidate can be in: IDLE (it
        declined) ignores it, ARMED stands down, ACTIVE stands down. Clearing
        activate_command_id matters just as much — otherwise that candidate's
        late ACTIVATE_AT ack is still routed as though this transport had a
        live transition to it."""
        previous = record.pending_destination
        if previous is None:
            return
        if record.pending_epoch is not None:
            self._send(transport_id, record, previous, record.pending_epoch, ActionType.WITHDRAW)
        record.pending_destination = None
        record.activate_command_id = None
        record.cutover_at = None
        record.withdraw_sent = False
        self._cancel_timer(record, "receipt_deadline_timer")

    def _stop_driving_to_released_candidate(self, transport_id: str, record: _TransportState) -> None:
        """Park the ambulance the instant a candidate it was driving toward
        loses its reservation. Whatever comes next (the next candidate, an
        abort's REDIRECT_NOTICE back to current, or nothing at all) issues
        its own notice, which restarts it."""
        if self._on_no_destination is not None:
            # Everything issued up to and including this seq belongs to the
            # attempt being abandoned, so the ambulance must ignore any of it
            # still in flight.
            self._on_no_destination(transport_id, record.notice_seq)

    def _give_up_on_placement(self, transport_id: str, record: _TransportState) -> None:
        """Every candidate declined and there is no "current" for R7's abort
        to fall back to (same reasoning as _redirect_before_first_activation).

        The ambulance has to be told: it was handed a REDIRECT_NOTICE for
        the first candidate before that candidate declined, so left alone it
        keeps driving to a hospital that has since released its bed and
        eventually logs Arrived there — checker.py's I3
        (ARRIVED_WITHOUT_RESERVATION), which E33 says the dispatcher must
        never produce."""
        self._cancel_timers(record)
        # The abandoned candidate may already be ARMED — or ACTIVE, if its
        # ACTIVATE_AT landed before capacity moved and displaced this
        # transport. Clearing our own bookkeeping without telling it leaves it
        # holding a transport nothing owns any more: an orphaned facility that
        # no later redirect will ever withdraw, which is a live I1 hazard.
        # WITHDRAW is safe whatever state it is in — a candidate that merely
        # declined fences it and ignores it.
        abandoned = record.pending_destination
        if abandoned is not None and record.pending_epoch is not None:
            self._send(transport_id, record, abandoned, record.pending_epoch, ActionType.WITHDRAW)
        # Retire the whole transition, not just pending_destination: a late
        # ack for its ACTIVATE_AT would otherwise still match
        # activate_command_id and be routed as if a redirect were in flight.
        self._clear_pending(record)
        record.status = DispatcherStatus.NO_ACCEPTING_FACILITY
        if self._on_no_destination is not None:
            # Everything issued up to and including this seq belongs to the
            # attempt being abandoned, so the ambulance must ignore any of it
            # still in flight.
            self._on_no_destination(transport_id, record.notice_seq)

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
        # _apply_cutover flips current_destination on this timer alone — it
        # does not wait for any ack (that's the whole point: it must stay
        # correct even if the pending facility's own APPLIED ack is never
        # seen, e.g. E7's out-of-order case). The pending facility applies
        # its OWN ACTIVATE_AT (effective_at=cutover_at, above) on a
        # *separate* timer it schedules independently once the command
        # arrives. Under FakeClock both are ordered deterministically
        # (E23's insertion-order tie-break, since this timer is inserted
        # first) — a real clock gives no such guarantee between two
        # independently-computed timers aimed at the same nominal instant,
        # and Event.ts_ms's own integer-millisecond rounding means their
        # *computed* targets can differ by a millisecond or two even when
        # both are "correct". _DISPATCHER_SETTLE_MS (much larger than that)
        # is pure margin ensuring this bookkeeping update always lands
        # safely after the pending facility has actually activated — see
        # _on_activate_received for the matching margin on the old
        # facility's withdrawal, which must land safely after *this*.
        record.cutover_timer = self._clock.schedule(
            cutover_at - t0 + _DISPATCHER_SETTLE_MS, lambda: self._apply_cutover(transport_id, record)
        )
        receipt_deadline_ms = t0 + 2 * self._config.d_max_ms + self._config.guard_ms
        record.receipt_deadline_timer = self._clock.schedule(
            receipt_deadline_ms - t0, lambda: self._on_receipt_deadline(transport_id, record)
        )
        record.status = DispatcherStatus.CUTOVER_SCHEDULED

    # -- R5: on_received -> send withdraw -----------------------------------

    def _on_activate_received(self, transport_id: str, record: _TransportState) -> None:
        if record.withdraw_sent:
            return  # ack dedup already guards this, but never send it twice
        if record.cutover_at is None or record.pending_epoch is None:
            # Defence in depth only. The ack that reaches here is matched on
            # activate_command_id, and every path that abandons a transition
            # now clears that id (_clear_pending, via give-up and
            # _stand_down_abandoned_candidate) — so a concluded transition's
            # ack no longer routes here at all.
            #
            # Deliberately NOT guarded on current_destination: when a cutover
            # timer fires before this ack arrives (which happens whenever the
            # delay bound is exceeded, E25), the transition has *completed*
            # rather than been abandoned, and the withdraw is still owed.
            # Guarding it away there stranded the old facility active.
            self._log(
                transport_id, record.current_epoch, EventType.STALE_IGNORED,
                {"command_id": record.activate_command_id, "reason": "transition_already_concluded"},
            )
            return
        self._cancel_timer(record, "receipt_deadline_timer")
        # Effective at cutover_at + 2*settle, not cutover_at: the spec's own
        # safety argument allows the old facility to withdraw "at the same
        # instant or later" — never earlier. One settle margin (matching
        # _schedule_cutover's cutover_timer) puts this safely after the
        # dispatcher's own bookkeeping update, which is itself one margin
        # after the new facility's real activation — a strict, non-racing
        # order: new facility active, then current_destination flips, then
        # (only then) the old facility actually stands down.
        command_id = self._send(
            transport_id, record, record.current_destination, record.pending_epoch,
            ActionType.WITHDRAW_AT, effective_at_ms=record.cutover_at + 2 * _DISPATCHER_SETTLE_MS,
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
        old_current, old_current_bed_type = record.current_destination, record.current_bed_type
        new_bed_type = record.pending_bed_type
        record.current_destination = record.pending_destination
        record.current_epoch = record.pending_epoch
        self._clear_pending(record)
        record.current_bed_type = new_bed_type
        record.status = DispatcherStatus.STABLE
        self._log(transport_id, record.current_epoch, EventType.CUTOVER_APPLIED, {"current_destination": record.current_destination})
        # R14: the old facility's bed is released here — the new one's
        # reservation stays held until Arrived (see on_arrived/R19).
        if old_current is not None and old_current_bed_type is not None:
            self._release_bed(transport_id, old_current, old_current_bed_type, record.current_epoch)
        self._run_queued_redirect(transport_id, record)

    def _on_activate_applied(self, transport_id: str, record: _TransportState) -> None:
        # start()'s finalize step (R10): there is no cutover_timer during
        # STARTING, so this ack — not a timer — is what flips current.
        # old_current is always None here (this is a transport's very first
        # placement) — the release below only ever fires for a later
        # _apply_cutover, never this one; written the same way for symmetry.
        old_current, old_current_bed_type = record.current_destination, record.current_bed_type
        new_bed_type = record.pending_bed_type
        record.current_destination = record.pending_destination
        record.current_epoch = record.pending_epoch
        self._clear_pending(record)  # also retires notice/prepare tracking — see its docstring
        record.current_bed_type = new_bed_type
        record.status = DispatcherStatus.STABLE
        self._log(transport_id, record.current_epoch, EventType.CUTOVER_APPLIED, {"current_destination": record.current_destination})
        if old_current is not None and old_current_bed_type is not None:
            self._release_bed(transport_id, old_current, old_current_bed_type, record.current_epoch)

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
        pending_destination, pending_bed_type = record.pending_destination, record.pending_bed_type
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REMAIN)
        self._send(transport_id, record, record.pending_destination, epoch, ActionType.WITHDRAW)
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REDIRECT_NOTICE)
        if pending_destination is not None and pending_bed_type is not None:
            self._release_bed(transport_id, pending_destination, pending_bed_type, epoch)
        record.current_epoch = epoch
        self._clear_pending(record)
        if record.current_destination is None:
            # Aborting an initial placement (a READY timeout before this
            # transport ever activated anywhere): the REDIRECT_NOTICE above
            # was a no-op because there is no facility to go back to, so the
            # ambulance is still pointed at the candidate that just failed.
            # Park it, exactly as an exhausted candidate list would.
            self._stop_driving_to_released_candidate(transport_id, record)
            record.status = DispatcherStatus.NO_ACCEPTING_FACILITY
            self._log(transport_id, epoch, EventType.REDIRECT_ABORTED, {})
            self._run_queued_redirect(transport_id, record)
            return
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
        old_pending_bed_type = record.pending_bed_type
        self._cancel_timers(record)
        epoch = self._allocate_epoch(record)
        self._log(transport_id, epoch, EventType.CUTOVER_CANCELLED, {"old_pending_epoch": old_pending_epoch})
        self._send(transport_id, record, record.current_destination, epoch, ActionType.REMAIN)
        self._send(transport_id, record, old_pending, epoch, ActionType.WITHDRAW)
        if old_pending_bed_type is not None:
            self._release_bed(transport_id, old_pending, old_pending_bed_type, epoch)
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
        if record.patient is None:
            self.redirect(transport_id, target)
            return
        try:
            self.redirect(transport_id, target)
        except NotEligible as exc:
            # By the time a queued redirect finally runs, capacity may have
            # moved on — fail safe the same way an exhausted candidate list
            # does: keep the current destination, don't crash the run.
            self._log(
                transport_id, record.current_epoch, EventType.NO_ACCEPTING_FACILITY,
                {"tried": [target], "reason": exc.reason},
            )

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
        # still_starting distinguishes this from _begin_redirect's identical
        # event type — projection.py replays purely from events and can't
        # otherwise tell "redirect while starting" (stay STARTING) apart
        # from a normal redirect (go PREPARING); see its REDIRECT_REQUESTED
        # handler.
        self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": target, "still_starting": True})
        record.prepare_command_id = self._send(transport_id, record, target, epoch, ActionType.PREPARE)
        record.notice_command_id = self._send(transport_id, record, target, epoch, ActionType.REDIRECT_NOTICE)

    def _redirect_before_first_activation_capacity_aware(
        self, transport_id: str, record: _TransportState, candidates: list[str]
    ) -> None:
        """Capacity-aware counterpart to _redirect_before_first_activation
        above — same reasoning (no "current" exists yet to fall back to),
        plus releasing the abandoned candidate's reservation (R14) and
        handing the rest of `candidates` to _try_next_candidate as this
        redirect's own fallback queue (R15)."""
        old_target = record.pending_destination
        old_bed_type = record.pending_bed_type
        epoch = self._allocate_epoch(record)
        self._send(transport_id, record, old_target, epoch, ActionType.WITHDRAW)
        if old_target is not None and old_bed_type is not None:
            self._release_bed(transport_id, old_target, old_bed_type, epoch)
        record.pending_destination = None
        record.pending_bed_type = None
        record.pending_epoch = epoch
        record.ready_received = False
        record.notice_applied = False
        record.status = DispatcherStatus.STARTING
        self._log(transport_id, epoch, EventType.REDIRECT_REQUESTED, {"target": candidates[0], "still_starting": True})
        record.candidate_queue = list(candidates)
        self._try_next_candidate(transport_id, record)

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
        # Phase 12+: a concluded transition (applied, cancelled or aborted)
        # leaves no pending reservation or fallback queue behind for the
        # next one. candidates_tried is deliberately NOT reset here — it's
        # this transport's running history (R15/NoAcceptingFacility's
        # "tried"), not per-attempt scratch state.
        record.pending_bed_type = None
        record.candidate_queue = []

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
        target: Optional[str],
        epoch: int,
        action: ActionType,
        effective_at_ms: Optional[int] = None,
        eta_minutes: Optional[float] = None,
        patient: Optional[Patient] = None,
    ) -> Optional[str]:
        if target is None:
            # There is no facility to address. This is reachable and correct
            # during an initial placement that never activated anywhere:
            # WITHDRAW from a candidate that was never prepared, or REMAIN at
            # a "current" that does not exist yet, are both no-ops by
            # definition. Command.target_facility requires a real id, so
            # without this the redirect blew up with a Pydantic
            # ValidationError instead. Callers that need the ambulance told
            # something must handle that themselves — see _abort.
            return None
        command_id = f"cmd-{next(self._command_seq)}"
        notice_seq: Optional[int] = None
        if action is ActionType.REDIRECT_NOTICE:
            record.notice_seq += 1
            notice_seq = record.notice_seq
        command = Command(
            command_id=command_id,
            transport_id=transport_id,
            target_facility=target,
            epoch=epoch,
            action=action,
            effective_at_ms=effective_at_ms,
            sent_at_ms=self._clock.now_ms(),
            eta_minutes=eta_minutes,
            patient=patient,
            notice_seq=notice_seq,
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
