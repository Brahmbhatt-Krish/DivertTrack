"""Application configuration, loaded once from the project-root .env file.

Every other module receives its timing/config values by importing `settings`
from here (or, in tests, by constructing a `Config` with explicit overrides) —
nothing reads `os.environ` anywhere else in the codebase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.models import Policy

# backend/app/config.py -> backend/app -> backend -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Expected an integer for env var {name!r}, received {raw!r}"
        ) from exc


def _read_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"Expected a float for env var {name!r}, received {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Config:
    """Simulation timing and connection settings.

    All durations are in simulated milliseconds. `D_MAX_MS` is the contract
    the bus enforces (see bus.py strict_bound); the others tune how fast a
    facility prepares, how long the dispatcher waits for READY, and how much
    margin (GUARD_MS) is added on top of the delay bound at every cutover.
    """

    groq_api_key: str
    d_max_ms: int
    guard_ms: int
    ready_timeout_ms: int
    prep_ms: int
    tick_ms: int
    db_path: str
    # -- Phase 12 (multi-hospital capacity extension) --------------------
    # Defaulted, unlike the fields above: every pre-existing direct
    # Config(...) construction (tests, conftest.py's make_harness) predates
    # this extension and shouldn't have to name these to keep working.
    speed_km_per_min: float = 1.0
    load_weight: float = 10.0
    saturation_limit: float = 0.9
    policy: Policy = Policy.MANUAL
    # How many simulated minutes of driving pass per real minute. Movement
    # only: eta_minutes() (and therefore acceptance check 5, the
    # transport-time window) keeps using the true speed_km_per_min above.
    #
    # They have to be separate. speed_km_per_min is a real clinical figure —
    # 1 km/min is ~60 km/h — and at that speed a 10 km transport takes ten
    # real minutes, so nobody watching the demo ever sees an ambulance
    # arrive. Raising the speed instead would shrink every ETA toward zero
    # and quietly make "outside_window" unreachable, turning a real triage
    # rule into a no-op. Compressing time leaves the model honest.
    #
    # Defaults to 1.0 here (tests construct Config(...) directly and must
    # stay deterministic); from_env picks the demo-friendly value.
    sim_time_scale: float = 1.0
    # A floor on how long one leg takes, however short the trip. The
    # dispatcher deliberately picks the *nearest* accepting hospital, so most
    # journeys are short, and a patient can be generated right on top of one
    # — those arrived almost instantly, leaving no window to demonstrate a
    # redirect in flight. Modelling it as a floor (rather than by slowing
    # everything down) keeps long transports proportionally longer.
    #
    # 0.0 here so tests keep their exact existing tick counts; from_env picks
    # the demo value.
    min_travel_ms: float = 0.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            d_max_ms=_read_int("D_MAX_MS", 1500),
            guard_ms=_read_int("GUARD_MS", 300),
            ready_timeout_ms=_read_int("READY_TIMEOUT_MS", 4000),
            prep_ms=_read_int("PREP_MS", 200),
            tick_ms=_read_int("TICK_MS", 500),
            db_path=os.environ.get("DB_PATH", "diverttrack.db"),
            speed_km_per_min=_read_float("SPEED_KM_PER_MIN", 1.0),
            load_weight=_read_float("LOAD_WEIGHT", 10.0),
            saturation_limit=_read_float("SATURATION_LIMIT", 0.9),
            policy=Policy(os.environ.get("POLICY", "manual")),
            # 60x: a typical 10-15 km transport lands in ~10-15 seconds.
            # 45x + a 12s floor: short hops take ~12s, a 10 km transport
            # ~13s, a cross-region one ~30s — enough to redirect in flight.
            sim_time_scale=_read_float("SIM_TIME_SCALE", 45.0),
            min_travel_ms=_read_float("MIN_TRAVEL_MS", 12000.0),
        )


settings = Config.from_env()
