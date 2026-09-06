"""The hospital read model: relational tables derived from the event log.

The log stays the single source of truth. Nothing here is ever written to
directly — every row is produced by projecting an event, and deleting every
table and replaying the log must reproduce them exactly (there is a test for
that, and it is the only thing that proves these tables are genuinely derived
rather than quietly drifted).

Why it exists: `project_hospitals()` and `project_ledger()` fold the *entire*
log on every call, which is O(events) and grows without bound for the life of
a deployment. This turns "what does the network look like right now" into an
indexed query, while the pure replay functions remain the authoritative
derivation the checker uses.

Tables live in the same SQLite file as the events, through the store's own
connection and lock — see EventStore.transaction for why a second connection
would be wrong.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

from app.events import Event, EventStore, EventType
from app.models import Specialist

SCHEMA = """
-- Every hospital ever registered. Decommissioned ones are kept, not deleted:
-- invariant I4 re-runs accept() over historical events, and a hospital that
-- has since closed must still resolve or every valid decision it ever made
-- looks like a violation.
CREATE TABLE IF NOT EXISTS hospitals (
    id                 TEXT    PRIMARY KEY,
    name               TEXT    NOT NULL,
    location_x         REAL    NOT NULL,
    location_y         REAL    NOT NULL,
    ventilators_total  INTEGER NOT NULL DEFAULT 0,
    registered_seq     INTEGER NOT NULL,
    decommissioned_seq INTEGER,
    updated_seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hospitals_active ON hospitals (decommissioned_seq);

-- A *missing* row means the hospital has no such ward at all; a row with
-- total = 0 means it has the ward and no capacity. Both fail the bed check,
-- but they are different facts and the model keeps them apart (Northshore
-- genuinely has no ICU).
CREATE TABLE IF NOT EXISTS hospital_beds (
    hospital_id TEXT    NOT NULL,
    bed_type    TEXT    NOT NULL,
    total       INTEGER NOT NULL,
    PRIMARY KEY (hospital_id, bed_type)
);

CREATE TABLE IF NOT EXISTS hospital_capabilities (
    hospital_id TEXT NOT NULL,
    capability  TEXT NOT NULL,
    PRIMARY KEY (hospital_id, capability)
);
CREATE INDEX IF NOT EXISTS idx_caps_by_capability ON hospital_capabilities (capability);

CREATE TABLE IF NOT EXISTS hospital_max_eta (
    hospital_id TEXT    NOT NULL,
    condition   TEXT    NOT NULL,
    minutes     INTEGER NOT NULL,
    PRIMARY KEY (hospital_id, condition)
);

-- Kept apart from the static record above because they have completely
-- different write rates: identity changes almost never, status changes
-- constantly.
CREATE TABLE IF NOT EXISTS hospital_status (
    hospital_id   TEXT PRIMARY KEY,
    diversion     TEXT NOT NULL DEFAULT 'OPEN',
    ed_saturation REAL NOT NULL DEFAULT 0.0,
    updated_seq   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hospital_specialists (
    hospital_id TEXT NOT NULL,
    specialist  TEXT NOT NULL,
    PRIMARY KEY (hospital_id, specialist)
);

-- Only meaningful while diversion = 'PARTIAL'.
CREATE TABLE IF NOT EXISTS hospital_diverted_categories (
    hospital_id TEXT NOT NULL,
    condition   TEXT NOT NULL,
    PRIMARY KEY (hospital_id, condition)
);

-- The bed ledger: who holds what, right now.
CREATE TABLE IF NOT EXISTS bed_holdings (
    hospital_id  TEXT    NOT NULL,
    transport_id TEXT    NOT NULL,
    bed_type     TEXT    NOT NULL,
    state        TEXT    NOT NULL,
    since_seq    INTEGER NOT NULL,
    PRIMARY KEY (hospital_id, transport_id)
);
CREATE INDEX IF NOT EXISTS idx_holdings_capacity ON bed_holdings (hospital_id, bed_type);

-- How far this projection has consumed the log. Without it a stale projection
-- is indistinguishable from a current one, and a crash mid-rebuild is
-- unrecoverable.
CREATE TABLE IF NOT EXISTS projection_checkpoint (
    name     TEXT    PRIMARY KEY,
    last_seq INTEGER NOT NULL
);
"""

_CHECKPOINT = "hospital_roster"

_TABLES = (
    "hospitals",
    "hospital_beds",
    "hospital_capabilities",
    "hospital_max_eta",
    "hospital_status",
    "hospital_specialists",
    "hospital_diverted_categories",
    "bed_holdings",
)


class HospitalReadModel:
    """Projects hospital events into queryable tables.

    Subscribe `apply` to an EventStore and it stays current incrementally; call
    `rebuild` to reconstruct from scratch (startup, or after /demo/reset wipes
    the log).
    """

    def __init__(self, store: EventStore) -> None:
        self._store = store
        with store.transaction() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # -- writing (projector) ------------------------------------------------

    def apply(self, event: Event) -> None:
        """Fold one event in. Anything that isn't a hospital fact is ignored —
        this projection deliberately knows nothing about the handoff protocol.
        """
        handler = _HANDLERS.get(event.type)
        if handler is None:
            return
        with self._store.transaction() as conn:
            handler(conn, event)
            conn.execute(
                "INSERT INTO projection_checkpoint (name, last_seq) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_seq = excluded.last_seq",
                (_CHECKPOINT, event.seq or 0),
            )
            conn.commit()

    def rebuild(self, events: Optional[Sequence[Event]] = None) -> int:
        """Drop everything and replay. Returns the number of events consumed.

        This is the operation that has to be exactly equivalent to the
        incremental path — if the two ever disagree, the tables are not a
        projection any more, they are a second source of truth.
        """
        source = self._store.replay() if events is None else events
        with self._store.transaction() as conn:
            for table in _TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM projection_checkpoint WHERE name = ?", (_CHECKPOINT,))
            last_seq = 0
            for event in source:
                handler = _HANDLERS.get(event.type)
                if handler is not None:
                    handler(conn, event)
                last_seq = event.seq or last_seq
            conn.execute(
                "INSERT INTO projection_checkpoint (name, last_seq) VALUES (?, ?)",
                (_CHECKPOINT, last_seq),
            )
            conn.commit()
        return len(source)

    def last_seq(self) -> int:
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT last_seq FROM projection_checkpoint WHERE name = ?", (_CHECKPOINT,)
            ).fetchone()
        return row[0] if row else 0

    # -- reading ------------------------------------------------------------

    def fetch_all(self, include_retired: bool = False) -> list[dict]:
        """Every hospital, in the same shape the API and the UI already use.

        One query per table rather than one per hospital: six hospitals is
        nothing, but the point of this module is that the cost stops depending
        on how long the deployment has been running.
        """
        with self._store.transaction() as conn:
            where = "" if include_retired else "WHERE decommissioned_seq IS NULL"
            hospitals = conn.execute(
                f"SELECT id, name, location_x, location_y, ventilators_total, "
                f"decommissioned_seq FROM hospitals {where} ORDER BY id"
            ).fetchall()
            if not hospitals:
                return []
            beds = conn.execute("SELECT hospital_id, bed_type, total FROM hospital_beds").fetchall()
            status = conn.execute(
                "SELECT hospital_id, diversion, ed_saturation FROM hospital_status"
            ).fetchall()
            specialists = conn.execute(
                "SELECT hospital_id, specialist FROM hospital_specialists"
            ).fetchall()
            diverted = conn.execute(
                "SELECT hospital_id, condition FROM hospital_diverted_categories"
            ).fetchall()
            holdings = conn.execute(
                "SELECT hospital_id, bed_type, transport_id, state FROM bed_holdings"
            ).fetchall()

        beds_by: dict[str, dict[str, int]] = {}
        for hospital_id, bed_type, total in beds:
            beds_by.setdefault(hospital_id, {})[bed_type] = total
        status_by = {row[0]: (row[1], row[2]) for row in status}
        specialists_by: dict[str, list[str]] = {}
        for hospital_id, specialist in specialists:
            specialists_by.setdefault(hospital_id, []).append(specialist)
        diverted_by: dict[str, list[str]] = {}
        for hospital_id, condition in diverted:
            diverted_by.setdefault(hospital_id, []).append(condition)
        holders_by: dict[str, dict[str, dict[str, list[str]]]] = {}
        for hospital_id, bed_type, transport_id, state in holdings:
            slot = holders_by.setdefault(hospital_id, {}).setdefault(
                bed_type, {"reserved": [], "occupied": []}
            )
            slot["occupied" if state == "OCCUPIED" else "reserved"].append(transport_id)

        views = []
        for hospital_id, name, x, y, ventilators, retired_seq in hospitals:
            totals = beds_by.get(hospital_id, {})
            held = holders_by.get(hospital_id, {})
            diversion, ed_saturation = status_by.get(hospital_id, ("OPEN", 0.0))
            free = {
                bed_type: total
                - len(held.get(bed_type, {}).get("reserved", []))
                - len(held.get(bed_type, {}).get("occupied", []))
                for bed_type, total in totals.items()
            }
            used = sum(total - free[bed_type] for bed_type, total in totals.items())
            capacity = sum(totals.values())
            views.append(
                {
                    "id": hospital_id,
                    "name": name,
                    "location": [x, y],
                    "beds_total": totals,
                    "free": free,
                    "load": (used / capacity) if capacity else 0.0,
                    "holders": {
                        bed_type: {
                            "reserved": sorted(held.get(bed_type, {}).get("reserved", [])),
                            "occupied": sorted(held.get(bed_type, {}).get("occupied", [])),
                        }
                        for bed_type in totals
                    },
                    "diversion": diversion,
                    "diverted_categories": sorted(diverted_by.get(hospital_id, [])),
                    "ed_saturation": ed_saturation,
                    "specialists_on_shift": sorted(specialists_by.get(hospital_id, [])),
                    "ventilators_total": ventilators,
                    "retired": retired_seq is not None,
                }
            )
        return views

    def fetch(self, hospital_id: str) -> Optional[dict]:
        for view in self.fetch_all(include_retired=True):
            if view["id"] == hospital_id:
                return view
        return None


# -- projections, one per event type ----------------------------------------


def _register(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    seq = event.seq or 0
    hospital_id = p["id"]
    conn.execute(
        "INSERT INTO hospitals (id, name, location_x, location_y, ventilators_total, "
        "registered_seq, decommissioned_seq, updated_seq) VALUES (?, ?, ?, ?, ?, ?, NULL, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, location_x=excluded.location_x, "
        "location_y=excluded.location_y, ventilators_total=excluded.ventilators_total, "
        "decommissioned_seq=NULL, updated_seq=excluded.updated_seq",
        (hospital_id, p["name"], p["location"][0], p["location"][1],
         p.get("ventilators_total", 0), seq, seq),
    )
    _replace_children(conn, hospital_id, p, seq)
    # A hospital starts fully open and fully staffed — the same defaults
    # models.initial_status() applies, kept in step deliberately.
    conn.execute(
        "INSERT INTO hospital_status (hospital_id, diversion, ed_saturation, updated_seq) "
        "VALUES (?, 'OPEN', 0.0, ?) ON CONFLICT(hospital_id) DO NOTHING",
        (hospital_id, seq),
    )
    existing = conn.execute(
        "SELECT 1 FROM hospital_specialists WHERE hospital_id = ? LIMIT 1", (hospital_id,)
    ).fetchone()
    if existing is None:
        conn.executemany(
            "INSERT INTO hospital_specialists (hospital_id, specialist) VALUES (?, ?)",
            [(hospital_id, s.value) for s in Specialist],
        )


def _update(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    seq = event.seq or 0
    hospital_id = p["id"]
    conn.execute(
        "UPDATE hospitals SET name=?, location_x=?, location_y=?, ventilators_total=?, "
        "updated_seq=? WHERE id=?",
        (p["name"], p["location"][0], p["location"][1], p.get("ventilators_total", 0),
         seq, hospital_id),
    )
    _replace_children(conn, hospital_id, p, seq)


def _replace_children(conn: sqlite3.Connection, hospital_id: str, payload: dict, seq: int) -> None:
    """Capabilities, wards and ETA overrides are replaced wholesale rather than
    diffed: the event carries the hospital's complete configuration, so a
    partial update could leave a capability behind that the event says is gone.

    Bed *totals* are the exception — BedsReported may already have moved them
    since registration, so an existing ward keeps its reported number and only
    genuinely new wards are inserted.
    """
    conn.execute("DELETE FROM hospital_capabilities WHERE hospital_id = ?", (hospital_id,))
    conn.executemany(
        "INSERT INTO hospital_capabilities (hospital_id, capability) VALUES (?, ?)",
        [(hospital_id, c) for c in payload.get("capabilities", [])],
    )
    conn.execute("DELETE FROM hospital_max_eta WHERE hospital_id = ?", (hospital_id,))
    conn.executemany(
        "INSERT INTO hospital_max_eta (hospital_id, condition, minutes) VALUES (?, ?, ?)",
        [(hospital_id, k, v) for k, v in (payload.get("max_eta_minutes") or {}).items()],
    )
    for bed_type, total in payload.get("beds_total", {}).items():
        conn.execute(
            "INSERT INTO hospital_beds (hospital_id, bed_type, total) VALUES (?, ?, ?) "
            "ON CONFLICT(hospital_id, bed_type) DO NOTHING",
            (hospital_id, bed_type, total),
        )


def _decommission(conn: sqlite3.Connection, event: Event) -> None:
    hospital_id = event.payload.get("hospital_id") or event.transport_id
    conn.execute(
        "UPDATE hospitals SET decommissioned_seq = ?, updated_seq = ? WHERE id = ?",
        (event.seq or 0, event.seq or 0, hospital_id),
    )


def _status_changed(conn: sqlite3.Connection, event: Event) -> None:
    hospital_id = event.facility_id or event.transport_id
    changes = event.payload
    seq = event.seq or 0
    if "diversion" in changes:
        conn.execute(
            "UPDATE hospital_status SET diversion = ?, updated_seq = ? WHERE hospital_id = ?",
            (changes["diversion"], seq, hospital_id),
        )
    if "ed_saturation" in changes:
        conn.execute(
            "UPDATE hospital_status SET ed_saturation = ?, updated_seq = ? WHERE hospital_id = ?",
            (float(changes["ed_saturation"]), seq, hospital_id),
        )
    if "specialists_on_shift" in changes:
        conn.execute("DELETE FROM hospital_specialists WHERE hospital_id = ?", (hospital_id,))
        conn.executemany(
            "INSERT INTO hospital_specialists (hospital_id, specialist) VALUES (?, ?)",
            [(hospital_id, s) for s in changes["specialists_on_shift"]],
        )
    if "diverted_categories" in changes:
        conn.execute(
            "DELETE FROM hospital_diverted_categories WHERE hospital_id = ?", (hospital_id,)
        )
        conn.executemany(
            "INSERT INTO hospital_diverted_categories (hospital_id, condition) VALUES (?, ?)",
            [(hospital_id, c) for c in changes["diverted_categories"]],
        )


def _beds_reported(conn: sqlite3.Connection, event: Event) -> None:
    hospital_id = event.facility_id or event.transport_id
    conn.execute(
        "INSERT INTO hospital_beds (hospital_id, bed_type, total) VALUES (?, ?, ?) "
        "ON CONFLICT(hospital_id, bed_type) DO UPDATE SET total = excluded.total",
        (hospital_id, event.payload["bed_type"], event.payload["total"]),
    )


def _bed_reserved(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO bed_holdings (hospital_id, transport_id, bed_type, state, since_seq) "
        "VALUES (?, ?, ?, 'RESERVED', ?) ON CONFLICT(hospital_id, transport_id) DO UPDATE SET "
        "bed_type = excluded.bed_type, state = 'RESERVED', since_seq = excluded.since_seq",
        (p["hospital_id"], p["transport_id"], p["bed_type"], event.seq or 0),
    )


def _bed_occupied(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO bed_holdings (hospital_id, transport_id, bed_type, state, since_seq) "
        "VALUES (?, ?, ?, 'OCCUPIED', ?) ON CONFLICT(hospital_id, transport_id) DO UPDATE SET "
        "bed_type = excluded.bed_type, state = 'OCCUPIED'",
        (p["hospital_id"], p["transport_id"], p["bed_type"], event.seq or 0),
    )


def _bed_released(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    # Matches _release_bed / _apply_bed_released: released means this transport
    # holds nothing here any more, reservation or occupancy alike.
    conn.execute(
        "DELETE FROM bed_holdings WHERE hospital_id = ? AND transport_id = ?",
        (p["hospital_id"], p["transport_id"]),
    )


_HANDLERS = {
    EventType.HOSPITAL_REGISTERED: _register,
    EventType.HOSPITAL_UPDATED: _update,
    EventType.HOSPITAL_DECOMMISSIONED: _decommission,
    EventType.HOSPITAL_STATUS_CHANGED: _status_changed,
    EventType.BEDS_REPORTED: _beds_reported,
    EventType.BED_RESERVED: _bed_reserved,
    EventType.BED_OCCUPIED: _bed_occupied,
    EventType.BED_RELEASED: _bed_released,
}
