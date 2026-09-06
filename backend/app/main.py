"""FastAPI entrypoint. One demo transport (seed.TRANSPORT) drives the /demo/*
routes; /transports/* is the general multi-transport API (E18). All shared
state lives on app.state.app_state, replaced wholesale by /demo/reset rather
than mutated piecemeal.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import ai
from app.checker import CheckResult, check
from app.clock import RealClock
from app.config import Config
from app.dispatcher import NotEligible, TargetRequired
from app.events import Event, EventStore
from app.models import (
    AgeGroup, BedType, ConditionCategory, Diversion, Hospital, Need, Patient, Policy,
    hospital_from_payload,
)
from app.presets import ALL_MULTI_PRESETS, ALL_PRESETS
from app.projection import FacilityView, TransportView, project_hospitals
from app.readmodel import HospitalReadModel
from app.scoring import RankedCandidate
from app.seed import HOSPITALS, MULTI_HOSPITALS, TRANSPORT
from app.simulation import Simulation, run_random_fuzz
from app.ws import Hub

DEMO_TRANSPORT_ID = TRANSPORT.transport_id
KNOWN_FACILITIES = frozenset(h.facility_id for h in HOSPITALS)
MULTI_HOSPITALS_BY_ID = {h.id: h for h in MULTI_HOSPITALS}


class AppState:
    """Everything the routes need. /demo/reset replaces this object
    wholesale (a fresh EventStore + Simulation) rather than mutating one in
    place, so nothing can accidentally hold a stale reference across a reset
    except the Hub, which is told explicitly (rebind_source)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = EventStore(config.db_path)
        self.clock = RealClock()
        self.simulation = Simulation(self.clock, self.store, config)
        self.hub = Hub()
        # Relational view of the hospitals, derived from the log. Subscribed
        # here so it stays current incrementally; rebuilt from scratch at
        # startup and after a reset. Never written to directly.
        self.hospitals_db = HospitalReadModel(self.store)
        self.store.subscribe(self.hospitals_db.apply)
        # (store revision, result) for GET /invariant — see that route.
        self.invariant_cache: Optional[tuple[int, CheckResult]] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Config.from_env() is called here, at startup, rather than relying on
    # the module-level `settings` singleton imported elsewhere — that lets
    # a test set DB_PATH before the app starts and have it actually take.
    state = AppState(Config.from_env())
    _bootstrap_roster(state)
    state.simulation.on_movement = state.hub.note_movement
    state.hub.configure(asyncio.get_running_loop(), state.store, state.simulation)
    app.state.app_state = state
    yield


app = FastAPI(title="DivertTrack", lifespan=lifespan)


def _state() -> AppState:
    return app.state.app_state


def _transport_exists(state: AppState, transport_id: str) -> bool:
    # True either for a transport this process's dispatcher is actively
    # managing, or one that only exists in event history from before a
    # restart (E21) — GET routes should still show that one, just as
    # INTERRUPTED.
    return state.simulation.dispatcher.is_known(transport_id) or bool(state.store.replay(transport_id))


def _require_transport_seen(state: AppState, transport_id: str) -> None:
    if not _transport_exists(state, transport_id):
        raise HTTPException(status_code=404, detail=f"Transport {transport_id!r} not found")


def _require_redirectable(state: AppState, transport_id: str) -> None:
    if state.simulation.dispatcher.is_known(transport_id):
        return
    if _transport_exists(state, transport_id):
        raise HTTPException(
            status_code=404,
            detail=f"Transport {transport_id!r} was interrupted by a restart and needs to be reset",
        )
    raise HTTPException(status_code=404, detail=f"Transport {transport_id!r} not found")


def _transport_view(state: AppState, transport_id: str) -> TransportView:
    return state.simulation.views().transports[transport_id]


class StartTransportRequest(BaseModel):
    transport_id: str
    destination: str
    manual_confirm: bool = False


class RedirectRequest(BaseModel):
    target: Optional[str] = None


class BedsReportRequest(BaseModel):
    bed_type: str
    total: int


class HospitalRequest(BaseModel):
    """Phase 21: a hospital's static configuration, as an operator supplies
    it. Enum members arrive as their string values and are validated in
    _build_hospital so an unknown bed type or capability is a 400 naming the
    field, not a 500."""

    id: str
    name: str
    location: tuple[float, float]
    beds_total: dict[str, int]
    capabilities: list[str] = []
    ventilators_total: int = 0
    max_eta_minutes: dict[str, int] = {}


