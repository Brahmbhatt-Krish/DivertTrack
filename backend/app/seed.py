"""Static demo data: the three hospitals, the one sample transport, and the
default network parameters simulation.py (Phase 6) builds its Bus from.

Phase 12 adds a second, richer seed (MULTI_HOSPITALS, INITIAL_HOSPITAL_
STATUSES, SAMPLE_PATIENTS, random_patient) for the multi-hospital capacity
extension, alongside — not replacing — HOSPITALS/TRANSPORT above, which the
original single-transport demo keeps using unchanged."""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.config import settings
from app.models import (
    AgeGroup,
    BedType,
    Capability,
    ConditionCategory,
    Diversion,
    Hospital,
    HospitalStatus,
    Need,
    Patient,
    Specialist,
)


@dataclass(frozen=True)
class HospitalSeed:
    facility_id: str
    beds_available: int
    distance_km: float
    specialities: tuple[str, ...]


HOSPITALS: tuple[HospitalSeed, ...] = (
    HospitalSeed("Hospital_A", beds_available=6, distance_km=4.2, specialities=("trauma", "cardiac")),
    HospitalSeed("Hospital_B", beds_available=3, distance_km=6.8, specialities=("stroke", "trauma")),
    HospitalSeed("Hospital_C", beds_available=9, distance_km=9.5, specialities=("burn", "pediatric")),
)


@dataclass(frozen=True)
class PatientSeed:
    patient_id: str
    acuity: int
    condition: str


@dataclass(frozen=True)
class TransportSeed:
    transport_id: str
    patient: PatientSeed
    initial_destination: str


TRANSPORT = TransportSeed(
    transport_id="AMB-101",
    patient=PatientSeed(patient_id="PT-01", acuity=3, condition="chest pain"),
    initial_destination="Hospital_A",
)


@dataclass(frozen=True)
class NetworkSeed:
    min_delay_ms: int
    max_delay_ms: int
    duplicate_rate: float


NETWORK = NetworkSeed(min_delay_ms=50, max_delay_ms=settings.d_max_ms, duplicate_rate=0.05)


# -- Phase 12: the six-hospital network (multi-hospital capacity extension) -
# Distinct capability mixes and locations spread across the 40x40 km grid,
# covering every constraint the spec calls for: Hospital_3 has no ICU beds,
# Hospital_2 has no paediatric beds, Hospital_4 has no cath lab, Hospital_2
# starts on PARTIAL diversion for TRAUMA.

MULTI_HOSPITALS: tuple[Hospital, ...] = (
    Hospital(
        id="Hospital_1",
        name="Riverside General",
        location=(8.0, 8.0),
        beds_total={BedType.GENERAL: 20, BedType.ICU: 6, BedType.PAEDIATRIC: 4, BedType.ISOLATION: 2},
        capabilities=frozenset(
            {
                Capability.TRAUMA_L1, Capability.CATH_LAB, Capability.STROKE_CENTRE, Capability.CT_SCAN,
                Capability.NICU, Capability.BURN_UNIT, Capability.BLOOD_BANK, Capability.VENTILATORS,
                Capability.BARIATRIC,
            }
        ),
        ventilators_total=10,
    ),
    Hospital(
        id="Hospital_2",
        name="Eastgate Trauma & Cardiac",
        location=(32.0, 8.0),
        beds_total={BedType.GENERAL: 15, BedType.ICU: 5, BedType.ISOLATION: 1},  # no paediatric beds
        capabilities=frozenset(
            {Capability.TRAUMA_L1, Capability.CATH_LAB, Capability.CT_SCAN, Capability.BLOOD_BANK, Capability.VENTILATORS}
        ),
        ventilators_total=6,
    ),
    Hospital(
        id="Hospital_3",
        name="Northshore Children's",
        location=(8.0, 32.0),
        beds_total={BedType.GENERAL: 10, BedType.PAEDIATRIC: 8, BedType.ISOLATION: 2},  # no ICU beds
        capabilities=frozenset({Capability.NICU, Capability.CT_SCAN, Capability.VENTILATORS}),
        ventilators_total=4,
    ),
    Hospital(
        id="Hospital_4",
        name="Westfield Community",
        location=(32.0, 32.0),
        beds_total={BedType.GENERAL: 12, BedType.ICU: 3},
        capabilities=frozenset({Capability.TRAUMA_L3, Capability.CT_SCAN}),  # no cath lab
        ventilators_total=3,
    ),
    Hospital(
        id="Hospital_5",
        name="Midtown Stroke Centre",
        location=(20.0, 15.0),
        beds_total={BedType.GENERAL: 14, BedType.ICU: 4, BedType.PAEDIATRIC: 2},
        capabilities=frozenset({Capability.STROKE_CENTRE, Capability.CT_SCAN, Capability.CATH_LAB}),
        ventilators_total=4,
    ),
    Hospital(
        id="Hospital_6",
        name="Southport Burn & Isolation",
        location=(20.0, 32.0),
        beds_total={BedType.GENERAL: 10, BedType.ICU: 3, BedType.ISOLATION: 4},
        capabilities=frozenset(
            {Capability.BURN_UNIT, Capability.BLOOD_BANK, Capability.TRAUMA_L2, Capability.CT_SCAN}
        ),
        ventilators_total=3,
    ),
)

