"""AI sidecar (Groq). Stubbed until Phase 10 — both functions return the
same "unavailable" shape Phase 10's real implementation falls back to on
any error, since right now that's simply true. main.py's /ai/* routes
already exist against this interface; Phase 10 replaces the bodies only.
"""
from __future__ import annotations

from typing import Any


def explain(events: list[Any]) -> dict:
    return {"error": "AI unavailable"}


def recommend(hospitals: list[Any], patient: dict, position: dict) -> dict:
    return {"error": "AI unavailable"}
