"""Phase 10: ai.py's own contract — validation, zero-bed/unknown-id
filtering, the (transport_id, current_epoch) cache, and the fail-safe
{"error": "AI unavailable"} shape. Every Groq client here is a fake; no test
in this file may reach the network.
"""
from types import SimpleNamespace
from typing import Any, Optional

import dataclasses
import json

from app import ai
from app.events import Event, EventType
from app.seed import HospitalSeed, PatientSeed

_HOSPITALS = (
    HospitalSeed("Hospital_A", beds_available=6, distance_km=4.2, specialities=("trauma",)),
    HospitalSeed("Hospital_B", beds_available=0, distance_km=6.8, specialities=("stroke",)),
    HospitalSeed("Hospital_C", beds_available=9, distance_km=9.5, specialities=("burn",)),
)
_PATIENT = PatientSeed(patient_id="PT-01", acuity=3, condition="chest pain")
_POSITION = ai.AmbulancePosition(progress=0.4, known_destination="Hospital_A")


class FakeCompletions:
    def __init__(self, content: Optional[str] = None, error: Optional[Exception] = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0
        self.last_kwargs: Optional[dict] = None

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: Optional[str] = None, error: Optional[Exception] = None) -> None:
        self.completions = FakeCompletions(content, error)
        self.chat = SimpleNamespace(completions=self.completions)


def _events(transport_id: str = "AMB-1", epoch: int = 1) -> list[Event]:
    return [
        Event(
            transport_id=transport_id, epoch=1, ts_ms=0, type=EventType.TRANSPORT_STARTED,
            facility_id=None, payload={"destination": "Hospital_A"},
        ),
        Event(
            transport_id=transport_id, epoch=epoch, ts_ms=50, type=EventType.CUTOVER_APPLIED,
            facility_id=None, payload={"current_destination": "Hospital_A"},
        ),
    ]


# -- explain() ----------------------------------------------------------------


def test_explain_returns_a_safe_message_and_never_calls_the_client_when_there_are_no_events() -> None:
    client = FakeClient(content="should never be read")
    result = ai.explain([], client=client)
    assert client.completions.calls == 0
    assert "explanation" in result


def test_explain_returns_the_narration_from_the_client() -> None:
    client = FakeClient(content="The ambulance was redirected once.")
    result = ai.explain(_events(), client=client, cache={})
    assert result == {"explanation": "The ambulance was redirected once."}


def test_explain_caches_by_transport_and_current_epoch_so_the_client_is_called_once() -> None:
    client = FakeClient(content="Narration.")
    cache: dict = {}
    first = ai.explain(_events("AMB-1", epoch=1), client=client, cache=cache)
    second = ai.explain(_events("AMB-1", epoch=1), client=client, cache=cache)
    assert first == second
    assert client.completions.calls == 1


def test_explain_does_not_use_a_cache_hit_from_a_different_epoch() -> None:
    client = FakeClient(content="Narration.")
    cache: dict = {}
    ai.explain(_events("AMB-1", epoch=1), client=client, cache=cache)
    ai.explain(_events("AMB-1", epoch=2), client=client, cache=cache)
    assert client.completions.calls == 2


def test_explain_returns_ai_unavailable_when_the_client_raises() -> None:
    client = FakeClient(error=RuntimeError("network down"))
    result = ai.explain(_events(), client=client, cache={})
    assert result == {"error": "AI unavailable"}


def test_explain_returns_ai_unavailable_on_empty_content_and_does_not_cache_the_failure() -> None:
    failing_client = FakeClient(content="")
    cache: dict = {}
    failed = ai.explain(_events(), client=failing_client, cache=cache)
    assert failed == {"error": "AI unavailable"}

    working_client = FakeClient(content="Narration.")
    recovered = ai.explain(_events(), client=working_client, cache=cache)
    assert recovered == {"explanation": "Narration."}
    assert working_client.completions.calls == 1  # the earlier failure was not cached


def test_explain_returns_ai_unavailable_with_no_client_and_no_api_key(monkeypatch) -> None:
    monkeypatch.setattr(ai, "settings", dataclasses.replace(ai.settings, groq_api_key=""))
    result = ai.explain(_events(), cache={})
    assert result == {"error": "AI unavailable"}


# -- recommend() ----------------------------------------------------------------


def test_recommend_returns_the_validated_ranked_list() -> None:
    client = FakeClient(
        content='{"ranked": [{"hospital_id": "Hospital_A", "score": 0.9, "reason": "closest"}]}'
    )
    result = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache={})
    assert result == {"ranked": [{"hospital_id": "Hospital_A", "score": 0.9, "reason": "closest"}]}


def test_recommend_drops_unknown_hospital_ids() -> None:
    client = FakeClient(
        content=(
            '{"ranked": ['
            '{"hospital_id": "Hospital_A", "score": 0.9, "reason": "closest"},'
            '{"hospital_id": "Hospital_Z", "score": 0.5, "reason": "invented"}'
            "]}"
        )
    )
    result = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache={})
    assert [entry["hospital_id"] for entry in result["ranked"]] == ["Hospital_A"]


def test_recommend_drops_zero_bed_hospitals() -> None:
    # Hospital_B has beds_available=0 in _HOSPITALS above.
    client = FakeClient(
        content=(
            '{"ranked": ['
            '{"hospital_id": "Hospital_B", "score": 0.9, "reason": "closest but full"},'
            '{"hospital_id": "Hospital_C", "score": 0.4, "reason": "has beds"}'
            "]}"
        )
    )
    result = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache={})
    assert [entry["hospital_id"] for entry in result["ranked"]] == ["Hospital_C"]


