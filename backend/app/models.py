"""Phase 12: the capacity-aware data model — hospitals, their live status,
patients and the richer multi-transport record. Pure data only (frozen
dataclasses and enums); the ledger that reads these against the event log
lives in projection.py, acceptance in acceptance.py, ranking in scoring.py.

This sits alongside seed.py's original HospitalSeed/PatientSeed/
TransportSeed rather than replacing them (see seed.py's docstring): the
single-transport demo (AMB-101, three hospitals) that Phases 0-11 built is
untouched and keeps using those. Hospital/Patient/Transport here are the
richer records the multi-transport extension (Phase 12+) adds on top.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


class BedType(Enum):
    GENERAL = "GENERAL"
    ICU = "ICU"
    ISOLATION = "ISOLATION"
    PAEDIATRIC = "PAEDIATRIC"


class Capability(Enum):
    CATH_LAB = "CATH_LAB"
    STROKE_CENTRE = "STROKE_CENTRE"
    TRAUMA_L1 = "TRAUMA_L1"
    TRAUMA_L2 = "TRAUMA_L2"
    TRAUMA_L3 = "TRAUMA_L3"
    BURN_UNIT = "BURN_UNIT"
    NICU = "NICU"
    CT_SCAN = "CT_SCAN"
    VENTILATORS = "VENTILATORS"
    BLOOD_BANK = "BLOOD_BANK"
    BARIATRIC = "BARIATRIC"


class Specialist(Enum):
    CARDIOLOGY = "CARDIOLOGY"
    NEUROLOGY = "NEUROLOGY"
    TRAUMA_SURGERY = "TRAUMA_SURGERY"
    OBSTETRICS = "OBSTETRICS"
    PAEDIATRICS = "PAEDIATRICS"
    PSYCHIATRY = "PSYCHIATRY"
    EMERGENCY = "EMERGENCY"


class Diversion(Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class ConditionCategory(Enum):
    CARDIAC = "CARDIAC"
    TRAUMA = "TRAUMA"
    STROKE = "STROKE"
    BURN = "BURN"
    RESPIRATORY = "RESPIRATORY"
    OBSTETRIC = "OBSTETRIC"
    PAEDIATRIC = "PAEDIATRIC"
    PSYCHIATRIC = "PSYCHIATRIC"
    GENERAL = "GENERAL"


class AgeGroup(Enum):
    NEONATE = "NEONATE"
    CHILD = "CHILD"
    ADULT = "ADULT"


class Need(Enum):
    VENTILATOR = "VENTILATOR"
    ISOLATION = "ISOLATION"
    CATH_LAB = "CATH_LAB"
    CT_SCAN = "CT_SCAN"
    BARIATRIC = "BARIATRIC"
    BLOOD_PRODUCTS = "BLOOD_PRODUCTS"


class Policy(Enum):
    MANUAL = "manual"
    AUTO = "auto"


# Default max_eta_minutes per condition category — used whenever a hospital's
# own max_eta_minutes doesn't override a category (see max_eta_for below).
DEFAULT_MAX_ETA_MINUTES: dict[ConditionCategory, int] = {
    ConditionCategory.STROKE: 45,
    ConditionCategory.CARDIAC: 30,
    ConditionCategory.TRAUMA: 60,
    ConditionCategory.BURN: 90,
    ConditionCategory.OBSTETRIC: 60,
    ConditionCategory.RESPIRATORY: 45,
    ConditionCategory.PSYCHIATRIC: 120,
    ConditionCategory.PAEDIATRIC: 60,
    ConditionCategory.GENERAL: 90,
}


@dataclass(frozen=True)
class Hospital:
    """Static configuration for one hospital — the six-hospital network's
    fixed facts (location, total bed counts, what it's equipped to treat).
    What actually varies over time lives in HospitalStatus instead, so this
    stays immutable and safe to share everywhere."""

    id: str
    name: str
    location: tuple[float, float]  # km on the 40x40 grid
    beds_total: dict[BedType, int]
    capabilities: frozenset[Capability]
    ventilators_total: int
    max_eta_minutes: dict[ConditionCategory, int] = field(default_factory=dict)


def hospital_to_payload(hospital: Hospital) -> dict:
    """Hospital -> a JSON-safe event payload. Enums become their values and
    the frozenset becomes a sorted list, so replaying the same log twice
    always rebuilds the identical Hospital (event payloads are persisted as
    JSON text; an unordered set would not round-trip stably)."""
    return {
        "id": hospital.id,
        "name": hospital.name,
        "location": list(hospital.location),
        "beds_total": {bed_type.value: total for bed_type, total in hospital.beds_total.items()},
        "capabilities": sorted(capability.value for capability in hospital.capabilities),
        "ventilators_total": hospital.ventilators_total,
        "max_eta_minutes": {
            condition.value: minutes for condition, minutes in hospital.max_eta_minutes.items()
        },
    }


def hospital_from_payload(payload: dict) -> Hospital:
    """The inverse. Raises ValueError on an unknown enum member, which is the
    correct failure: a bed type or capability that no longer exists in this
    build cannot be honoured, and silently dropping it would make a hospital
    quietly less capable than its own registration event says."""
    return Hospital(
        id=payload["id"],
        name=payload["name"],
        location=(float(payload["location"][0]), float(payload["location"][1])),
        beds_total={BedType(key): int(total) for key, total in payload["beds_total"].items()},
        capabilities=frozenset(Capability(value) for value in payload.get("capabilities", [])),
        ventilators_total=int(payload.get("ventilators_total", 0)),
        max_eta_minutes={
            ConditionCategory(key): int(minutes)
            for key, minutes in (payload.get("max_eta_minutes") or {}).items()
        },
    )


def max_eta_for(hospital: Hospital, condition: ConditionCategory) -> int:
    """A hospital's own max_eta_minutes overrides the category default only
    where it names that category explicitly."""
    return hospital.max_eta_minutes.get(condition, DEFAULT_MAX_ETA_MINUTES[condition])


@dataclass(frozen=True)
class HospitalStatus:
    """Everything about a hospital that changes over time. Phase 12 only
    defines the shape; Phase 14's apply_status_event() is what actually
    derives one of these from HospitalStatusChanged/BedsReported events,
    replayable the same way FacilityView/TransportView are (see
    projection.py). beds_total/ventilators_total start equal to the
    matching Hospital's own but can drift independently via BedsReported."""

    specialists_on_shift: frozenset[Specialist]
    ed_saturation: float
    diversion: Diversion
    diverted_categories: frozenset[ConditionCategory]
    beds_total: dict[BedType, int]
    ventilators_total: int


def initial_status(hospital: Hospital) -> HospitalStatus:
    """A hospital's status before any HospitalStatusChanged/BedsReported
    event has been applied — fully open, fully staffed, capacity matching
    its static Hospital record. seed.py overrides specific fields on top of
    this for the hospitals that must start in a non-default state (E.g. one
    hospital seeded on PARTIAL diversion for TRAUMA)."""
    return HospitalStatus(
        specialists_on_shift=frozenset(Specialist),
        ed_saturation=0.0,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(hospital.beds_total),
        ventilators_total=hospital.ventilators_total,
    )


@dataclass(frozen=True)
class Patient:
    id: str
    acuity: int  # 1 critical ... 5 minor
    condition: ConditionCategory
    age_group: AgeGroup
    needs: frozenset[Need] = field(default_factory=frozenset)
    override: Literal["none", "nearest_capable"] = "none"


@dataclass(frozen=True)
class Transport:
    """The multi-transport extension's richer transport record — one per
    ambulance. Distinct from seed.TransportSeed (which the original
    single-demo-transport code path keeps using unchanged): this adds the
    2D position a moving ambulance needs for scoring.eta_minutes, on top of
    the same id/patient shape."""

    transport_id: str
    patient: Patient
    origin: tuple[float, float]
    position: tuple[float, float]
    initial_destination: Optional[str] = None
