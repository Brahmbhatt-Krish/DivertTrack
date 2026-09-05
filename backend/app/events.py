"""The append-only event log. Every other module's state is a projection of
what EventStore holds — nothing else is a source of truth (see projection.py
and checker.py, both Phase 5).
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
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
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_INDEX_SQL)
        self._conn.commit()
        self._subscribers: list[Callable[[Event], None]] = []

    def append(self, event: Event) -> Event:
        if event.seq is not None:
            raise ValueError(
                f"Expected an unpersisted event (seq=None), received seq={event.seq}"
            )
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
        persisted = dataclasses.replace(event, seq=cursor.lastrowid)
        for subscriber in self._subscribers:
            subscriber(persisted)
        return persisted

    def replay(self, transport_id: Optional[str] = None) -> list[Event]:
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

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)


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
