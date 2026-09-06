"""The append-only event log. Every other module's state is a projection of
what EventStore holds — nothing else is a source of truth (see projection.py
and checker.py, both Phase 5).
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class EventType(Enum):
    TRANSPORT_STARTED = "TransportStarted"
    REDIRECT_REQUESTED = "RedirectRequested"
    REDIRECT_QUEUED = "RedirectQueued"
    COMMAND_SENT = "CommandSent"
    MESSAGE_DELIVERED = "MessageDelivered"
    ACK_RECEIVED = "AckReceived"
    READY_CONFIRMED = "ReadyConfirmed"
    CUTOVER_SCHEDULED = "CutoverScheduled"
    WITHDRAW_SENT = "WithdrawSent"
    CUTOVER_CANCELLED = "CutoverCancelled"
    CUTOVER_APPLIED = "CutoverApplied"
    STALE_IGNORED = "StaleIgnored"
    DUPLICATE_IGNORED = "DuplicateIgnored"
    TARGET_MISMATCH = "TargetMismatch"
    ILLEGAL_TRANSITION = "IllegalTransition"
    REDIRECT_ABORTED = "RedirectAborted"
    BOUND_EXCEEDED = "BoundExceeded"
    FACILITY_STATE_CHANGED = "FacilityStateChanged"
    ARRIVED = "Arrived"

    # -- Phase 12+ (multi-hospital capacity extension) --------------------
    # Only the three the bed ledger itself replays are added here; the rest
    # of the extension's new event types (BedsReported, HospitalStatusChanged,
    # CandidateDeclined, ...) belong to the phases that actually emit them
    # (13-15/18), not this pure, read-only ledger.
    BED_RESERVED = "BedReserved"
    BED_RELEASED = "BedReleased"
    BED_OCCUPIED = "BedOccupied"
    # Phase 13 (dispatcher: reservation, candidate iteration, policy):
    CANDIDATE_DECLINED = "CandidateDeclined"
    LATE_DECLINE_IGNORED = "LateDeclineIgnored"
    AUTO_REDIRECT = "AutoRedirect"
    NO_ACCEPTING_FACILITY = "NoAcceptingFacility"
    # Phase 14 (facility: acceptance on PREPARE, live status, drift):
    HOSPITAL_STATUS_CHANGED = "HospitalStatusChanged"
    BEDS_REPORTED = "BedsReported"
    # Phase 15 (R18, capacity rebalance):
    CAPACITY_REBALANCE = "CapacityRebalance"
    # Phase 21: the hospital roster itself. Until these existed, *which*
    # hospitals were in the network was static config read at import time —
    # the one part of the system state that could not be reconstructed by
    # replaying the log. These close that gap: the roster is now derived from
    # events like everything else (see projection.project_hospitals).
    #
    # Like the other hospital-scoped events, these carry the hospital id in
    # `transport_id` — the column is NOT NULL and predates them. See
    # project_hospitals for why that stays a deliberate convention.
    HOSPITAL_REGISTERED = "HospitalRegistered"
    HOSPITAL_UPDATED = "HospitalUpdated"
    HOSPITAL_DECOMMISSIONED = "HospitalDecommissioned"


@dataclass(frozen=True)
class Event:
    """One immutable fact in the log. `seq` is assigned by EventStore.append
    and must be None on every event handed to it — a caller passing an
    already-persisted event back in is almost certainly a bug."""

    transport_id: str
    epoch: int
    ts_ms: int
    type: EventType
    facility_id: Optional[str]
    payload: dict[str, Any]
    seq: Optional[int] = None


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    transport_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    type TEXT NOT NULL,
    facility_id TEXT,
    payload TEXT NOT NULL
)
"""
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_events_transport_seq ON events (transport_id, seq)"
)
_SELECT_COLUMNS = "seq, transport_id, epoch, ts_ms, type, facility_id, payload"


class EventStore:
    """Append-only log backed by SQLite. `path=':memory:'` (the default) is
    for tests; a real file path is what makes replay-on-restart (E21)
    possible — the table is recreated idempotently on open either way."""

    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False: Phase 8's fuzz endpoint runs the
        # simulation on a background thread while the store may still be
        # queried from the request-handling thread.
        #
        # That flag only *permits* cross-thread use — it does not serialize
        # it, and concurrent use of one connection raises
        # "sqlite3.InterfaceError: bad parameter or other API misuse". Under
        # a real server there are genuinely three writers/readers in play:
        # sync route handlers (on FastAPI's threadpool), bus deliveries and
        # ambulance ticks (on the event loop via RealClock), and the fuzz
        # background task. Hence the lock below, held only around actual
        # connection calls.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_INDEX_SQL)
        self._conn.commit()
        self._subscribers: list[Callable[[Event], None]] = []
        # Bumped on every append/clear. Readers that derive something
        # expensive from the whole log (the global invariant check, a
        # facility's ledger view) memoize against this instead of
        # recomputing per call — a plain integer compare replaces an
        # O(events) replay when nothing has changed since last time.
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def append(self, event: Event) -> Event:
        if event.seq is not None:
            raise ValueError(
                f"Expected an unpersisted event (seq=None), received seq={event.seq}"
            )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (transport_id, epoch, ts_ms, type, facility_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.transport_id,
                    event.epoch,
                    event.ts_ms,
                    event.type.value,
                    event.facility_id,
                    json.dumps(event.payload),
                ),
            )
            self._conn.commit()
            self._revision += 1
            persisted = dataclasses.replace(event, seq=cursor.lastrowid)
        # Subscribers run outside the lock: they are free to read the store
        # back (the hub replays for its projection), which would otherwise
        # re-enter it on the same call stack.
        for subscriber in self._subscribers:
            subscriber(persisted)
        return persisted

    def replay(self, transport_id: Optional[str] = None) -> list[Event]:
        with self._lock:
            if transport_id is None:
                rows = self._conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM events ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM events WHERE transport_id = ? ORDER BY seq",
                    (transport_id,),
                ).fetchall()
        return [_row_to_event(row) for row in rows]

    @contextmanager
    def transaction(self):
        """Hand the underlying connection out under this store's own lock.

        The read model (readmodel.py) lives in the same SQLite file so there is
        one database to deploy and one thing to back up — and it must not open
        a second connection to it, because two writers to one SQLite file
        deadlock under exactly the concurrency this app has (route handlers on
        the threadpool, bus deliveries on the event loop). Sharing the lock is
        what makes that safe.
        """
        with self._lock:
            yield self._conn

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def clear(self) -> None:
        """Wipes the log — used by POST /demo/reset. Subscribers are left
        registered; seq is not restarted, since nothing depends on it
        beginning at 1 after a reset, only on staying monotonic."""
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.commit()
            self._revision += 1


def _row_to_event(row: tuple) -> Event:
    seq, transport_id, epoch, ts_ms, type_value, facility_id, payload = row
    return Event(
        seq=seq,
        transport_id=transport_id,
        epoch=epoch,
        ts_ms=ts_ms,
        type=EventType(type_value),
        facility_id=facility_id,
        payload=json.loads(payload),
    )
