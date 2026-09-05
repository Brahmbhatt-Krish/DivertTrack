"""Phase 19: M1 (2D position movement, arrival at <=0.1km) and M2 (eta_minutes
uses current position, not origin)."""
from dataclasses import replace

import pytest
from app.ambulance import Ambulance
from app.clock import FakeClock
from app.config import Config
from app.events import EventStore, EventType
from app.messages import ActionType, Command
from app.models import ConditionCategory, Hospital, BedType, max_eta_for
from app.scoring import eta_minutes


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> None:
        self.sent.append(message)


def _config() -> Config:
    return Config(groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=500, prep_ms=50, tick_ms=100, db_path=":memory:", speed_km_per_min=1.0)


def _hospital(location) -> Hospital:
    return Hospital(id="H1", name="H1", location=location, beds_total={BedType.GENERAL: 5}, capabilities=frozenset(), ventilators_total=0)


def _notice(target: str, epoch: int = 1) -> Command:
    return Command(command_id=f"cmd-{target}-{epoch}", transport_id="T1", target_facility=target, epoch=epoch, action=ActionType.REDIRECT_NOTICE, sent_at_ms=0)


def test_m1_ambulance_moves_toward_destination_each_tick() -> None:
    clock = FakeClock()
    store = EventStore(":memory:")
    hospital = _hospital((10.0, 0.0))
    ambulance = Ambulance("T1", clock, store, RecordingBus(), _config(), hospitals={"H1": hospital}, position=(0.0, 0.0))
    ambulance.receive_command(_notice("H1"))
    clock.advance(100)  # one tick: speed 1.0 km/min * 100ms/60000 = ~0.00167 km -- tiny but nonzero
    assert ambulance.position[0] > 0.0
    assert ambulance.position[0] < 10.0


def test_m1_arrival_within_0_1km_logs_arrived() -> None:
    clock = FakeClock()
    store = EventStore(":memory:")
    hospital = _hospital((0.05, 0.0))  # already within the 0.1km arrival threshold
    ambulance = Ambulance("T1", clock, store, RecordingBus(), _config(), hospitals={"H1": hospital}, position=(0.0, 0.0))
    ambulance.receive_command(_notice("H1"))
    clock.advance(200)
    arrived = [e for e in store.replay("T1") if e.type is EventType.ARRIVED]
    assert arrived and arrived[0].payload["at"] == "H1"
    assert ambulance.progress == 1.0


def test_m1_redirect_mid_journey_resets_toward_new_destination() -> None:
    clock = FakeClock()
    store = EventStore(":memory:")
    hospitals = {"H1": _hospital((20.0, 0.0)), "H2": _hospital((5.0, 0.0))}
    ambulance = Ambulance("T1", clock, store, RecordingBus(), _config(), hospitals=hospitals, position=(0.0, 0.0))
    ambulance.receive_command(_notice("H1"))
    clock.advance(500)
    midway_position = ambulance.position
    ambulance.receive_command(_notice("H2", epoch=2))
    assert ambulance.known_destination == "H2"
    assert ambulance.progress == 0.0
    assert ambulance.position == midway_position  # position itself doesn't teleport, only the target/progress reset


def test_m2_on_arrived_callback_fires_with_transport_and_hospital_id() -> None:
    clock = FakeClock()
    store = EventStore(":memory:")
    hospital = _hospital((0.05, 0.0))
    calls = []
    ambulance = Ambulance(
        "T1", clock, store, RecordingBus(), _config(), hospitals={"H1": hospital}, position=(0.0, 0.0),
        on_arrived=lambda tid, hid: calls.append((tid, hid)),
    )
    ambulance.receive_command(_notice("H1"))
    clock.advance(200)
    assert calls == [("T1", "H1")]


def test_m2_eta_minutes_reflects_current_not_origin_position() -> None:
    hospital = _hospital((10.0, 0.0))
    far = eta_minutes((0.0, 0.0), hospital, speed_km_per_min=1.0)
    near = eta_minutes((9.0, 0.0), hospital, speed_km_per_min=1.0)
    assert near < far


