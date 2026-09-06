"""Wires one Bus, the three Facilities, an ambulance per transport, and the
Dispatcher into a single runnable system. This is the only module that
constructs all of them together — everywhere else takes its dependencies
by injection (see clock.py's docstring), and this is where that injection
actually happens.
"""
from __future__ import annotations

import random
from typing import Callable, NamedTuple, Optional, Sequence

from app.ambulance import Ambulance
from app.bus import Bus
from app.checker import check
from app.clock import Clock, FakeClock
from app.config import Config
from app.dispatcher import Dispatcher
from app.events import Event, EventStore, EventType
from app.facility import Facility
from app.models import BedType, Hospital, Patient, hospital_to_payload, initial_status
from app.presets import ALL_MULTI_PRESETS, ALL_PRESETS
from app.projection import Projection, apply_status_event, project
from app.projection import free as _ledger_free
from app.projection import load as _ledger_load
from app.seed import HOSPITALS, NETWORK

_FACILITY_IDS = tuple(h.facility_id for h in HOSPITALS)


class Simulation:
    def __init__(
        self,
        clock: Clock,
        store: EventStore,
        config: Config,
        strict_bound: bool = True,
        min_delay_ms: Optional[int] = None,
        max_delay_ms: Optional[int] = None,
        hospitals: Optional[dict[str, Hospital]] = None,
    ) -> None:
        self._clock = clock
        self._store = store
        self._config = config
        self._bus = Bus(
            clock,
            store,
            d_max_ms=config.d_max_ms,
            min_delay_ms=min_delay_ms if min_delay_ms is not None else NETWORK.min_delay_ms,
            # NETWORK.max_delay_ms bakes in the *global* settings.d_max_ms at
            # import time; the bound that actually matters is this instance's
            # own config.d_max_ms, so that's the default here instead.
            max_delay_ms=max_delay_ms if max_delay_ms is not None else config.d_max_ms,
            duplicate_rate=NETWORK.duplicate_rate,
            strict_bound=strict_bound,
        )

        self._facilities: dict[str, Facility] = {}
        for hospital in HOSPITALS:
            facility = Facility(hospital.facility_id, clock, store, self._bus, config)
            self._facilities[hospital.facility_id] = facility
            self._bus.register_endpoint(hospital.facility_id, facility.receive_command)

        # Phase 12+: a second, richer hospital set (six of them, capacity-
        # aware) alongside — not replacing — the three plain ones above.
        # `hospitals` is None for every pre-existing caller.
        self._hospitals: dict[str, Hospital] = dict(hospitals or {})
        for hospital in self._hospitals.values():
            facility = Facility(hospital.id, clock, store, self._bus, config, hospital=hospital)
            self._facilities[hospital.id] = facility
            self._bus.register_endpoint(hospital.id, facility.receive_command)

        self._ambulances: dict[str, Ambulance] = {}
        self._dispatcher = Dispatcher(
            clock, store, self._bus, config, hospitals=self._hospitals,
            on_no_destination=self._stand_down_ambulance,
        )
        self._bus.register_dispatcher(self._dispatcher.on_ack)
        # How many times each multi-preset has been run, so repeat clicks
        # get fresh transport ids instead of colliding (see run_multi_preset).
        self._preset_runs: dict[str, int] = {}
        # Set by main.py to the hub's movement hook; None everywhere else
        # (tests and the fuzz engine have nobody watching).
        self.on_movement: Optional[Callable[[str], None]] = None

    # -- read-only access for callers that need it (tests, main.py) --------

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def hospitals(self) -> dict[str, Hospital]:
        return dict(self._hospitals)

    @property
    def facilities(self) -> dict[str, Facility]:
        return dict(self._facilities)

    @property
    def ambulances(self) -> dict[str, Ambulance]:
        return dict(self._ambulances)

    # -- orchestration -----------------------------------------------------

    def start(self, transport_id: str, destination: str, manual_confirm: bool = False) -> None:
        ambulance = Ambulance(transport_id, self._clock, self._store, self._bus, self._config, manual_confirm=manual_confirm)
        self._ambulances[transport_id] = ambulance
        self._bus.register_endpoint(transport_id, ambulance.receive_command)
        self._dispatcher.start(transport_id, destination)

    def redirect(self, transport_id: str, target: Optional[str] = None) -> None:
        self._dispatcher.redirect(transport_id, target)

    def start_capacity_aware(
        self, transport_id: str, patient: Patient, position: tuple[float, float], destination: Optional[str] = None
    ) -> None:
        """Phase 15: one capacity-aware transport, wired end to end — a
        real Ambulance with M1/M2 position-based movement, its arrival
        reported back to the dispatcher (R19) and its live position kept in
        sync on every tick (so a later redirect's eta_minutes reflects
        where it actually is, not where it started — M2)."""
        ambulance = Ambulance(
            transport_id, self._clock, self._store, self._bus, self._config,
            hospitals=self._hospitals, position=position, on_arrived=self._dispatcher.on_arrived,
            on_position_changed=self._on_position_changed,
        )
        self._ambulances[transport_id] = ambulance
        self._bus.register_endpoint(transport_id, ambulance.receive_command)
        self._dispatcher.start(transport_id, destination=destination, patient=patient, position=position)

    # -- Phase 21: the roster is mutable at runtime ------------------------

    def register_hospital(self, hospital: Hospital, *, log: bool = True) -> None:
        """Bring a hospital into the network mid-run.

        Three things have to happen together or the hospital is a ghost that
        wins rankings and never answers: it needs a Facility, that Facility
        needs a bus endpoint (PREPARE is addressed by id), and the dispatcher
        needs a status entry. Registration used to be construction-only, so
        all three were impossible after startup.

        `log=False` is for replaying a roster that is already in the log —
        bootstrap and restart — where re-appending would duplicate history."""
        existing = self._facilities.get(hospital.id)
        self._hospitals[hospital.id] = hospital
        if existing is None:
            facility = Facility(hospital.id, self._clock, self._store, self._bus, self._config, hospital=hospital)
            self._facilities[hospital.id] = facility
            self._bus.register_endpoint(hospital.id, facility.receive_command)
        self._dispatcher.add_hospital(hospital)
        if log:
            self._store.append(
                Event(
                    transport_id=hospital.id, epoch=0, ts_ms=self._clock.now_ms(),
                    type=EventType.HOSPITAL_REGISTERED, facility_id=hospital.id,
                    payload=hospital_to_payload(hospital),
                )
            )

    def restore_hospital_status_from_log(self) -> None:
        """Re-derive every hospital's live status by replaying the log (E21).

        Registration alone is not enough to rebuild the network: a hospital
        put on diversion, or whose bed count was changed, must come back that
        way after a restart. Without this the running system believed every
        hospital was wide open while checker.py — which does replay the whole
        log — knew better, and the two disagreed about whether an acceptance
        was legal. Uses the same pure apply_status_event the checker uses, so
        they cannot drift."""
        events = self._store.replay()
        by_hospital: dict[str, list[Event]] = {}
        for event in events:
            if event.type in (EventType.HOSPITAL_STATUS_CHANGED, EventType.BEDS_REPORTED):
                if event.facility_id is not None:
                    by_hospital.setdefault(event.facility_id, []).append(event)
        for hospital_id, hospital in self._hospitals.items():
            status = initial_status(hospital)
            for event in by_hospital.get(hospital_id, []):
                status = apply_status_event(status, event)
            facility = self._facilities.get(hospital_id)
            if facility is not None:
                facility.restore_status(status)
            self._dispatcher.restore_hospital_status(hospital_id, status)

    def update_hospital(self, hospital: Hospital) -> None:
        """Re-configure a hospital in place (capabilities, name, location).
        The Facility keeps its identity and its live status — only the static
        record it was built from changes."""
        previous_status = self._dispatcher.hospital_status_of(hospital.id)
        self._hospitals[hospital.id] = hospital
        facility = self._facilities.get(hospital.id)
        if facility is not None:
            facility.set_hospital(hospital)
        self._dispatcher.add_hospital(hospital)
        self._store.append(
            Event(
                transport_id=hospital.id, epoch=0, ts_ms=self._clock.now_ms(),
                type=EventType.HOSPITAL_UPDATED, facility_id=hospital.id,
                payload=hospital_to_payload(hospital),
            )
        )
        # Bed counts live on the event-sourced HospitalStatus, not on the
        # static Hospital record — free()/load() and every acceptance check
        # read the status. Updating only the record left the update silently
        # ineffective: the API returned 200 and the bed counts never moved.
        # Route the change through the same BedsReported path a hospital uses
        # to report beds itself, so capacity rebalance (R18) also runs.
        if previous_status is not None:
            for bed_type, total in hospital.beds_total.items():
                if previous_status.beds_total.get(bed_type) != total:
                    self.report_beds(hospital.id, bed_type, total)
            for bed_type in previous_status.beds_total:
                if bed_type not in hospital.beds_total:
                    self.report_beds(hospital.id, bed_type, 0)

    def decommission_hospital(self, hospital_id: str, reason: str = "decommissioned") -> None:
        """Retire a hospital that may have ambulances already driving to it.

        Deliberately expressed in terms the system already understands rather
        than as a new teardown path: closing a hospital *is* its capacity
        going to zero. Reporting every bed type to 0 runs R18's existing
        rebalance, which displaces the transports holding those beds and
        re-routes them through the ordinary redirect protocol — so the
        one-active-facility invariant is preserved by the same machinery that
        preserves it everywhere else. FULL diversion stops it being chosen
        again on the way out."""
        if hospital_id not in self._hospitals:
            raise KeyError(hospital_id)
        self.report_hospital_status(hospital_id, {"diversion": "FULL"})
        for bed_type in list(self._hospitals[hospital_id].beds_total):
            self.report_beds(hospital_id, bed_type, 0)
        self._dispatcher.remove_hospital(hospital_id)
        self._hospitals.pop(hospital_id, None)
        self._store.append(
            Event(
                transport_id=hospital_id, epoch=0, ts_ms=self._clock.now_ms(),
                type=EventType.HOSPITAL_DECOMMISSIONED, facility_id=hospital_id,
                payload={"hospital_id": hospital_id, "reason": reason},
            )
        )

    def report_hospital_status(self, hospital_id: str, changes: dict) -> None:
        """Phase 17's POST /hospitals/{id}/status."""
        event = self._store.append(
            Event(
                transport_id=hospital_id, epoch=0, ts_ms=self._clock.now_ms(),
                type=EventType.HOSPITAL_STATUS_CHANGED, facility_id=hospital_id, payload=changes,
            )
        )
        facility = self._facilities.get(hospital_id)
        if facility is not None:
            facility.on_status_event(event)
            status = facility.status_of()
            if status is not None:
                self._dispatcher.on_hospital_status_changed(hospital_id, status)

    def report_beds(self, hospital_id: str, bed_type: BedType, total: int) -> None:
        """Phase 17's POST /hospitals/{id}/beds."""
        event = self._store.append(
            Event(
                transport_id=hospital_id, epoch=0, ts_ms=self._clock.now_ms(),
                type=EventType.BEDS_REPORTED, facility_id=hospital_id,
                payload={"bed_type": bed_type.value, "total": total},
            )
        )
        facility = self._facilities.get(hospital_id)
        if facility is not None:
            facility.on_status_event(event)
        self._dispatcher.on_beds_reported(hospital_id, bed_type, total)

    def run_multi_preset(self, name: str) -> list[str]:
        """Phase 17: runs one of presets.ALL_MULTI_PRESETS — applies any
        at_ms=0 status/bed overrides immediately (so a preset like
        LAST_BED_RACE/DECLINE_CHAIN, which needs a hospital already
        deficient the instant its transports start, isn't racing its own
        scheduled callback on a real clock), schedules the rest, then
        starts every scripted transport."""
        preset = ALL_MULTI_PRESETS[name]
        self._dispatcher.set_policy(preset.policy)
        for status_override in preset.status_overrides:
            if status_override.at_ms <= 0:
                self.report_hospital_status(status_override.hospital_id, status_override.changes)
            else:
                self._clock.schedule(
                    status_override.at_ms,
                    lambda o=status_override: self.report_hospital_status(o.hospital_id, o.changes),
                )
        for bed_override in preset.bed_overrides:
            if bed_override.at_ms <= 0:
                self.report_beds(bed_override.hospital_id, bed_override.bed_type, bed_override.total)
            else:
                self._clock.schedule(
                    bed_override.at_ms,
                    lambda o=bed_override: self.report_beds(o.hospital_id, o.bed_type, o.total),
                )
        from app.dispatcher import NotEligible

        # Preset transport ids used to be derived from the preset name and the
        # patient index alone, so a second click reused the first run's ids and
        # the dispatcher rejected them as already-started (a 500 on every run
        # after the first). A per-simulation run counter makes each click a
        # fresh set. It is omitted on run 1 so the ids a first run produces —
        # and every test that asserts on them — are unchanged.
        self._preset_runs[name] = self._preset_runs.get(name, 0) + 1
        run = self._preset_runs[name]
        suffix = "" if run == 1 else f"-r{run}"

        started: list[str] = []
        for index, transport in enumerate(preset.transports):
            transport_id = f"{name}{suffix}-{transport.patient.id}-{index}"
            try:
                self.start_capacity_aware(transport_id, transport.patient, transport.position, transport.target)
            except NotEligible:
                pass  # E28: the second of two racing transports is expected to fail here in manual policy
            started.append(transport_id)
        return started

    def start_batch(self, patients: Sequence[tuple[Patient, tuple[float, float]]]) -> list[str]:
        """Phase 15/17: POST /transports/batch's engine — one fresh
        transport_id per (patient, position) pair, each run through
        start_capacity_aware with no explicit destination (auto-chosen via
        rank()/choose()). A transport with genuinely no accepting hospital
        (E30/R15's NoAcceptingFacility) doesn't crash the rest of the
        batch — it's still returned, just never gets a destination."""
        from app.dispatcher import NotEligible

        started: list[str] = []
        for index, (patient, position) in enumerate(patients):
            transport_id = f"BATCH-{patient.id}-{index}"
            try:
                self.start_capacity_aware(transport_id, patient, position)
            except NotEligible:
                pass
            started.append(transport_id)
        return started

    def confirm(self, transport_id: str) -> None:
        """Manually applies whatever REDIRECT_NOTICE(s) that transport's
        ambulance is holding when running with manual_confirm=True."""
        self._ambulances[transport_id].confirm()

    def confirm_ready(self, facility_id: str, transport_id: str) -> None:
        """Manually sends whatever READY ack(s) that facility is holding
        for this transport when running with manual_ready=True."""
        self._facilities[facility_id].confirm_ready(transport_id)

    def set_manual_ready(self, facility_id: str, enabled: bool) -> None:
        self._facilities[facility_id].set_manual_ready(enabled)

    def set_manual_confirm(self, transport_id: str, enabled: bool) -> None:
        self._ambulances[transport_id].set_manual_confirm(enabled)

    def load_delays(self, delays: Sequence[int], duplicate_rate: float) -> None:
        self._bus.load_preset(delays, duplicate_rate)

    def hold(self, command_id: str) -> None:
        self._bus.hold(command_id)

    def release(self, command_id: str) -> None:
        self._bus.release(command_id)

    def in_flight(self) -> list[dict]:
        return self._bus.in_flight()

    def ambulance_view(self, transport_id: str) -> Optional[dict]:
        ambulance = self._ambulances.get(transport_id)
        if ambulance is None:
            return None
        return {
            # What the *crew* believes, which is not always what the
            # dispatcher has committed to: the two diverge for exactly the
            # length of a handoff, which is the whole point of the protocol.
            "known_destination": ambulance.known_destination,
            "progress": ambulance.progress,
            "position": ambulance.position,
            "remaining_km": ambulance.remaining_km,
            "arrived": ambulance.has_arrived,
        }

    def _on_position_changed(self, transport_id: str, position: tuple[float, float]) -> None:
        """An ambulance moved. The dispatcher needs it (ETA is scored from the
        *current* position, not the origin), and so does anything watching —
        movement appends no event, so without this hook the hub only learns a
        transport has moved when some unrelated protocol event happens to fire
        for it, and the map and progress bars sit frozen in between."""
        self._dispatcher.update_position(transport_id, position)
        if self.on_movement is not None:
            self.on_movement(transport_id)

    def _stand_down_ambulance(self, transport_id: str, through_seq: int = 0) -> None:
        """Dispatcher -> ambulance direction of the wiring in
        start_capacity_aware (which wires the ambulance -> dispatcher
        direction). Called when a transport is left with no destination at
        all; see Ambulance.stand_down."""
        ambulance = self._ambulances.get(transport_id)
        if ambulance is not None:
            ambulance.stand_down(through_seq)

    def transport_list(self) -> list[dict]:
        """One row per capacity-aware transport — the shape GET /transports
        returns and the hub pushes as "transport_list". Defined once, here,
        rather than in the route: the two used to be separate and the table
        could only be refreshed by hand, which is exactly the drift this
        avoids."""
        # Read from the dispatcher's live state rather than replaying the log.
        # This runs on every hub flush (the table is pushed live), and views()
        # replays *and projects the whole log twice* — O(events), which at a
        # few thousand events was costing ~25ms per flush and climbing without
        # bound for the length of a session. The rows below are display state
        # for transports this dispatcher is actively running, so its own
        # bookkeeping is the right source; the log-derived projection remains
        # authoritative for the invariant check, which is what actually has to
        # be independent of the dispatcher.
        rows: list[dict] = []
        for transport_id in self._dispatcher.known_patients():
            rows.append(
                {
                    "transport_id": transport_id,
                    "current_destination": self._dispatcher.current_destination_of(transport_id),
                    "pending_destination": self._dispatcher.pending_destination_of(transport_id),
                    "current_epoch": self._dispatcher.current_epoch_of(transport_id),
                    "status": self._dispatcher.status_of(transport_id).value,
                    "position": self._dispatcher.position_of(transport_id),
                    # DispatcherStatus has no ARRIVED member (arrival ends the
                    # journey, it isn't a handoff state), so this is the only
                    # thing that tells a transport still driving from one that
                    # has landed.
                    "arrived_at": self._dispatcher.arrived_at_of(transport_id),
                }
            )
        return rows

    def hospital_ids(self) -> list[str]:
        return list(self._hospitals)

    def hospital_view(self, hospital_id: str) -> Optional[dict]:
        if hospital_id not in self._hospitals:
            return None
        hospital = self._hospitals[hospital_id]
        status = self._dispatcher.hospital_status_of(hospital_id)
        view = self._dispatcher.ledger_view(hospital_id)
        return {
            "id": hospital.id,
            "name": hospital.name,
            "location": hospital.location,
            "beds_total": {bed_type.value: total for bed_type, total in status.beds_total.items()},
            "free": {bed_type.value: _ledger_free(view, status, bed_type) for bed_type in status.beds_total},
            "load": _ledger_load(view, status),
            # The reverse lookup: a bar reading 4/6 says nothing about *whose*
            # four beds those are. Reserved and occupied are listed separately
            # because they mean different things — a reservation is a bed held
            # for an ambulance still en route and can still be released; an
            # occupied bed has a patient in it.
            "holders": {
                bed_type.value: {
                    "reserved": sorted(view.reserved.get(bed_type, frozenset())),
                    "occupied": sorted(view.occupied.get(bed_type, frozenset())),
                }
                for bed_type in status.beds_total
            },
            "diversion": status.diversion.value,
            "diverted_categories": [c.value for c in status.diverted_categories],
            "ed_saturation": status.ed_saturation,
            "specialists_on_shift": [s.value for s in status.specialists_on_shift],
        }

    def run_preset(self, transport_id: str, name: str) -> None:
        preset = ALL_PRESETS[name]
        self._bus.load_preset(preset.delays, preset.duplicate_rate)
        for scheduled in preset.redirects:
            target = scheduled.target_facility
            self._clock.schedule(scheduled.at_ms, lambda t=target: self.redirect(transport_id, t))

    def run_until_quiet(self) -> None:
        if not isinstance(self._clock, FakeClock):
            raise TypeError("run_until_quiet() requires a FakeClock; RealClock runs on its own event loop")
        self._clock.run_until_quiet()

    # -- reading the result --------------------------------------------------

    def events(self, transport_id: Optional[str] = None) -> list[Event]:
        return self._store.replay(transport_id)

    def views(self) -> Projection:
        """E21: a transport this dispatcher instance has no live memory of
        (only reachable right after a restart, since start() always
        registers into the live dispatcher) shows as INTERRUPTED rather
        than whatever transient status its last event happened to leave —
        there are no timers left anywhere to carry it forward."""
        events = self._store.replay()
        live = project(events, mark_interrupted=False)
        interrupted = project(events, mark_interrupted=True)
        transports = {
            transport_id: (view if self._dispatcher.is_known(transport_id) else interrupted.transports[transport_id])
            for transport_id, view in live.transports.items()
        }
        return Projection(transports=transports, facilities=live.facilities)