INITIAL_HOSPITAL_STATUSES: dict[str, HospitalStatus] = {
    "Hospital_1": HospitalStatus(
        specialists_on_shift=frozenset(Specialist),  # fully staffed tertiary centre
        ed_saturation=0.4,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(MULTI_HOSPITALS[0].beds_total),
        ventilators_total=MULTI_HOSPITALS[0].ventilators_total,
    ),
    "Hospital_2": HospitalStatus(
        specialists_on_shift=frozenset({Specialist.CARDIOLOGY, Specialist.TRAUMA_SURGERY, Specialist.EMERGENCY}),
        ed_saturation=0.6,
        diversion=Diversion.PARTIAL,
        diverted_categories=frozenset({ConditionCategory.TRAUMA}),
        beds_total=dict(MULTI_HOSPITALS[1].beds_total),
        ventilators_total=MULTI_HOSPITALS[1].ventilators_total,
    ),
    "Hospital_3": HospitalStatus(
        specialists_on_shift=frozenset({Specialist.PAEDIATRICS, Specialist.EMERGENCY}),
        ed_saturation=0.3,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(MULTI_HOSPITALS[2].beds_total),
        ventilators_total=MULTI_HOSPITALS[2].ventilators_total,
    ),
    "Hospital_4": HospitalStatus(
        specialists_on_shift=frozenset({Specialist.EMERGENCY, Specialist.TRAUMA_SURGERY}),
        ed_saturation=0.5,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(MULTI_HOSPITALS[3].beds_total),
        ventilators_total=MULTI_HOSPITALS[3].ventilators_total,
    ),
    "Hospital_5": HospitalStatus(
        specialists_on_shift=frozenset({Specialist.NEUROLOGY, Specialist.CARDIOLOGY, Specialist.EMERGENCY}),
        ed_saturation=0.45,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(MULTI_HOSPITALS[4].beds_total),
        ventilators_total=MULTI_HOSPITALS[4].ventilators_total,
    ),
    "Hospital_6": HospitalStatus(
        specialists_on_shift=frozenset({Specialist.TRAUMA_SURGERY, Specialist.EMERGENCY}),
        ed_saturation=0.35,
        diversion=Diversion.OPEN,
        diverted_categories=frozenset(),
        beds_total=dict(MULTI_HOSPITALS[5].beds_total),
        ventilators_total=MULTI_HOSPITALS[5].ventilators_total,
    ),
}

# Three illustrative patients for Phase 12's smoke check — chosen to exercise
# a capability check (cath lab), a diversion decline (Hospital_2 is on
# PARTIAL diversion for TRAUMA), and the NICU/paediatric-bed edge case (E39).
SAMPLE_PATIENTS: tuple[Patient, ...] = (
    Patient(id="P-CARDIAC", acuity=2, condition=ConditionCategory.CARDIAC, age_group=AgeGroup.ADULT),
    Patient(id="P-TRAUMA", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT),
    Patient(id="P-NEONATE", acuity=4, condition=ConditionCategory.GENERAL, age_group=AgeGroup.NEONATE),
)


def random_patient(rng: random.Random, patient_id: str) -> Patient:
    """A patient generator for fuzzing/demos (Phase 19 reuses this) — every
    field drawn independently, biased toward the common case (adult,
    no special equipment needs, no override)."""
    age_group = rng.choices(
        [AgeGroup.ADULT, AgeGroup.CHILD, AgeGroup.NEONATE], weights=[0.8, 0.15, 0.05]
    )[0]
    needs = frozenset(need for need in Need if rng.random() < 0.12)
    override = "nearest_capable" if rng.random() < 0.1 else "none"
    return Patient(
        id=patient_id,
        acuity=rng.randint(1, 5),
        condition=rng.choice(list(ConditionCategory)),
        age_group=age_group,
        needs=needs,
        override=override,
    )
