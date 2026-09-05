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


def test_a_multi_preset_can_be_run_more_than_once() -> None:
    """Preset transport ids were derived from the preset name and patient
    index alone, so a second click reused the first run's ids and the
    dispatcher rejected them as already-started — every repeat click 500'd."""
    from app.clock import FakeClock
    from app.config import Config
    from app.events import EventStore
    from app.seed import MULTI_HOSPITALS
    from app.simulation import Simulation

    clock, store = FakeClock(), EventStore(":memory:")
    config = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:",
    )
    sim = Simulation(clock, store, config, hospitals={h.id: h for h in MULTI_HOSPITALS})

    first = sim.run_multi_preset("MULTI_NORMAL")
    clock.run_until_quiet()
    second = sim.run_multi_preset("MULTI_NORMAL")
    clock.run_until_quiet()

    assert first and second
    assert not set(first) & set(second), "a repeat run must not reuse transport ids"
    # Run 1 keeps the original id shape, so nothing that asserts on it breaks.
    assert first[0].startswith("MULTI_NORMAL-")
    assert "-r2-" in second[0]
