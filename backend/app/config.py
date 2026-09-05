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
        )


settings = Config.from_env()
