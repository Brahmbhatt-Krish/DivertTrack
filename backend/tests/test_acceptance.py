"""Phase 19: acceptance.py — the seven ordered checks, required_bed_type's
precedence, required_capability_groups' disjunctive trauma rule."""
from app.acceptance import accept, required_bed_type, required_capability_groups, required_specialist
from app.models import (
    AgeGroup, BedType, Capability, ConditionCategory, Diversion, Hospital, HospitalStatus, Need, Patient, Specialist,
)
from app.projection import empty_ledger_view

HOSPITAL = Hospital(
    id="H1", name="H1", location=(0.0, 0.0),
    beds_total={BedType.GENERAL: 5, BedType.ICU: 2, BedType.PAEDIATRIC: 2, BedType.ISOLATION: 1},
    capabilities=frozenset({Capability.CATH_LAB, Capability.NICU, Capability.VENTILATORS}),
    ventilators_total=1,
)
OPEN_STATUS = HospitalStatus(
    specialists_on_shift=frozenset(Specialist),
    ed_saturation=0.0,
    diversion=Diversion.OPEN,
    diverted_categories=frozenset(),
    beds_total=dict(HOSPITAL.beds_total),
    ventilators_total=HOSPITAL.ventilators_total,
)
EMPTY_VIEW = empty_ledger_view(HOSPITAL.id)


def _patient(**overrides) -> Patient:
    defaults = dict(id="P1", acuity=3, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)
    defaults.update(overrides)
    return Patient(**defaults)


def test_required_bed_type_precedence_isolation_beats_acuity_beats_age() -> None:
    assert required_bed_type(_patient(needs=frozenset({Need.ISOLATION}), acuity=1)) == BedType.ISOLATION
    assert required_bed_type(_patient(acuity=2, age_group=AgeGroup.NEONATE)) == BedType.ICU  # E39's own reasoning
    assert required_bed_type(_patient(acuity=4, age_group=AgeGroup.NEONATE)) == BedType.PAEDIATRIC
    assert required_bed_type(_patient(acuity=4, age_group=AgeGroup.ADULT)) == BedType.GENERAL


def test_trauma_capability_is_disjunctive_by_acuity() -> None:
    critical = required_capability_groups(_patient(condition=ConditionCategory.TRAUMA, acuity=2))
    assert critical == [frozenset({Capability.TRAUMA_L1, Capability.TRAUMA_L2})]
    non_critical = required_capability_groups(_patient(condition=ConditionCategory.TRAUMA, acuity=4))
    assert non_critical == [frozenset({Capability.TRAUMA_L1, Capability.TRAUMA_L2, Capability.TRAUMA_L3})]


def test_required_specialist_condition_beats_age_fallback() -> None:
    # A cardiac child still needs cardiology first per the spec's literal
    # ordering (paediatrics is the non-adult fallback, checked after every
    # condition-specific row) — see acceptance.py's docstring.
    assert required_specialist(_patient(condition=ConditionCategory.CARDIAC, age_group=AgeGroup.CHILD)) is Specialist.CARDIOLOGY
    assert required_specialist(_patient(condition=ConditionCategory.GENERAL, age_group=AgeGroup.CHILD)) is Specialist.PAEDIATRICS
    assert required_specialist(_patient(condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)) is Specialist.EMERGENCY


def test_check_1_full_diversion_declines_everyone() -> None:
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "diversion": Diversion.FULL})
    decision = accept(_patient(), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert not decision.accepted
    assert decision.reason == "on_full_diversion"


def test_e38_partial_diversion_declines_matching_category_accepts_others() -> None:
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "diversion": Diversion.PARTIAL, "diverted_categories": frozenset({ConditionCategory.TRAUMA})})
    trauma = accept(_patient(condition=ConditionCategory.TRAUMA, acuity=4), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert not trauma.accepted and trauma.reason == "on_partial_diversion:TRAUMA"
    cardiac = accept(_patient(condition=ConditionCategory.CARDIAC), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert cardiac.accepted


def test_check_2_missing_capability_declines_with_name() -> None:
    decision = accept(_patient(condition=ConditionCategory.STROKE), HOSPITAL, OPEN_STATUS, EMPTY_VIEW, 0.0, 0.9)
    assert not decision.accepted
    assert decision.reason.startswith("no_capability:")


def test_e39_neonate_needs_nicu_and_paediatric_bed() -> None:
    no_nicu = Hospital(id="H2", name="H2", location=(0, 0), beds_total={BedType.PAEDIATRIC: 3}, capabilities=frozenset(), ventilators_total=0)
    status = HospitalStatus(frozenset(Specialist), 0.0, Diversion.OPEN, frozenset(), dict(no_nicu.beds_total), 0)
    decision = accept(_patient(age_group=AgeGroup.NEONATE, acuity=4), no_nicu, status, empty_ledger_view(no_nicu.id), 0.0, 0.9)
    assert not decision.accepted
    assert decision.reason == "no_capability:NICU"


def test_e40_ventilator_need_checked_even_with_a_free_icu_bed() -> None:
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "ventilators_total": 0})
    decision = accept(_patient(needs=frozenset({Need.VENTILATOR}), acuity=1), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert not decision.accepted
    assert decision.reason == "no_ventilator"


def test_check_3_no_bed_declines() -> None:
    full = Hospital(id="H3", name="H3", location=(0, 0), beds_total={BedType.GENERAL: 0}, capabilities=frozenset(), ventilators_total=0)
    status = HospitalStatus(frozenset(Specialist), 0.0, Diversion.OPEN, frozenset(), dict(full.beds_total), 0)
    decision = accept(_patient(), full, status, empty_ledger_view(full.id), 0.0, 0.9)
    assert not decision.accepted and decision.reason == "no_bed:GENERAL"


def test_check_4_no_specialist_declines() -> None:
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "specialists_on_shift": frozenset()})
    decision = accept(_patient(), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert not decision.accepted and decision.reason == "no_specialist:EMERGENCY"


def test_check_5_outside_window_declines_unless_override() -> None:
    decision = accept(_patient(condition=ConditionCategory.CARDIAC), HOSPITAL, OPEN_STATUS, EMPTY_VIEW, 999.0, 0.9)
    assert not decision.accepted and decision.reason == "outside_window"


def test_e36_override_skips_window_and_saturation() -> None:
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "ed_saturation": 0.95})
    overridden = accept(_patient(override="nearest_capable", acuity=3, condition=ConditionCategory.CARDIAC), HOSPITAL, status, EMPTY_VIEW, 999.0, 0.9)
    assert overridden.accepted
    normal = accept(_patient(acuity=3, condition=ConditionCategory.CARDIAC), HOSPITAL, status, EMPTY_VIEW, 5.0, 0.9)
    assert not normal.accepted and normal.reason == "ed_saturated"


def test_check_6_ed_saturated_only_applies_to_acuity_3_and_above() -> None:
    # Triage logic: a critical patient (acuity < 3) is let in despite
    # saturation; a less-critical one (>=3) is the one turned away.
    status = HospitalStatus(**{**OPEN_STATUS.__dict__, "ed_saturation": 0.95})
    critical = accept(_patient(acuity=2), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert critical.accepted
    less_critical = accept(_patient(acuity=3), HOSPITAL, status, EMPTY_VIEW, 0.0, 0.9)
    assert not less_critical.accepted and less_critical.reason == "ed_saturated"


def test_accepted_decision_carries_the_bed_type() -> None:
    decision = accept(_patient(acuity=4), HOSPITAL, OPEN_STATUS, EMPTY_VIEW, 0.0, 0.9)
    assert decision.accepted
    assert decision.bed_type is BedType.GENERAL
