"""Phase 12: eta_minutes, score, rank, choose — deterministic ranking of
hospital candidates for one transport. Pure functions only, same spirit as
acceptance.py: no I/O, no clock, no bus — a caller (the dispatcher, the
demo's /candidates route) supplies every live fact explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.acceptance import accept
from app.models import Hospital, HospitalStatus, Patient
from app.projection import HospitalLedgerView, empty_ledger_view, load


def eta_minutes(position: tuple[float, float], hospital: Hospital, speed_km_per_min: float) -> float:
    dx = position[0] - hospital.location[0]
    dy = position[1] - hospital.location[1]
    distance_km = (dx * dx + dy * dy) ** 0.5
    return distance_km / speed_km_per_min


def _evaluate(
    patient: Patient,
    position: tuple[float, float],
    hospital: Hospital,
    status: HospitalStatus,
    ledger_view: HospitalLedgerView,
    speed_km_per_min: float,
    load_weight: float,
    saturation_limit: float,
) -> tuple[Optional[float], Optional[str]]:
    """(score, exclusion_reason) — exactly one of the two is None. Shared by
    score() and rank() so accept()/eta_minutes() are computed once per
    hospital, not twice."""
    eta = eta_minutes(position, hospital, speed_km_per_min)
    decision = accept(patient, hospital, status, ledger_view, eta, saturation_limit)
    if not decision.accepted:
        return None, decision.reason
    if patient.override == "nearest_capable":
        return -eta, None
    return -eta - load_weight * load(ledger_view, status), None


def score(
    patient: Patient,
    position: tuple[float, float],
    hospital: Hospital,
    status: HospitalStatus,
    ledger_view: HospitalLedgerView,
    speed_km_per_min: float,
    load_weight: float,
    saturation_limit: float,
) -> Optional[float]:
    """None if accept() fails on this (the caller's) view of the hospital;
    otherwise -eta, minus a load penalty unless the patient is on a
    nearest-capable override (load is ignored for that case per spec)."""
    value, _reason = _evaluate(
        patient, position, hospital, status, ledger_view, speed_km_per_min, load_weight, saturation_limit
    )
    return value


@dataclass(frozen=True)
class RankedCandidate:
    hospital_id: str
    score: Optional[float]  # None means excluded
    reason: Optional[str]  # the accept() failure reason; set only when excluded


def rank(
    patient: Patient,
    position: tuple[float, float],
    hospitals: list[Hospital],
    statuses: dict[str, HospitalStatus],
    ledger: dict[str, HospitalLedgerView],
    speed_km_per_min: float,
    load_weight: float,
    saturation_limit: float,
) -> list[RankedCandidate]:
    """Sorted by score descending; excluded hospitals (score=None) listed
    last with their exclusion reason; ties broken by hospital_id."""
    accepted: list[RankedCandidate] = []
    excluded: list[RankedCandidate] = []
    for hospital in hospitals:
        status = statuses[hospital.id]
        view = ledger.get(hospital.id, empty_ledger_view(hospital.id))
        value, reason = _evaluate(
            patient, position, hospital, status, view, speed_km_per_min, load_weight, saturation_limit
        )
        if value is not None:
            accepted.append(RankedCandidate(hospital.id, value, None))
        else:
            excluded.append(RankedCandidate(hospital.id, None, reason))

    accepted.sort(key=lambda candidate: (-candidate.score, candidate.hospital_id))
    excluded.sort(key=lambda candidate: candidate.hospital_id)
    return accepted + excluded


def choose(
    patient: Patient,
    position: tuple[float, float],
    hospitals: list[Hospital],
    statuses: dict[str, HospitalStatus],
    ledger: dict[str, HospitalLedgerView],
    speed_km_per_min: float,
    load_weight: float,
    saturation_limit: float,
) -> Optional[str]:
    ranked = rank(patient, position, hospitals, statuses, ledger, speed_km_per_min, load_weight, saturation_limit)
    return ranked[0].hospital_id if ranked and ranked[0].score is not None else None