def test_sim_time_scale_speeds_up_the_drive_without_touching_the_eta() -> None:
    """The demo needs an ambulance to arrive in seconds, not the ten real
    minutes a 10 km trip takes at a true 1 km/min.

    The tempting fix — raise speed_km_per_min — would also shrink every
    eta_minutes() toward zero, and eta feeds acceptance check 5, so
    "outside_window" would silently stop rejecting anything. sim_time_scale
    compresses wall-clock for movement only; this pins that separation down.
    """
    hospital = Hospital(
        id="H", name="H", location=(10.0, 0.0), beds_total={BedType.GENERAL: 1},
        capabilities=frozenset(), ventilators_total=0,
    )
    slow = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=500, db_path=":memory:", speed_km_per_min=1.0, sim_time_scale=1.0,
    )
    fast = replace(slow, sim_time_scale=60.0)

    def ticks_to_arrive(config: Config) -> int:
        clock, store = FakeClock(), EventStore(":memory:")
        ambulance = Ambulance(
            "T1", clock, store, RecordingBus(), config,
            hospitals={"H": hospital}, position=(0.0, 0.0),
        )
        ambulance.receive_command(
            Command(
                command_id="c1", transport_id="T1", target_facility="H", epoch=1,
                action=ActionType.REDIRECT_NOTICE, sent_at_ms=0, notice_seq=1,
            )
        )
        clock.run_until_quiet()
        assert [e for e in store.replay("T1") if e.type is EventType.ARRIVED], "never arrived"
        return clock.now_ms()

    slow_ms, fast_ms = ticks_to_arrive(slow), ticks_to_arrive(fast)
    # 60x the distance per tick, so ~1/60th the simulated time to cover it.
    assert fast_ms * 30 < slow_ms, (fast_ms, slow_ms)

    # ...and the ETA the dispatcher scores on is identical either way: it is
    # a function of the real speed, which sim_time_scale must never touch.
    assert slow.speed_km_per_min == fast.speed_km_per_min
    assert eta_minutes((0.0, 0.0), hospital, fast.speed_km_per_min) == pytest.approx(10.0)

    # And the window it feeds still rejects: 40 km is 40 minutes at the real
    # speed, past the 30-minute cardiac limit, however fast the demo runs.
    far = replace(hospital, location=(40.0, 0.0))
    assert eta_minutes((0.0, 0.0), far, fast.speed_km_per_min) > max_eta_for(
        far, ConditionCategory.CARDIAC
    )


def test_min_travel_ms_gives_a_leg_a_floor_even_at_zero_distance() -> None:
    """The dispatcher picks the nearest accepting hospital, so most journeys
    are short — and a patient can sit on a hospital's own coordinates, which
    arrived on the first tick. That left no window to demonstrate a redirect
    in flight. A leg now always takes at least min_travel_ms.
    """
    hospital = _hospital((10.0, 0.0))
    base = Config(
        groq_api_key="", d_max_ms=200, guard_ms=50, ready_timeout_ms=1500, prep_ms=50,
        tick_ms=100, db_path=":memory:", speed_km_per_min=1.0, sim_time_scale=60.0,
    )

    def arrival_ms(config: Config, position: tuple[float, float]) -> int:
        clock, store = FakeClock(), EventStore(":memory:")
        ambulance = Ambulance(
            "T1", clock, store, RecordingBus(), config,
            hospitals={"H1": hospital}, position=position,
        )
        ambulance.receive_command(
            Command(
                command_id="c1", transport_id="T1", target_facility="H1", epoch=1,
                action=ActionType.REDIRECT_NOTICE, sent_at_ms=0, notice_seq=1,
            )
        )
        clock.run_until_quiet()
        arrived = [e for e in store.replay("T1") if e.type is EventType.ARRIVED]
        assert arrived, "never arrived"
        return arrived[0].ts_ms

    # Standing on the hospital: instant without a floor, floored with one.
    assert arrival_ms(base, (10.0, 0.0)) < 1000
    floored = replace(base, min_travel_ms=5000.0)
    assert arrival_ms(floored, (10.0, 0.0)) >= 5000

    # A long transport already takes longer than the floor, so it is
    # unaffected — the floor may only ever slow a leg down, never speed it up.
    assert arrival_ms(floored, (0.0, 0.0)) == arrival_ms(base, (0.0, 0.0))
