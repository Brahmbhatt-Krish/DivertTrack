"""Phase 19: each of the five new multi-hospital presets runs to quiet with
I1-I4 holding."""
import pytest

from app.checker import check
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore
from app.presets import ALL_MULTI_PRESETS
from app.seed import MULTI_HOSPITALS
from app.simulation import Simulation

HOSPITALS_BY_ID = {h.id: h for h in MULTI_HOSPITALS}


def _config() -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=800, prep_ms=50, tick_ms=100, db_path=":memory:")


@pytest.mark.parametrize("name", list(ALL_MULTI_PRESETS))
def test_preset_runs_to_quiet_with_invariant_holding(name: str) -> None:
    clock = FakeClock()
    store = EventStore(":memory:")
    sim = Simulation(clock, store, _config(), hospitals=HOSPITALS_BY_ID)
    sim.run_multi_preset(name)
    clock.run_until_quiet()

    patients = sim.dispatcher.known_patients()
    result = check(store.replay(), patients=patients, hospitals=HOSPITALS_BY_ID)
    assert not any(v.kind.value == "ZERO_ACTIVE" for v in result.violations), result.violations
    assert not any(v.kind.value == "MULTI_ACTIVE" for v in result.violations), result.violations
    assert not any(v.kind.value == "OVERBOOKED" for v in result.violations), result.violations