def test_recommend_caches_by_transport_and_current_epoch() -> None:
    client = FakeClient(content='{"ranked": []}')
    cache: dict = {}
    ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache=cache)
    ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache=cache)
    assert client.completions.calls == 1


def test_recommend_does_not_use_a_cache_hit_from_a_different_epoch() -> None:
    client = FakeClient(content='{"ranked": []}')
    cache: dict = {}
    ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache=cache)
    ai.recommend("AMB-1", 2, _HOSPITALS, _PATIENT, _POSITION, client=client, cache=cache)
    assert client.completions.calls == 2


def test_recommend_returns_ai_unavailable_on_malformed_json_and_does_not_cache_the_failure() -> None:
    failing_client = FakeClient(content="not json")
    cache: dict = {}
    failed = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=failing_client, cache=cache)
    assert failed == {"error": "AI unavailable"}

    working_client = FakeClient(content='{"ranked": []}')
    recovered = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=working_client, cache=cache)
    assert recovered == {"ranked": []}
    assert working_client.completions.calls == 1  # the earlier failure was not cached


def test_recommend_returns_ai_unavailable_when_the_client_raises() -> None:
    client = FakeClient(error=TimeoutError("took too long"))
    result = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache={})
    assert result == {"error": "AI unavailable"}


def test_recommend_returns_ai_unavailable_with_no_client_and_no_api_key(monkeypatch) -> None:
    monkeypatch.setattr(ai, "settings", dataclasses.replace(ai.settings, groq_api_key=""))
    result = ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, cache={})
    assert result == {"error": "AI unavailable"}


def test_recommend_uses_the_specified_model_and_json_response_format() -> None:
    client = FakeClient(content='{"ranked": []}')
    ai.recommend("AMB-1", 1, _HOSPITALS, _PATIENT, _POSITION, client=client, cache={})
    assert client.completions.last_kwargs["model"] == ai._RECOMMEND_MODEL
    assert client.completions.last_kwargs["temperature"] == 0
    assert client.completions.last_kwargs["response_format"] == {"type": "json_object"}


def test_explain_uses_the_specified_model_and_temperature() -> None:
    client = FakeClient(content="Narration.")
    ai.explain(_events(), client=client, cache={})
    assert client.completions.last_kwargs["model"] == ai._EXPLAIN_MODEL
    assert client.completions.last_kwargs["temperature"] == 0.2


# -- Phase 23: justify + parse_patient --------------------------------------


def test_justify_explains_a_decision_without_influencing_it(monkeypatch) -> None:
    """It is handed the choice that was already made and the shortlist it came
    from. It never returns a hospital, so it cannot move a patient."""
    client = FakeClient('Riverside won: nearest with a free ICU bed. Eastgate was on diversion.')
    result = ai.justify(
        "T1", "Hospital_1",
        {"acuity": 2, "condition": "CARDIAC", "age_group": "ADULT", "needs": [], "override": "none"},
        [{"hospital_id": "Hospital_1", "score": -2.0, "reason": None},
         {"hospital_id": "Hospital_2", "score": None, "reason": "on_full_diversion"}],
        client=client,
    )
    assert "Riverside" in result["explanation"]
    assert "hospital_id" not in result


def test_justify_needs_no_network_when_there_are_no_candidates() -> None:
    assert "No hospitals" in ai.justify("T1", None, {}, [], client=None)["explanation"]


def test_parse_patient_validates_every_field_against_the_real_enums() -> None:
    """A hallucinated condition must not reach the clinical rules: each field
    falls back to the safe default rather than propagating."""
    client = FakeClient(json.dumps({
        "acuity": 99, "condition": "SPACE_FLU", "age_group": "ROBOT",
        "needs": ["VENTILATOR", "TELEPORTER"], "override": "do_whatever",
    }))
    parsed = ai.parse_patient("something odd", client=client)["patient"]
    assert parsed == {
        "acuity": 3, "condition": "GENERAL", "age_group": "ADULT",
        "needs": ["VENTILATOR"], "override": "none",
    }


def test_parse_patient_keeps_valid_values() -> None:
    client = FakeClient(json.dumps({
        "acuity": 1, "condition": "CARDIAC", "age_group": "CHILD",
        "needs": ["CATH_LAB"], "override": "nearest_capable",
    }))
    parsed = ai.parse_patient("critical child, cardiac, nearest capable", client=client)["patient"]
    assert parsed["acuity"] == 1
    assert parsed["condition"] == "CARDIAC"
    assert parsed["age_group"] == "CHILD"
    assert parsed["override"] == "nearest_capable"


def test_parse_patient_rejects_empty_text() -> None:
    assert "error" in ai.parse_patient("   ", client=None)


def test_both_new_helpers_fall_back_when_the_model_fails() -> None:
    """A rate limit or a timeout must never break the demo."""
    boom = RuntimeError("429 rate limited")
    assert ai.justify(
        "T1", "H1", {}, [{"hospital_id": "H1"}], client=FakeClient(error=boom)
    ) == {"error": "AI unavailable"}
    assert ai.parse_patient("anything", client=FakeClient(error=boom)) == {"error": "AI unavailable"}
