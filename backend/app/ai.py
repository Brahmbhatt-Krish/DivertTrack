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


def _validate_ranked(raw: Any, hospitals: Sequence[HospitalSeed]) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("ranked"), list):
        raise ValueError(f"expected a JSON object with a 'ranked' list, received {raw!r}")

    beds_by_id = {h.facility_id: h.beds_available for h in hospitals}
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
        ranked = _validate_ranked(data, hospitals)
    except Exception:
        return dict(_UNAVAILABLE)

    result = {"ranked": ranked}
    cache[key] = result
    return result
