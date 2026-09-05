"""FastAPI entrypoint. One demo transport (seed.TRANSPORT) drives the /demo/*
routes; /transports/* is the general multi-transport API (E18). All shared
state lives on app.state.app_state, replaced wholesale by /demo/reset rather
than mutated piecemeal.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app import ai
from app.checker import CheckResult, check
from app.clock import RealClock
from app.config import Config
from app.events import Event, EventStore
from app.presets import ALL_PRESETS
from app.projection import FacilityView, TransportView
from app.seed import HOSPITALS, TRANSPORT
from app.simulation import Simulation, run_random_fuzz
from app.ws import Hub

DEMO_TRANSPORT_ID = TRANSPORT.transport_id
KNOWN_FACILITIES = frozenset(h.facility_id for h in HOSPITALS)


class AppState:
    """Everything the routes need. /demo/reset replaces this object
    wholesale (a fresh EventStore + Simulation) rather than mutating one in
    place, so nothing can accidentally hold a stale reference across a reset
    except the Hub, which is told explicitly (rebind_in_flight_source)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = EventStore(config.db_path)
        self.clock = RealClock()
        self.simulation = Simulation(self.clock, self.store, config)
        self.hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Config.from_env() is called here, at startup, rather than relying on
    # the module-level `settings` singleton imported elsewhere — that lets
    # a test set DB_PATH before the app starts and have it actually take.
    state = AppState(Config.from_env())
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
    target: str


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


@app.post("/transports/{transport_id}/redirect")
def redirect_transport(transport_id: str, body: RedirectRequest) -> TransportView:
    state = _state()
    _require_redirectable(state, transport_id)
    if body.target not in KNOWN_FACILITIES:
        raise HTTPException(status_code=400, detail=f"Unknown facility {body.target!r}")
    state.simulation.redirect(transport_id, body.target)
    return _transport_view(state, transport_id)


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
def demo_preset(name: str) -> dict[str, str]:
    state = _state()
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
    state.hub.rebind_in_flight_source(state.simulation)
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
    return ai.recommend(list(HOSPITALS), {}, {})


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