class FuzzRunSummary(NamedTuple):
    runs: int
    passed: int
    failed: int
    max_local_overlap_ms: int
    violations: list[dict]


def run_random_fuzz(config: Config, runs: int, rng: Optional[random.Random] = None) -> FuzzRunSummary:
    """POST /demo/fuzz's engine: `runs` independent randomized scenarios,
    structurally similar to test_fuzz.py's Hypothesis strategy but driven
    by plain `random` — Hypothesis is a test-time tool, not something a
    running server invokes on a request. Each run gets its own fresh
    FakeClock/EventStore/Simulation, entirely separate from the live demo's
    own state."""
    rng = rng or random.Random()
    passed = failed = 0
    max_overlap_ms = 0
    violations: list[dict] = []
    for _ in range(runs):
        result = check(_one_fuzz_run(config, rng))
        if result.passed:
            passed += 1
        else:
            failed += 1
            violations.extend(
                {"transport_id": v.transport_id, "ts_ms": v.ts_ms, "kind": v.kind.value, "detail": v.detail}
                for v in result.violations
            )
        max_overlap_ms = max(max_overlap_ms, result.max_local_overlap_ms)
    return FuzzRunSummary(runs=runs, passed=passed, failed=failed, max_local_overlap_ms=max_overlap_ms, violations=violations)


def _one_fuzz_run(config: Config, rng: random.Random) -> list[Event]:
    clock = FakeClock()
    store = EventStore(":memory:")
    sim = Simulation(clock, store, config)
    for i in range(rng.randint(1, 10)):
        transport_id = f"FUZZ-{i}"
        initial = rng.choice(_FACILITY_IDS)
        sim.start(transport_id, initial)
        seen_targets = [initial]
        at_ms = 0
        for _ in range(rng.randint(0, 4)):
            # Occasionally repeat an earlier target (R8/R9) or fire close on
            # the last one's heels (R6's queue path) — same bias as
            # test_fuzz.py's strategy, just via plain random here.
            target = rng.choice(seen_targets) if rng.random() < 0.3 else rng.choice(_FACILITY_IDS)
            at_ms += rng.randint(0, 800)
            clock.schedule(at_ms, lambda t=transport_id, dest=target: sim.redirect(t, dest))
            seen_targets.append(target)
    sim.run_until_quiet()
    return sim.events()
