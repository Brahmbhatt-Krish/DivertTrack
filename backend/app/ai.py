"""AI sidecar (Groq). Read-only: explain() narrates the event log for a
non-technical audience, recommend() ranks hospitals for a redirect decision.
Neither has a write path — the UI's "Apply" on a recommendation goes through
the ordinary /transports/{id}/redirect route, never through here.

Fail-safe boundary: this module talks to an external network service, so
every failure mode (no API key, network error, timeout, malformed model
output) is caught and collapses to the same {"error": "AI unavailable"}
shape rather than raising — the rest of the system must keep working with
GROQ_API_KEY empty. That is why the try/except blocks below are broad; that
breadth is the point here, not an oversight.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from groq import Groq

from app.config import settings
from app.events import Event
from app.projection import project
from app.seed import HospitalSeed, PatientSeed

_EXPLAIN_MODEL = "allam-2-7b"
_RECOMMEND_MODEL = "allam-2-7b"
_TIMEOUT_S = 6.0
_MAX_EVENTS = 40
_EXPLAIN_MAX_TOKENS = 400
_RECOMMEND_MAX_TOKENS = 500

_UNAVAILABLE: dict = {"error": "AI unavailable"}

_EXPLAIN_SYSTEM_PROMPT = (
    "You narrate an ambulance-diversion event log for a non-technical audience "
    "(a patient's family, a hospital administrator, a jury). Write 4 to 6 "
    "sentences, past tense. Mention every redirect and every discarded or "
    "ignored message and why it was discarded. End by stating how many "
    "facilities were actively receiving the patient at any given moment. "
    "Never invent an event that is not in the log given to you."
)

_RECOMMEND_SYSTEM_PROMPT = (
    "You rank candidate hospitals for an ambulance redirect given the "
    "hospitals, the patient, and the ambulance's current position. Respond "
    "with JSON only, no prose: "
    '{"ranked": [{"hospital_id": string, "score": number between 0 and 1, '
    '"reason": string}, ...]}, most suitable first. Only use hospital_id '
    "values from the hospitals you were given."
)


_JUSTIFY_MODEL = "allam-2-7b"
_PARSE_MODEL = "allam-2-7b"
_JUSTIFY_MAX_TOKENS = 300
_PARSE_MAX_TOKENS = 300

_JUSTIFY_SYSTEM_PROMPT = (
    "You explain, to an ambulance dispatcher, why one hospital was chosen for "
    "a patient over the others. You are given the chosen hospital, the "
    "patient, and the full ranked shortlist: accepted hospitals have a score "
    "(higher is better; it trades travel time against how full the hospital "
    "is) and rejected ones have a reason code instead. Write 2 to 4 plain "
    "sentences. Say why the chosen one won, and name the most significant "
    "hospitals that were ruled out and what ruled them out, translating the "
    "reason codes into plain English (no_bed:ICU means it had no free "
    "intensive-care bed; no_capability:CATH_LAB means it has no "
    "catheterisation lab; outside_window means it was too far for this "
    "condition; on_full_diversion means it was not accepting patients at "
    "all). Never invent a hospital or a reason that is not in the data given "
    "to you."
)

_PARSE_SYSTEM_PROMPT = (
    "You turn a dispatcher's free-text description of a patient into JSON. "
    "Respond with JSON only, no prose: "
    '{"acuity": 1-5, "condition": string, "age_group": string, '
    '"needs": [string], "override": "none"|"nearest_capable"}. '
    "acuity is a triage score where 1 is critical and 5 is minor. "
    "condition is one of CARDIAC, TRAUMA, STROKE, BURN, RESPIRATORY, "
    "OBSTETRIC, PAEDIATRIC, PSYCHIATRIC, GENERAL. "
    "age_group is one of NEONATE, CHILD, ADULT. "
    "needs is any of VENTILATOR, ISOLATION, CATH_LAB, CT_SCAN, BARIATRIC, "
    "BLOOD_PRODUCTS, and is usually empty. "
    "Use override \"nearest_capable\" only if the text says to take them to "
    "the nearest possible hospital regardless of anything else. "
    "If the text does not say, default to acuity 3, GENERAL, ADULT, no needs, "
    "override none. Use only the exact values listed above."
)


@dataclass(frozen=True)
class AmbulancePosition:
    """What recommend() knows about where the ambulance is right now —
    Simulation.ambulance_view()'s shape, typed for this module boundary."""

    progress: float
    known_destination: Optional[str]


