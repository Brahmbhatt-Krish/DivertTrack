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

from dataclasses import dataclass, field
from typing import Optional

from app.models import AgeGroup, BedType, ConditionCategory, Patient, Policy


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
        # First redirect A -> B (epoch 2): fast through PREPARE/NOTICE/
        # READY/ACTIVATE_AT/RECEIVED/WITHDRAW_AT...
        100, 100, 100, 100, 100, 100, 100,
        # ...then every late-stage epoch-2 ack (A's WITHDRAW_AT-RECEIVED,
        # A's WITHDRAW-APPLIED, B's ACTIVATE_AT-APPLIED) held back as a
        # block. Their exact relative order isn't fully pinned down by the
        # spec and can shift slightly under a real (non-simulated) clock —
        # delaying the whole block, not just one guessed position, is what
        # actually guarantees B's activate-APPLIED(v2) is still in flight
        # when the second redirect starts, regardless of which of the
        # three lands where.
        500, 500, 500,
        # Second redirect B -> C (epoch 3), fired later by the scenario:
        # fast enough that C's READY(v3) reaches the dispatcher well before
        # the held-back block above finally arrives, so B's APPLIED(v2) is
        # fenced as stale (R2) once it does.
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

# -- Phase 17: multi-hospital presets ---------------------------------------
# Each is a scripted scenario (patients + positions, plus timed hospital
# status/bed drops) run against the six-hospital seed via
# Simulation.run_multi_preset() — distinct from the plain Preset above
# (delay tables for the single AMB-101 demo transport), which these leave
# entirely unchanged.


@dataclass(frozen=True)
class MultiPresetTransport:
    patient: Patient
    position: tuple[float, float]
    target: Optional[str] = None  # explicit target, for a manual-policy scenario like LAST_BED_RACE


@dataclass(frozen=True)
class StatusOverride:
    at_ms: int
    hospital_id: str
    changes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BedOverride:
    at_ms: int
    hospital_id: str
    bed_type: BedType
    total: int


@dataclass(frozen=True)
class MultiPreset:
    name: str
    policy: Policy
    transports: tuple[MultiPresetTransport, ...]
    status_overrides: tuple[StatusOverride, ...] = ()
    bed_overrides: tuple[BedOverride, ...] = ()


MULTI_NORMAL = MultiPreset(
    name="MULTI_NORMAL",
    policy=Policy.MANUAL,
    transports=(
        MultiPresetTransport(Patient(id="P1", acuity=3, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT), (5.0, 5.0)),
        MultiPresetTransport(Patient(id="P2", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT), (10.0, 10.0)),
        MultiPresetTransport(Patient(id="P3", acuity=2, condition=ConditionCategory.CARDIAC, age_group=AgeGroup.ADULT), (15.0, 15.0)),
    ),
)

LAST_BED_RACE = MultiPreset(
    name="LAST_BED_RACE",
    policy=Policy.MANUAL,
    transports=(
        MultiPresetTransport(Patient(id="P1", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT), (8.0, 9.0), target="Hospital_1"),
        MultiPresetTransport(Patient(id="P2", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT), (8.0, 9.0), target="Hospital_1"),
    ),
    # Hospital_3 has no ICU beds at all in the base seed; drop Hospital_1's
    # (which does) down to a single one so the second transport's identical
    # request genuinely races the first for it.
    bed_overrides=(BedOverride(at_ms=0, hospital_id="Hospital_1", bed_type=BedType.ICU, total=1),),
)

DECLINE_CHAIN = MultiPreset(
    name="DECLINE_CHAIN",
    policy=Policy.AUTO,
    transports=(
        MultiPresetTransport(Patient(id="P1", acuity=4, condition=ConditionCategory.TRAUMA, age_group=AgeGroup.ADULT), (8.0, 8.0)),
    ),
    # Take the two closest trauma-capable hospitals out of contention right
    # before the transport starts: Hospital_1 loses its trauma surgeon,
    # Hospital_5 (no trauma capability) never mattered anyway, so give
    # Hospital_6 (the next-closest with a trauma capability) a full GENERAL
    # ward — leaving only the third-closest, Hospital_4, to accept.
    status_overrides=(
        StatusOverride(at_ms=0, hospital_id="Hospital_1", changes={"specialists_on_shift": []}),
    ),
    bed_overrides=(BedOverride(at_ms=0, hospital_id="Hospital_6", bed_type=BedType.GENERAL, total=0),),
)

CAPACITY_DROP = MultiPreset(
    name="CAPACITY_DROP",
    policy=Policy.MANUAL,
    transports=tuple(
        MultiPresetTransport(
            Patient(id=f"P{i}", acuity=2, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT), (8.0, 8.0)
        )
        for i in range(4)
    ),
    bed_overrides=(BedOverride(at_ms=3000, hospital_id="Hospital_1", bed_type=BedType.ICU, total=2),),
)

MASS_CASUALTY = MultiPreset(
    name="MASS_CASUALTY",
    policy=Policy.AUTO,
    transports=tuple(
        MultiPresetTransport(
            Patient(id=f"P{i}", acuity=(i % 5) + 1, condition=ConditionCategory.GENERAL, age_group=AgeGroup.ADULT),
            (float(4 + i * 3 % 36), float(4 + i * 7 % 36)),
        )
        for i in range(10)
    ),
    status_overrides=(StatusOverride(at_ms=4000, hospital_id="Hospital_2", changes={"diversion": "FULL"}),),
)

ALL_MULTI_PRESETS: dict[str, MultiPreset] = {
    preset.name: preset for preset in (MULTI_NORMAL, LAST_BED_RACE, DECLINE_CHAIN, CAPACITY_DROP, MASS_CASUALTY)
}


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
