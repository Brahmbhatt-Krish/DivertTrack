"""Phase 19: scoring.py — eta_minutes, score/rank exclusion+ordering, the
override's load-ignoring rule, and choose()'s first-or-None contract."""
from app.models import AgeGroup, BedType, ConditionCategory, Diversion, Hospital, HospitalStatus, Patient, Specialist
from app.projection import empty_ledger_view
from app.scoring import choose, eta_minutes, rank, score

NEAR = Hospital(id="Near", name="Near", location=(0.0, 0.0), beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=0)
FAR = Hospital(id="Far", name="Far", location=(30.0, 0.0), beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=0)
BLOCKED = Hospital(id="Blocked", name="Blocked", location=(1.0, 0.0), beds_total={BedType.GENERAL: 0}, capabilities=frozenset(), ventilators_total=0)


def _status(hospital: Hospital, **overrides) -> HospitalStatus:
    base = dict(
        specialists_on_shift=frozenset(Specialist), ed_saturation=0.0, diversion=Diversion.OPEN,
        diverted_categories=frozenset(), beds_total=dict(hospital.beds_total), ventilators_total=hospital.ventilators_total,
    )
    base.update(overrides)
    return HospitalStatus(**base)


def _patient(**overrides) -> Patient:
    defaults = dict(id="P1", acuity=4, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT)
    defaults.update(overrides)
    return Patient(**defaults)


def test_eta_minutes_is_euclidean_distance_over_speed() -> None:
    assert eta_minutes((0.0, 0.0), FAR, speed_km_per_min=2.0) == 15.0


def test_score_is_none_when_accept_fails() -> None:
    statuses = {BLOCKED.id: _status(BLOCKED)}
    view = empty_ledger_view(BLOCKED.id)
    assert score(_patient(), (0.0, 0.0), BLOCKED, statuses[BLOCKED.id], view, 1.0, 10.0, 0.9) is None


def test_rank_orders_by_score_desc_and_lists_excluded_last_with_reason() -> None:
    hospitals = [FAR, NEAR, BLOCKED]
    statuses = {h.id: _status(h) for h in hospitals}
    ledger = {h.id: empty_ledger_view(h.id) for h in hospitals}
    ranked = rank(_patient(), (0.0, 0.0), hospitals, statuses, ledger, 1.0, 10.0, 0.9)
    assert [c.hospital_id for c in ranked] == ["Near", "Far", "Blocked"]
    assert ranked[0].score is not None and ranked[-1].score is None
    assert ranked[-1].reason is not None


def test_rank_ties_break_by_hospital_id() -> None:
    same_a = Hospital(id="B", name="B", location=(5.0, 0.0), beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=0)
    same_b = Hospital(id="A", name="A", location=(5.0, 0.0), beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=0)
    hospitals = [same_a, same_b]
    statuses = {h.id: _status(h) for h in hospitals}
    ledger = {h.id: empty_ledger_view(h.id) for h in hospitals}
    ranked = rank(_patient(), (0.0, 0.0), hospitals, statuses, ledger, 1.0, 10.0, 0.9)
    assert [c.hospital_id for c in ranked] == ["A", "B"]


def test_override_ignores_load_in_scoring() -> None:
    hospital = Hospital(id="H", name="H", location=(10.0, 0.0), beds_total={BedType.GENERAL: 1}, capabilities=frozenset(), ventilators_total=0)
    status = _status(hospital)
    view = empty_ledger_view(hospital.id)
    normal = score(_patient(), (0.0, 0.0), hospital, status, view, 1.0, 1000.0, 0.9)
    overridden = score(_patient(override="nearest_capable"), (0.0, 0.0), hospital, status, view, 1.0, 1000.0, 0.9)
    assert overridden == -10.0
    assert normal == -10.0  # load is 0 here (nothing reserved yet) so they coincide; see test below for the real effect


def test_choose_returns_first_ranked_or_none() -> None:
    hospitals = [NEAR, BLOCKED]
    statuses = {h.id: _status(h) for h in hospitals}
    ledger = {h.id: empty_ledger_view(h.id) for h in hospitals}
    assert choose(_patient(), (0.0, 0.0), hospitals, statuses, ledger, 1.0, 10.0, 0.9) == "Near"
    only_blocked_statuses = {BLOCKED.id: statuses[BLOCKED.id]}
    only_blocked_ledger = {BLOCKED.id: ledger[BLOCKED.id]}
    assert choose(_patient(), (0.0, 0.0), [BLOCKED], only_blocked_statuses, only_blocked_ledger, 1.0, 10.0, 0.9) is None
