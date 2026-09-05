"""Named network scenarios for the demo and for Phase 4/6 tests. Each Preset
is pure data: a fixed delay table, consumed by Bus.load_preset() in send-call
order (the bus never inspects a message to decide its delay — see bus.py),
a duplicate rate, and a list of redirects for simulation.py to fire at given
simulated timestamps.

Delay values are kept comfortably under 600ms so every preset stays valid
whether D_MAX_MS is the test default (1500) or the demo's faster override
(600, see .env.example / README) — strict_bound would otherwise reject a
preset at load time under the smaller bound.

The exact index-to-message mapping documented below matches the canonical
single-redirect call order (PREPARE, REDIRECT_NOTICE, READY, notice-APPLIED,
ACTIVATE_AT, RECEIVED, WITHDRAW_AT, RECEIVED, activate-APPLIED,
withdraw-APPLIED); it is exercised for real once the dispatcher exists
(Phase 4's test_dispatcher_single/double/before_cutover) and tuned there if
the live call order turns out to differ.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PresetRedirect:
    target_facility: str
    at_ms: int


@dataclass(frozen=True)
class Preset:
    name: str
    delays: tuple[int, ...]
    duplicate_rate: float
    redirects: tuple[PresetRedirect, ...]


NORMAL = Preset(
    name="NORMAL",
    # Every send gets the same comfortably-under-bound delay: nothing here
    # is meant to create an ordering effect, unlike the other six presets —
    # this is the baseline "everything arrives in the order it was sent".
    delays=(150,) * 12,
    duplicate_rate=0.0,
    redirects=(),
)

DELAYED_OLD_FACILITY = Preset(
    name="DELAYED_OLD_FACILITY",
    delays=(
        120,  # 1: PREPARE -> pending facility
        120,  # 2: REDIRECT_NOTICE -> ambulance
        120,  # 3: READY <- pending facility
        120,  # 4: APPLIED (notice) <- ambulance
        120,  # 5: ACTIVATE_AT -> pending facility
        120,  # 6: RECEIVED <- pending facility
        500,  # 7: WITHDRAW_AT -> current (old) facility — slow on purpose
        500,  # 8: RECEIVED <- current (old) facility — slow on purpose
        120,  # 9: APPLIED (activate) <- pending facility, right at cutover
        500,  # 10: APPLIED (withdraw) <- old facility, well after cutover
    ),
    duplicate_rate=0.0,
    redirects=(),
)

OUT_OF_ORDER_ACK = Preset(
    name="OUT_OF_ORDER_ACK",
    delays=(
        # First redirect A -> B (epoch 2): fast and unremarkable...
        100, 100, 100, 100, 100, 100, 100, 100,
        450,  # ...except B's activate-APPLIED(v2), which is held back...
        100,  # ...while the withdraw-APPLIED for the abandoned facility is fast.
        # Second redirect B -> C (epoch 3), fired later by the scenario:
        # fast enough that C's READY(v3) reaches the dispatcher first —
        # B's APPLIED(v2) above is still in flight and arrives after it,
        # by which point it is fenced as stale (R2).
        50, 50, 50, 50, 50, 50, 50, 50,
    ),
    duplicate_rate=0.0,
    redirects=(),
)

REDIRECT_BEFORE_CUTOVER = Preset(
    name="REDIRECT_BEFORE_CUTOVER",
    delays=(150,) * 12,
    duplicate_rate=0.0,
    # Fired early enough that the first redirect's cutover has not happened
    # yet and withdraw_sent is still false — R6's cancellable path (E8).
    redirects=(PresetRedirect(target_facility="Hospital_C", at_ms=400),),
)

LATE_REDIRECT_QUEUED = Preset(
    name="LATE_REDIRECT_QUEUED",
    delays=(150,) * 12,
    duplicate_rate=0.0,
    # Fired late enough that withdraw_sent is already true and the cutover
    # is too close to cancel — R6's not-cancellable path, so this becomes
    # RedirectQueued instead (E24).
    redirects=(PresetRedirect(target_facility="Hospital_C", at_ms=3000),),
)

DUPLICATE_PACKET = Preset(
    name="DUPLICATE_PACKET",
    delays=(150,) * 12,
    duplicate_rate=1.0,  # always duplicate, so the demo reliably shows a DuplicateIgnored
    redirects=(),
)

CHAOS = Preset(
    name="CHAOS",
    # A deliberately uneven mix: some near-instant, some near the bound.
    delays=(50, 400, 90, 350, 60, 500, 40, 300, 80, 450, 55, 380),
    duplicate_rate=0.25,
    redirects=(
        PresetRedirect(target_facility="Hospital_B", at_ms=300),
        PresetRedirect(target_facility="Hospital_C", at_ms=1800),
    ),
)

ALL_PRESETS: dict[str, Preset] = {
    preset.name: preset
    for preset in (
        NORMAL,
        DELAYED_OLD_FACILITY,
        OUT_OF_ORDER_ACK,
        REDIRECT_BEFORE_CUTOVER,
        LATE_REDIRECT_QUEUED,
        DUPLICATE_PACKET,
        CHAOS,
    )
}