def _default_client() -> Optional[Groq]:
    # No key configured is treated the same as "the service is down": Phase
    # 10's spec is explicit that AI is a sidecar the rest of the system
    # never depends on, and the demo must run with GROQ_API_KEY empty.
    if not settings.groq_api_key:
        return None
    return Groq(api_key=settings.groq_api_key, timeout=_TIMEOUT_S)


def _compact_event(event: Event) -> dict:
    return {
        "seq": event.seq,
        "epoch": event.epoch,
        "ts_ms": event.ts_ms,
        "type": event.type.value,
        "facility_id": event.facility_id,
        "payload": event.payload,
    }


_explain_cache: dict[tuple[str, int], dict] = {}


def explain(events: list[Event], client: Optional[Groq] = None, cache: Optional[dict] = None) -> dict:
    """`events` is expected to already be one transport's log, in seq order
    (main.py passes EventStore.replay(transport_id)) — current_epoch for the
    cache key is derived from it via the same pure project() the checker and
    UI use, rather than adding a parameter every caller has to supply."""
    if not events:
        return {"explanation": "No activity has been recorded for this transport yet."}

    cache = _explain_cache if cache is None else cache
    transport_id = events[-1].transport_id
    current_epoch = project(events).transports[transport_id].current_epoch
    key = (transport_id, current_epoch)
    if key in cache:
        return cache[key]

    resolved_client = client if client is not None else _default_client()
    if resolved_client is None:
        return dict(_UNAVAILABLE)

    payload = json.dumps([_compact_event(e) for e in events[-_MAX_EVENTS:]], separators=(",", ":"))
    try:
        completion = resolved_client.chat.completions.create(
            model=_EXPLAIN_MODEL,
            temperature=0.2,
            max_completion_tokens=_EXPLAIN_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        text = completion.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"expected non-empty text content, received {text!r}")
    except Exception:
        return dict(_UNAVAILABLE)

    result = {"explanation": text.strip()}
    cache[key] = result
    return result


_recommend_cache: dict[tuple[str, int], dict] = {}


def _validate_ranked(
    raw: Any, hospitals: Sequence[HospitalSeed], free_beds: Optional[dict[str, int]] = None
) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("ranked"), list):
        raise ValueError(f"expected a JSON object with a 'ranked' list, received {raw!r}")

    # beds_available on HospitalSeed is a *static literal* from seed.py that
    # the simulation never updates, so validating against it rejected picks at
    # hospitals that really did have beds and accepted picks at hospitals that
    # did not. The caller passes live counts instead; the seed value is only a
    # fallback for the plain demo, which has no bed model at all.
    beds_by_id = {
        h.facility_id: (
            free_beds.get(h.facility_id, h.beds_available) if free_beds is not None else h.beds_available
        )
        for h in hospitals
    }
    validated: list[dict] = []
    for entry in raw["ranked"]:
        if not isinstance(entry, dict):
            continue
        hospital_id, score, reason = entry.get("hospital_id"), entry.get("score"), entry.get("reason")
        if hospital_id not in beds_by_id:
            continue  # unknown id — the model invented or mistyped one
        if beds_by_id[hospital_id] <= 0:
            continue  # zero-bed picks are never actionable
        if not isinstance(score, (int, float)) or not isinstance(reason, str):
            continue
        validated.append({"hospital_id": hospital_id, "score": float(score), "reason": reason})
    return validated