class StatusChangeRequest(BaseModel):
    specialists_on_shift: Optional[list[str]] = None
    ed_saturation: Optional[float] = None
    diversion: Optional[str] = None
    diverted_categories: Optional[list[str]] = None


class BatchPatientRequest(BaseModel):
    id: str
    acuity: int
    condition: str
    age_group: str = "ADULT"
    needs: list[str] = []
    override: str = "none"
    position: tuple[float, float]


class BatchStartRequest(BaseModel):
    patients: list[BatchPatientRequest]


class PolicyRequest(BaseModel):
    mode: str  # "manual" | "auto"


def _bootstrap_roster(state: "AppState") -> None:
    """Bring the simulation's roster up to whatever the log says.

    On a fresh log this writes seed.MULTI_HOSPITALS in as HospitalRegistered
    events — the seed is now a *bootstrap*, not a source of truth. On a log
    that already has roster events (a restart, E21) it replays them instead,
    so a hospital an operator added survives a restart and one they retired
    stays retired. Either way the roster ends up derived from the log.
    """
    roster = project_hospitals(state.store.replay()).active
    if not roster:
        for hospital in MULTI_HOSPITALS:
            state.simulation.register_hospital(hospital)
        return
    for hospital in roster.values():
        state.simulation.register_hospital(hospital, log=False)
    # Replaying an existing roster appends nothing, so the incremental
    # subscription sees no events — rebuild the tables from the log instead.
    state.hospitals_db.rebuild()
    # Registration restores *which* hospitals exist; this restores what state
    # they are in (diversion, bed counts). Both have to come from the log or
    # the running system and the invariant checker disagree about history.
    state.simulation.restore_hospital_status_from_log()


def _hospital_view(state: "AppState", hospital_id: str) -> dict:
    return state.simulation.hospital_view(hospital_id)


def _candidate_view(candidate: RankedCandidate) -> dict:
    return {"hospital_id": candidate.hospital_id, "score": candidate.score, "reason": candidate.reason}


def _build_hospital(body: "HospitalRequest") -> Hospital:
    try:
        return hospital_from_payload(body.model_dump())
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid hospital: {exc}") from exc


