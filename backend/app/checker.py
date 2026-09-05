"""The invariant checker. A facility is *effectively* active for a transport
iff its local state is ACTIVE and it is that transport's current_destination
(see the README's "effective-active rule") — that definition is what makes
this a pure, independent audit rather than a re-statement of whatever the
dispatcher already believes: it walks the raw event log itself and never
imports Dispatcher or Facility.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.events import Event, EventType
from app.messages import FacilityState


class ViolationKind(str, Enum):
    MULTI_ACTIVE = "MULTI_ACTIVE"
    ZERO_ACTIVE = "ZERO_ACTIVE"
    ARRIVED_AT_WRONG_FACILITY = "ARRIVED_AT_WRONG_FACILITY"


@dataclass(frozen=True)
class Violation:
    transport_id: str
    ts_ms: int
    kind: ViolationKind
    detail: dict


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    violations: list[Violation]
    max_local_overlap_ms: int
    transitions_checked: int


def check(events: list[Event]) -> CheckResult:
    violations: list[Violation] = []
    max_local_overlap_ms = 0
    transitions_checked = 0

    for transport_id, transport_events in _group_by_transport(events).items():
        result = _check_transport(transport_id, transport_events)
        violations.extend(result.violations)
        max_local_overlap_ms = max(max_local_overlap_ms, result.max_local_overlap_ms)
        transitions_checked += result.transitions_checked

    return CheckResult(
        passed=len(violations) == 0,
        violations=violations,
        max_local_overlap_ms=max_local_overlap_ms,
        transitions_checked=transitions_checked,
    )


def _group_by_transport(events: list[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.transport_id, []).append(event)
    return grouped


def _check_transport(transport_id: str, events: list[Event]) -> CheckResult:
    violations: list[Violation] = []
    transitions_checked = 0
    max_local_overlap_ms = 0

    current_destination: Optional[str] = None
    facility_states: dict[str, FacilityState] = {}
    overlap_started_at: dict[str, int] = {}  # facility_id -> ts_ms it went ACTIVE while not current

    for _, instant in _group_by_instant(events):
        for event in instant:
            if event.type is EventType.CUTOVER_APPLIED:
                current_destination = event.payload["current_destination"]
            elif event.type is EventType.FACILITY_STATE_CHANGED and event.facility_id is not None:
                facility_states[event.facility_id] = FacilityState(event.payload["to"])
            elif event.type is EventType.ARRIVED:
                # E19: compared against current, never pending — pending
                # isn't even tracked here.
                if current_destination is not None and event.payload["at"] != current_destination:
                    violations.append(
                        Violation(
                            transport_id,
                            event.ts_ms,
                            ViolationKind.ARRIVED_AT_WRONG_FACILITY,
                            {"arrived_at": event.payload["at"], "current_destination": current_destination},
                        )
                    )
            # CutoverCancelled and RedirectAborted deliberately leave
            # current_destination untouched — neither one changes who's
            # actually receiving the patient.

        if current_destination is None:
            continue

        ts_ms = instant[-1].ts_ms
        transitions_checked += 1
        current_is_active = facility_states.get(current_destination) is FacilityState.ACTIVE
        if not current_is_active:
            violations.append(
                Violation(
                    transport_id,
                    ts_ms,
                    ViolationKind.ZERO_ACTIVE,
                    {"current_destination": current_destination, "facility_states": _snapshot(facility_states)},
                )
            )

        # Overlap is informational only (spec: a stale facility being
        # locally ACTIVE "is not a violation of this invariant") — tracked
        # here purely for max_local_overlap_ms.
        for facility_id, state in facility_states.items():
            is_overlapping = facility_id != current_destination and state is FacilityState.ACTIVE
            if is_overlapping:
                overlap_started_at.setdefault(facility_id, ts_ms)
            elif facility_id in overlap_started_at:
                max_local_overlap_ms = max(max_local_overlap_ms, ts_ms - overlap_started_at.pop(facility_id))

    if events:
        last_ts_ms = events[-1].ts_ms
        for start_ms in overlap_started_at.values():
            max_local_overlap_ms = max(max_local_overlap_ms, last_ts_ms - start_ms)

    return CheckResult(
        passed=len(violations) == 0,
        violations=violations,
        max_local_overlap_ms=max_local_overlap_ms,
        transitions_checked=transitions_checked,
    )


def _group_by_instant(events: list[Event]) -> list[tuple[int, list[Event]]]:
    """Groups consecutive (already seq-ordered) events sharing one ts_ms —
    an "atomic instant" per the spec — preserving arrival order."""
    groups: list[tuple[int, list[Event]]] = []
    for event in events:
        if groups and groups[-1][0] == event.ts_ms:
            groups[-1][1].append(event)
        else:
            groups.append((event.ts_ms, [event]))
    return groups


def _snapshot(facility_states: dict[str, FacilityState]) -> dict[str, str]:
    return {facility_id: state.value for facility_id, state in facility_states.items()}