def recommend(
    transport_id: str,
    current_epoch: int,
    hospitals: Sequence[HospitalSeed],
    patient: PatientSeed,
    position: AmbulancePosition,
    client: Optional[Groq] = None,
    cache: Optional[dict] = None,
    free_beds: Optional[dict[str, int]] = None,
) -> dict:
    cache = _recommend_cache if cache is None else cache
    key = (transport_id, current_epoch)
    if key in cache:
        return cache[key]

    resolved_client = client if client is not None else _default_client()
    if resolved_client is None:
        return dict(_UNAVAILABLE)

    payload = json.dumps(
        {
            "hospitals": [dataclasses.asdict(h) for h in hospitals],
            "patient": dataclasses.asdict(patient),
            "position": dataclasses.asdict(position),
        },
        separators=(",", ":"),
    )
    try:
        completion = resolved_client.chat.completions.create(
            model=_RECOMMEND_MODEL,
            temperature=0,
            max_completion_tokens=_RECOMMEND_MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _RECOMMEND_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        text = completion.choices[0].message.content
        data = json.loads(text)
        ranked = _validate_ranked(data, hospitals, free_beds)
    except Exception:
        return dict(_UNAVAILABLE)

    result = {"ranked": ranked}
    cache[key] = result
    return result


# -- 1. why was this hospital chosen? ---------------------------------------


def justify(
    transport_id: str,
    chosen: Optional[str],
    patient: dict,
    candidates: Sequence[dict],
    client: Optional[Groq] = None,
) -> dict:
    """Narrate one placement decision from the ranking that produced it.

    Purely explanatory: it is handed the decision that was already made and
    the shortlist it came from, and never gets to influence either. The
    ranking is deterministic and reproducible; this only puts it in English.
    """
    if not candidates:
        return {"explanation": "No hospitals were evaluated for this transport yet."}

    resolved_client = client if client is not None else _default_client()
    if resolved_client is None:
        return dict(_UNAVAILABLE)

    payload = json.dumps(
        {"chosen": chosen, "patient": patient, "shortlist": list(candidates)},
        separators=(",", ":"),
    )
    try:
        completion = resolved_client.chat.completions.create(
            model=_JUSTIFY_MODEL,
            temperature=0,
            max_completion_tokens=_JUSTIFY_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _JUSTIFY_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        text = completion.choices[0].message.content
    except Exception:
        return dict(_UNAVAILABLE)

    if not isinstance(text, str) or not text.strip():
        return dict(_UNAVAILABLE)
    return {"explanation": text.strip()}


# -- 2. free text -> a patient ----------------------------------------------

_CONDITIONS = {
    "CARDIAC", "TRAUMA", "STROKE", "BURN", "RESPIRATORY",
    "OBSTETRIC", "PAEDIATRIC", "PSYCHIATRIC", "GENERAL",
}
_AGE_GROUPS = {"NEONATE", "CHILD", "ADULT"}
_NEEDS = {"VENTILATOR", "ISOLATION", "CATH_LAB", "CT_SCAN", "BARIATRIC", "BLOOD_PRODUCTS"}


def parse_patient(text: str, client: Optional[Groq] = None) -> dict:
    """Turn a dispatcher's description into a Patient.

    The model is used as a *parser*, never as a decision-maker: it produces a
    patient, and the deterministic acceptance function decides where that
    patient can go. Every field is validated against the real enums and a bad
    value falls back to the safe default rather than propagating — a
    hallucinated condition must not reach the clinical rules.
    """
    if not text or not text.strip():
        return {"error": "Describe the patient first."}

    resolved_client = client if client is not None else _default_client()
    if resolved_client is None:
        return dict(_UNAVAILABLE)

    try:
        completion = resolved_client.chat.completions.create(
            model=_PARSE_MODEL,
            temperature=0,
            max_completion_tokens=_PARSE_MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": text.strip()},
            ],
        )
        raw = json.loads(completion.choices[0].message.content)
    except Exception:
        return dict(_UNAVAILABLE)

    if not isinstance(raw, dict):
        return dict(_UNAVAILABLE)

    acuity = raw.get("acuity")
    if not isinstance(acuity, int) or not 1 <= acuity <= 5:
        acuity = 3
    condition = raw.get("condition")
    if condition not in _CONDITIONS:
        condition = "GENERAL"
    age_group = raw.get("age_group")
    if age_group not in _AGE_GROUPS:
        age_group = "ADULT"
    needs = raw.get("needs")
    needs = sorted(n for n in needs if n in _NEEDS) if isinstance(needs, list) else []
    override = raw.get("override")
    if override not in ("none", "nearest_capable"):
        override = "none"

    return {
        "patient": {
            "acuity": acuity, "condition": condition, "age_group": age_group,
            "needs": needs, "override": override,
        }
    }