def _build_patient(body: "BatchPatientRequest") -> Patient:
    # The enums are validated here rather than left to raise: a bad
    # condition/age_group/need is a malformed request, and letting ValueError
    # escape turned it into a 500 with a traceback instead of a 400 naming the
    # bad field (report_beds above already does this for BedType).
    try:
        condition = ConditionCategory(body.condition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown condition {body.condition!r}") from exc
    try:
        age_group = AgeGroup(body.age_group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown age group {body.age_group!r}") from exc
    try:
        needs = frozenset(Need(n) for n in body.needs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown need in {body.needs!r}") from exc
    return Patient(
        id=body.id,
        acuity=body.acuity,
        condition=condition,
        age_group=age_group,
        needs=needs,
        override=body.override,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# -- transports: the general, multi-transport API (E18) ---------------------


@app.post("/transports", status_code=201)
def create_transport(body: StartTransportRequest) -> TransportView:
    state = _state()
    if body.destination not in KNOWN_FACILITIES:
        raise HTTPException(status_code=400, detail=f"Unknown facility {body.destination!r}")
    if _transport_exists(state, body.transport_id):
        raise HTTPException(status_code=409, detail=f"Transport {body.transport_id!r} already started")
    state.simulation.start(body.transport_id, body.destination, manual_confirm=body.manual_confirm)
    return _transport_view(state, body.transport_id)


@app.post("/transports/{transport_id}/redirect", response_model=None)
def redirect_transport(transport_id: str, body: RedirectRequest):
    # No response_model: this returns a TransportView for the plain path
    # (unchanged from before this extension) but a richer
    # {"transport":..., "candidates":...} dict for a capacity-aware one —
    # FastAPI can't validate both against one declared shape.
    state = _state()
    _require_redirectable(state, transport_id)
    is_capacity_aware = state.simulation.dispatcher.patient_of(transport_id) is not None
    if not is_capacity_aware:
        if body.target is None or body.target not in KNOWN_FACILITIES:
            raise HTTPException(status_code=400, detail=f"Unknown facility {body.target!r}")
        state.simulation.redirect(transport_id, body.target)
        return _transport_view(state, transport_id)

    if body.target is not None and body.target not in state.simulation.hospitals:
        raise HTTPException(status_code=400, detail=f"Unknown hospital {body.target!r}")
    try:
        state.simulation.redirect(transport_id, body.target)
    except NotEligible as exc:
        raise HTTPException(status_code=409, detail={"error": "not_eligible", "reason": exc.reason}) from exc
    except TargetRequired as exc:
        # Omitting the target asks the dispatcher to choose, which only AUTO
        # policy permits (under MANUAL the operator picks). That's a bad
        # request, not a server fault — it used to escape as a 500.
        #
        # Deliberately not `except ValueError`: Pydantic's ValidationError is
        # one too, and catching both reported genuine internal bugs as 400s.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    candidates = [_candidate_view(c) for c in state.simulation.dispatcher.candidates_of(transport_id)]
    return {"transport": _transport_view(state, transport_id), "candidates": candidates}


# -- Phase 17: multi-hospital capacity extension ----------------------------


@app.get("/hospitals")
def list_hospitals(include_retired: bool = False) -> list[dict]:
    """Served from the read-model tables, not by replaying the log.

    project_hospitals()/project_ledger() fold every event on every call, which
    grows without bound for the life of a deployment; this is an indexed query.
    The pure replays remain the authoritative derivation the checker uses, and
    a test asserts the two agree.
    """
    return _state().hospitals_db.fetch_all(include_retired=include_retired)


@app.get("/hospitals/{hospital_id}")
def get_hospital(hospital_id: str) -> dict:
    view = _state().hospitals_db.fetch(hospital_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Unknown hospital {hospital_id!r}")
    return view


@app.post("/hospitals", status_code=201)
def register_hospital(body: HospitalRequest) -> dict:
    """Add a hospital to the live network. It is immediately rankable: the
    dispatcher gets a status entry, a Facility is built, and its bus endpoint
    is registered, so it can accept a PREPARE on the very next redirect."""
    state = _state()
    # Checked against facilities, not just the capacity-aware roster: the
    # three legacy demo facilities (Hospital_A/B/C) share the same id space
    # and the same message bus. Registering over one of them produced a
    # hospital that ranked and won like any other but was backed by a plain
    # facility with no accept() — it took patients no capacity check had
    # ever approved.
    if body.id in state.simulation.facilities:
        raise HTTPException(status_code=409, detail=f"Facility {body.id!r} already exists")
    state.simulation.register_hospital(_build_hospital(body))
    return _hospital_view(state, body.id)


@app.put("/hospitals/{hospital_id}")
def update_hospital(hospital_id: str, body: HospitalRequest) -> dict:
    state = _state()
    if hospital_id not in state.simulation.hospitals:
        raise HTTPException(status_code=404, detail=f"Unknown hospital {hospital_id!r}")
    if body.id != hospital_id:
        raise HTTPException(status_code=400, detail="Body id must match the path id")
    state.simulation.update_hospital(_build_hospital(body))
    return _hospital_view(state, hospital_id)


@app.delete("/hospitals/{hospital_id}")
def decommission_hospital(hospital_id: str, reason: str = "decommissioned") -> dict:
    """Retire a hospital, including one with ambulances already en route:
    its capacity drops to zero, which runs the ordinary R18 rebalance and
    re-routes those transports through the normal redirect protocol."""
    state = _state()
    if hospital_id not in state.simulation.hospitals:
        raise HTTPException(status_code=404, detail=f"Unknown hospital {hospital_id!r}")
    state.simulation.decommission_hospital(hospital_id, reason)
    return {"decommissioned": hospital_id, "remaining": state.simulation.hospital_ids()}


@app.post("/hospitals/{hospital_id}/beds")
def report_beds(hospital_id: str, body: BedsReportRequest) -> dict:
    if hospital_id not in _state().simulation.hospitals:
        raise HTTPException(status_code=404, detail=f"Unknown hospital {hospital_id!r}")
    try:
        bed_type = BedType(body.bed_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown bed type {body.bed_type!r}") from exc
    state = _state()
    state.simulation.report_beds(hospital_id, bed_type, body.total)
    return _hospital_view(state, hospital_id)


@app.post("/hospitals/{hospital_id}/status")
def report_status(hospital_id: str, body: StatusChangeRequest) -> dict:
    if hospital_id not in _state().simulation.hospitals:
        raise HTTPException(status_code=404, detail=f"Unknown hospital {hospital_id!r}")
    changes: dict = {}
    if body.specialists_on_shift is not None:
        changes["specialists_on_shift"] = body.specialists_on_shift
    if body.ed_saturation is not None:
        changes["ed_saturation"] = body.ed_saturation
    if body.diversion is not None:
        try:
            Diversion(body.diversion)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Unknown diversion {body.diversion!r}") from exc
        changes["diversion"] = body.diversion
    if body.diverted_categories is not None:
        changes["diverted_categories"] = body.diverted_categories
    state = _state()
    state.simulation.report_hospital_status(hospital_id, changes)
    return _hospital_view(state, hospital_id)


@app.get("/transports")
def list_transports() -> list[dict]:
    # Simulation.transport_list() owns the row shape; the hub pushes the
    # identical payload as "transport_list" on every change, so this route is
    # now just the initial seed rather than something the UI has to re-poll.
    return _state().simulation.transport_list()


@app.post("/transports/batch", status_code=201)
def start_batch(body: BatchStartRequest) -> dict:
    state = _state()
    patients = [(_build_patient(p), p.position) for p in body.patients]
    started = state.simulation.start_batch(patients)
    return {"transport_ids": started}


@app.post("/transports/{transport_id}/discharge")
def discharge_transport(transport_id: str) -> dict:
    """Free the bed a treated patient was in. Capacity only — the transport
    stays where it is and the facility stays ACTIVE, so nothing about the
    handoff invariant changes."""
    state = _state()
    _require_transport_seen(state, transport_id)
    freed = state.simulation.discharge(transport_id)
    if freed is None:
        raise HTTPException(
            status_code=409,
            detail=f"Transport {transport_id!r} has no patient in a bed to discharge",
        )
    hospital_id, bed_type = freed
    return {"discharged": transport_id, "hospital_id": hospital_id, "bed_type": bed_type.value}


@app.get("/transports/{transport_id}/candidates")
def get_candidates(transport_id: str) -> list[dict]:
    state = _state()
    _require_transport_seen(state, transport_id)
    return [_candidate_view(c) for c in state.simulation.dispatcher.candidates_of(transport_id)]


@app.get("/policy")
def get_policy() -> dict:
    return {"policy": _state().simulation.dispatcher.policy.value}


@app.post("/policy")
def set_policy(body: PolicyRequest) -> dict:
    try:
        policy = Policy(body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown policy mode {body.mode!r}") from exc
    _state().simulation.dispatcher.set_policy(policy)
    return {"policy": policy.value}


@app.get("/invariant")
def get_global_invariant() -> CheckResult:
    """I1-I4 over the entire log — the most expensive read in the API, so
    the result is memoized against the store's revision: repeated calls
    while nothing new has been appended cost an integer compare."""
    state = _state()
    revision = state.store.revision
    if state.invariant_cache is not None and state.invariant_cache[0] == revision:
        return state.invariant_cache[1]
    result = check(
        state.store.replay(),
        patients=state.simulation.dispatcher.known_patients(),
        # No `hospitals=`: check() projects the roster from the log itself,
        # including hospitals since decommissioned, so history is audited
        # against the network as it was rather than as it is now.
        saturation_limit=state.config.saturation_limit,
    )
    state.invariant_cache = (revision, result)
    return result


@app.get("/transports/{transport_id}")
def get_transport(transport_id: str) -> TransportView:
    state = _state()
    _require_transport_seen(state, transport_id)
    return _transport_view(state, transport_id)


@app.get("/transports/{transport_id}/facilities")
def get_transport_facilities(transport_id: str) -> dict[str, FacilityView]:
    state = _state()
    _require_transport_seen(state, transport_id)
    projection = state.simulation.views()
    return {
        facility_id: view
        for (facility_id, tid), view in projection.facilities.items()
        if tid == transport_id
    }


@app.get("/transports/{transport_id}/events")
def get_transport_events(transport_id: str) -> list[Event]:
    state = _state()
    _require_transport_seen(state, transport_id)
    return state.store.replay(transport_id)


@app.get("/transports/{transport_id}/invariant")
def get_transport_invariant(transport_id: str) -> CheckResult:
    state = _state()
    _require_transport_seen(state, transport_id)
    return check(state.store.replay(transport_id))


# -- demo: drives the one seeded transport for the UI's controls -----------


@app.post("/demo/preset/{name}")
def demo_preset(name: str) -> dict:
    state = _state()
    if name in ALL_MULTI_PRESETS:
        started = state.simulation.run_multi_preset(name)
        return {"preset": name, "transport_ids": started}
    if name not in ALL_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset {name!r}")
    _require_redirectable(state, DEMO_TRANSPORT_ID)
    state.simulation.run_preset(DEMO_TRANSPORT_ID, name)
    return {"preset": name, "transport_id": DEMO_TRANSPORT_ID}


@app.post("/demo/reset")
def demo_reset() -> dict[str, str]:
    state = _state()
    state.store.clear()
    # A fresh Simulation discards every Facility/Dispatcher/Ambulance and
    # their in-memory state — the intended "start the demo over" behavior.
    # Known limitation: any timer already scheduled by the old Simulation
    # (e.g. a pending cutover) is not cancelled here and could still write
    # a few stray, ultimately harmless events after reset before settling;
    # a production system would track and cancel those handles explicitly.
    state.simulation = Simulation(state.clock, state.store, state.config)
    state.hospitals_db.rebuild()  # the log was just cleared
    _bootstrap_roster(state)
    state.simulation.on_movement = state.hub.note_movement
    state.hub.rebind_source(state.simulation)
    # Memoized against store.revision, which clear() bumps — but drop it
    # anyway rather than relying on that coupling from another module.
    state.invariant_cache = None
    # Clearing the log fires no events, so without this every connected
    # browser keeps rendering the pre-reset world (see Hub.broadcast_reset).
    state.hub.broadcast_reset()
    return {"status": "reset"}


@app.post("/demo/hold/{command_id}")
def demo_hold(command_id: str) -> dict[str, str]:
    _state().simulation.hold(command_id)
    return {"held": command_id}


@app.post("/demo/release/{command_id}")
def demo_release(command_id: str) -> dict[str, str]:
    _state().simulation.release(command_id)
    return {"released": command_id}


@app.post("/demo/confirm/{endpoint_id}")
def demo_confirm(endpoint_id: str) -> dict[str, str]:
    sim = _state().simulation
    if endpoint_id in sim.facilities:
        sim.confirm_ready(endpoint_id, DEMO_TRANSPORT_ID)
    elif endpoint_id in sim.ambulances:
        sim.confirm(endpoint_id)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown endpoint {endpoint_id!r}")
    return {"confirmed": endpoint_id}


@app.post("/demo/fuzz")
def demo_fuzz(background_tasks: BackgroundTasks, runs: int = 20) -> dict[str, int]:
    runs = max(1, min(runs, 500))
    state = _state()
    background_tasks.add_task(_run_fuzz_and_broadcast, state.config, runs, state.hub)
    return {"status_code": 202, "runs": runs}


def _run_fuzz_and_broadcast(config: Config, runs: int, hub: Hub) -> None:
    summary = run_random_fuzz(config, runs)
    hub.broadcast_fuzz(
        {
            "runs": summary.runs,
            "passed": summary.passed,
            "failed": summary.failed,
            "max_local_overlap_ms": summary.max_local_overlap_ms,
            "violations": summary.violations,
        }
    )


# -- AI sidecar (Phase 10 fills in app/ai.py's bodies) ----------------------


@app.post("/ai/explain/{transport_id}")
def ai_explain(transport_id: str) -> dict:
    state = _state()
    _require_transport_seen(state, transport_id)
    return ai.explain(state.store.replay(transport_id))


@app.post("/ai/recommend/{transport_id}")
def ai_recommend(transport_id: str) -> dict:
    state = _state()
    _require_transport_seen(state, transport_id)
    current_epoch = _transport_view(state, transport_id).current_epoch
    raw_position = state.simulation.ambulance_view(transport_id) or {"progress": 0.0, "known_destination": None}
    position = ai.AmbulancePosition(
        progress=raw_position["progress"], known_destination=raw_position["known_destination"]
    )
    # Live free-bed counts, not the static seed literals: the three legacy
    # facilities have no bed model, so fall back to their seeded number and
    # let a capacity-aware hospital of the same name (there is none today,
    # but the map is the right shape) override it.
    free_beds = {
        hospital_id: sum(view["free"].values())
        for hospital_id in state.simulation.hospital_ids()
        for view in [state.simulation.hospital_view(hospital_id)]
        if view is not None
    }
    return ai.recommend(
        transport_id, current_epoch, HOSPITALS, TRANSPORT.patient, position, free_beds=free_beds
    )


class DescribeRequest(BaseModel):
    text: str
    position: tuple[float, float] = (20.0, 20.0)


@app.post("/ai/justify/{transport_id}")
def ai_justify(transport_id: str) -> dict:
    """Why this hospital, and not the others — narrated from the ranking that
    actually produced the decision. Explanatory only: it is handed the choice
    that was already made and never influences it."""
    state = _state()
    _require_transport_seen(state, transport_id)
    patient = state.simulation.dispatcher.patient_of(transport_id)
    if patient is None:
        raise HTTPException(
            status_code=409,
            detail=f"Transport {transport_id!r} has no patient record to explain",
        )
    candidates = [_candidate_view(c) for c in state.simulation.dispatcher.candidates_of(transport_id)]
    chosen = (
        state.simulation.dispatcher.current_destination_of(transport_id)
        or state.simulation.dispatcher.pending_destination_of(transport_id)
    )
    return ai.justify(
        transport_id,
        chosen,
        {
            "acuity": patient.acuity,
            "condition": patient.condition.value,
            "age_group": patient.age_group.value,
            "needs": sorted(n.value for n in patient.needs),
            "override": patient.override,
        },
        candidates,
    )


@app.post("/ai/dispatch")
def ai_dispatch(body: DescribeRequest) -> dict:
    """Describe a patient in plain English and dispatch them.

    The model is a *parser* here, never a decision-maker: it produces a
    patient, every field is validated against the real enums, and the
    deterministic acceptance function decides where that patient may go. A
    hallucinated hospital is impossible because the model is never asked for
    one.
    """
    parsed = ai.parse_patient(body.text)
    if "patient" not in parsed:
        return parsed  # {"error": ...} — surfaced to the caller as-is
    state = _state()
    request = BatchPatientRequest(
        id=f"AI-{int(state.clock.now_ms())}",
        position=body.position,
        **parsed["patient"],
    )
    started = state.simulation.start_batch([(_build_patient(request), body.position)])
    return {"patient": parsed["patient"], "transport_ids": started}


# -- live updates ------------------------------------------------------------


@app.websocket("/events/live")
async def events_live(websocket: WebSocket) -> None:
    state = _state()
    await state.hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything; this just detects disconnects
    except WebSocketDisconnect:
        state.hub.disconnect(websocket)


# -- serving the built frontend (production only) ----------------------------
#
# Registered last, deliberately: this mounts a catch-all at "/", and FastAPI
# matches routes in declaration order, so every API route and the WebSocket
# above already wins. In development nothing is built and this is skipped —
# Vite serves the app and proxies here instead.
#
# Same origin is the whole point: api.js uses relative paths and ws.js derives
# the WebSocket URL from window.location, so serving both from one process
# needs no CORS, no base-URL configuration, and no second deployment.

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str) -> FileResponse:
    """Serve the built SPA, falling back to index.html for client-side routes.

    A path that looks like an API call is 404'd rather than answered with
    index.html: returning HTML with a 200 to a fetch() that expected JSON
    turns a simple "unknown route" into a confusing parse error at the caller.
    """
    if not _FRONTEND_DIST.is_dir():
        raise HTTPException(status_code=404, detail="Frontend is not built (run: npm run build)")

    candidate = (_FRONTEND_DIST / full_path).resolve()
    # Containment check: a crafted path must not read outside dist/.
    if full_path and _FRONTEND_DIST in candidate.parents and candidate.is_file():
        return FileResponse(candidate)

    index = _FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Frontend is not built (run: npm run build)")
    return FileResponse(index)
