"""Phase 12: the acceptance function — the seven ordered checks that decide
whether one hospital may take one patient right now. Pure and side-effect
free: it reads a patient, one hospital's static facts and live status, and
that hospital's ledger view, and returns a Decision. Used two ways for the
same rule (the spec's own requirement): the facility calls this on its own
live status when a PREPARE arrives (authoritative — Phase 14's F1), the
dispatcher calls it on its last-known status to pre-filter/rank candidates
before ever sending a PREPARE (this module's caller in scoring.py, and
Phase 13's dispatcher).

Signature note: the spec sketches accept() as
accept(patient, status, ledger_view, eta_minutes). Capabilities and a
hospital's own max_eta_minutes overrides are Hospital-level static facts in
the spec's own data model (Hospital.capabilities, Hospital.max_eta_minutes),
not HospitalStatus fields, so `hospital` is added here as an explicit
parameter rather than duplicated onto HospitalStatus. `saturation_limit` is
also explicit (this codebase's convention is config passed in, never read
from a global — see Facility/Dispatcher/Bus all taking `config` by
constructor injection) rather than reached for as a module-level default.
The seven checks and their reasons are unchanged from the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
    max_eta_for,
)
from app.projection import HospitalLedgerView, free, ventilators_free


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: Optional[str]
    bed_type: Optional[BedType]


def required_bed_type(patient: Patient) -> BedType:
    """First match in order, per spec — a patient needing isolation always
    gets an isolation bed regardless of acuity or age; acuity outranks age
    (a critical neonate needs an ICU bed, not a paediatric-ward one; see
    E39's own patient, which relies on acuity NOT being <=2 to reach the
    age-group row below)."""
    if Need.ISOLATION in patient.needs:
        return BedType.ISOLATION
    if patient.acuity <= 2:
        return BedType.ICU
    if patient.age_group in (AgeGroup.CHILD, AgeGroup.NEONATE):
        return BedType.PAEDIATRIC
    return BedType.GENERAL


def required_capability_groups(patient: Patient) -> list[frozenset[Capability]]:
    """Each group is an "at least one of" requirement — most are a single
    capability, but trauma's rule ("TRAUMA_L1 or TRAUMA_L2", "any
    TRAUMA_Lx") is genuinely disjunctive, so a group, not a single value, is
    the right shape for all of them."""
    groups: list[frozenset[Capability]] = []
    condition = patient.condition

    if condition is ConditionCategory.CARDIAC:
        groups.append(frozenset({Capability.CATH_LAB}))
    elif condition is ConditionCategory.STROKE:
        groups.append(frozenset({Capability.STROKE_CENTRE}))
        groups.append(frozenset({Capability.CT_SCAN}))
    elif condition is ConditionCategory.TRAUMA:
        if patient.acuity <= 2:
            groups.append(frozenset({Capability.TRAUMA_L1, Capability.TRAUMA_L2}))
        else:
            groups.append(frozenset({Capability.TRAUMA_L1, Capability.TRAUMA_L2, Capability.TRAUMA_L3}))
    elif condition is ConditionCategory.BURN:
        groups.append(frozenset({Capability.BURN_UNIT}))

    if patient.age_group is AgeGroup.NEONATE:
        groups.append(frozenset({Capability.NICU}))
    if Need.VENTILATOR in patient.needs:
        groups.append(frozenset({Capability.VENTILATORS}))
    if Need.CT_SCAN in patient.needs:
        groups.append(frozenset({Capability.CT_SCAN}))
    if Need.BARIATRIC in patient.needs:
        groups.append(frozenset({Capability.BARIATRIC}))
    if Need.BLOOD_PRODUCTS in patient.needs:
        groups.append(frozenset({Capability.BLOOD_BANK}))
    return groups


def _group_label(group: frozenset[Capability]) -> str:
    return "_OR_".join(sorted(capability.value for capability in group))


def required_specialist(patient: Patient) -> Specialist:
    """First match in order, mirroring required_bed_type's own precedence
    rule — a cardiac patient who happens to be a child still needs
    cardiology first per the spec's literal ordering (paediatrics is the
    condition-independent fallback for a non-adult, checked after every
    condition-specific row)."""
    condition = patient.condition
    if condition is ConditionCategory.CARDIAC:
        return Specialist.CARDIOLOGY
    if condition is ConditionCategory.STROKE:
        return Specialist.NEUROLOGY
    if condition is ConditionCategory.TRAUMA:
        return Specialist.TRAUMA_SURGERY
    if condition is ConditionCategory.OBSTETRIC:
        return Specialist.OBSTETRICS
    if condition is ConditionCategory.PAEDIATRIC or patient.age_group is not AgeGroup.ADULT:
        return Specialist.PAEDIATRICS
    if condition is ConditionCategory.PSYCHIATRIC:
        return Specialist.PSYCHIATRY
    return Specialist.EMERGENCY


def accept(
    patient: Patient,
    hospital: Hospital,
    status: HospitalStatus,
    ledger_view: HospitalLedgerView,
    eta_minutes: float,
    saturation_limit: float,
) -> Decision:
    """Run the seven checks in the spec's exact order, returning the first
    failure — never any earlier partial result, so acceptance always has
    exactly one authoritative reason."""
    override = patient.override == "nearest_capable"

    # 1. Diversion.
    if status.diversion is Diversion.FULL:
        return Decision(False, "on_full_diversion", None)
    if status.diversion is Diversion.PARTIAL and patient.condition in status.diverted_categories:
        return Decision(False, f"on_partial_diversion:{patient.condition.value}", None)

    # 2. Capabilities (+ the ventilator-specific equipment check).
    for group in required_capability_groups(patient):
        if not (hospital.capabilities & group):
            return Decision(False, f"no_capability:{_group_label(group)}", None)
    if Need.VENTILATOR in patient.needs and ventilators_free(ledger_view, status) <= 0:
        return Decision(False, "no_ventilator", None)

    # 3. Bed availability.
    bed_type = required_bed_type(patient)
    if free(ledger_view, status, bed_type) <= 0:
        return Decision(False, f"no_bed:{bed_type.value}", None)

    # 4. Staffing.
    specialist = required_specialist(patient)
    if specialist not in status.specialists_on_shift:
        return Decision(False, f"no_specialist:{specialist.value}", None)

    # 5. Transport-time window (skipped for a nearest-capable override).
    if not override and eta_minutes > max_eta_for(hospital, patient.condition):
        return Decision(False, "outside_window", None)

    # 6. ED saturation (skipped for a nearest-capable override).
    if not override and status.ed_saturation > saturation_limit and patient.acuity >= 3:
        return Decision(False, "ed_saturated", None)

    # 7. Accept.
    return Decision(True, None, bed_type)
