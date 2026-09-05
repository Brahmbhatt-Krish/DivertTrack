"""Phase 8: the HTTP + WebSocket API, via FastAPI's TestClient. Each test
gets a fresh app lifespan (and thus a fresh in-memory EventStore/Simulation)
by entering its own `with TestClient(app)` block."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import ai
from app.main import app
from app.seed import TRANSPORT

DEMO_TRANSPORT_ID = TRANSPORT.transport_id


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", ":memory:")
    with TestClient(app) as test_client:
        yield test_client


def test_health_check(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# -- start (R10) -------------------------------------------------------------


def test_start_creates_a_transport_in_starting_status(client: TestClient) -> None:
    response = client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    assert response.status_code == 201
    body = response.json()
    assert body["transport_id"] == "AMB-1"
    assert body["pending_destination"] == "Hospital_A"
    assert body["status"] == "STARTING"


def test_starting_an_already_started_transport_is_409(client: TestClient) -> None:
    # E15
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_B"})
    assert response.status_code == 409


def test_starting_with_an_unknown_destination_is_400(client: TestClient) -> None:
    # E14
    response = client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_Z"})
    assert response.status_code == 400


# -- redirect ----------------------------------------------------------------


def test_redirect_updates_pending_destination(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/transports/AMB-1/redirect", json={"target": "Hospital_B"})
    assert response.status_code == 200
    assert response.json()["pending_destination"] == "Hospital_B"


def test_redirect_on_an_unknown_transport_is_404(client: TestClient) -> None:
    # E14
    response = client.post("/transports/AMB-999/redirect", json={"target": "Hospital_B"})
    assert response.status_code == 404


def test_redirect_to_an_unknown_facility_is_400(client: TestClient) -> None:
    # E14
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/transports/AMB-1/redirect", json={"target": "Hospital_Z"})
    assert response.status_code == 400


# -- views ---------------------------------------------------------------


def test_getting_an_unknown_transport_is_404(client: TestClient) -> None:
    assert client.get("/transports/AMB-999").status_code == 404


def test_get_transport_returns_the_current_view(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.get("/transports/AMB-1")
    assert response.status_code == 200
    assert response.json()["transport_id"] == "AMB-1"


def test_get_transport_facilities_lists_touched_facilities(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.get("/transports/AMB-1/facilities")
    assert response.status_code == 200
    assert "Hospital_A" in response.json()


def test_get_transport_events_lists_only_that_transports_events(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    events = client.get("/transports/AMB-1/events").json()
    assert len(events) > 0
    assert all(e["transport_id"] == "AMB-1" for e in events)


# -- invariant -----------------------------------------------------------


def test_get_transport_invariant_passes_for_a_fresh_transport(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.get("/transports/AMB-1/invariant")
    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["violations"] == []


# -- demo ------------------------------------------------------------------


def test_demo_preset_requires_the_demo_transport_to_exist_first(client: TestClient) -> None:
    assert client.post("/demo/preset/NORMAL").status_code == 404


def test_demo_preset_with_an_unknown_name_is_400(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": DEMO_TRANSPORT_ID, "destination": "Hospital_A"})
    response = client.post("/demo/preset/NOT_A_REAL_PRESET")
    assert response.status_code == 400


def test_reset_clears_events(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    assert len(client.get("/transports/AMB-1/events").json()) > 0

    assert client.post("/demo/reset").status_code == 200

    assert client.get("/transports/AMB-1").status_code == 404


def test_demo_hold_and_release_do_not_error_for_an_unknown_command_id(client: TestClient) -> None:
    assert client.post("/demo/hold/cmd-does-not-exist").status_code == 200
    assert client.post("/demo/release/cmd-does-not-exist").status_code == 200


def test_demo_confirm_on_an_unknown_endpoint_is_404(client: TestClient) -> None:
    assert client.post("/demo/confirm/NotARealEndpoint").status_code == 404


def test_demo_confirm_on_a_known_facility_succeeds(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": DEMO_TRANSPORT_ID, "destination": "Hospital_A"})
    response = client.post("/demo/confirm/Hospital_A")
    assert response.status_code == 200


def test_demo_fuzz_starts_a_background_run(client: TestClient) -> None:
    response = client.post("/demo/fuzz?runs=3")
    assert response.status_code == 200
    assert response.json()["runs"] == 3


# -- AI stubs (Phase 10 fills in the real bodies) ---------------------------


def test_ai_explain_on_an_unknown_transport_is_404(client: TestClient) -> None:
    assert client.post("/ai/explain/AMB-999").status_code == 404


def test_ai_explain_is_unavailable_without_a_configured_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic regardless of the ambient .env: this route's fail-safe
    # contract (no key -> {"error": ...}, never a 500) is what's under test
    # here, not whatever a real Groq call happens to return today.
    monkeypatch.setattr(ai, "settings", dataclasses.replace(ai.settings, groq_api_key=""))
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/ai/explain/AMB-1")
    assert response.status_code == 200
    assert response.json() == {"error": "AI unavailable"}


def test_ai_recommend_is_unavailable_without_a_configured_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "settings", dataclasses.replace(ai.settings, groq_api_key=""))
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/ai/recommend/AMB-1")
    assert response.status_code == 200
    assert response.json() == {"error": "AI unavailable"}


# -- WebSocket ---------------------------------------------------------------


def _batch_kinds(websocket) -> list[str]:
    """The hub coalesces a flush window into one {"kind": "batch"} message
    (see ws.py) — this unwraps it to the kinds it carries."""
    message = websocket.receive_json()
    assert message["kind"] == "batch"
    return [sub["kind"] for sub in message["messages"]]


def test_websocket_receives_at_least_one_message_after_a_redirect(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    with client.websocket_connect("/events/live") as websocket:
        client.post("/transports/AMB-1/redirect", json={"target": "Hospital_B"})
        kinds = _batch_kinds(websocket)
        assert any(kind in ("event", "transport", "facility", "invariant", "in_flight") for kind in kinds)


# -- Phase 19: multi-hospital capacity extension routes ----------------------


def test_list_hospitals_returns_all_six(client: TestClient) -> None:
    response = client.get("/hospitals")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 6
    assert {"id", "beds_total", "free", "load", "diversion"} <= body[0].keys()


def test_batch_start_creates_the_requested_transports(client: TestClient) -> None:
    response = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 3, "condition": "GENERAL", "position": [5.0, 5.0]}]},
    )
    assert response.status_code == 201
    assert len(response.json()["transport_ids"]) == 1


def test_redirect_returns_409_not_eligible_for_a_capacity_aware_transport(client: TestClient) -> None:
    client.post(
        "/hospitals/Hospital_2/beds", json={"bed_type": "ICU", "total": 0}
    )
    started = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [30.0, 8.0]}]},
    ).json()["transport_ids"][0]
    response = client.post(f"/transports/{started}/redirect", json={"target": "Hospital_2"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "not_eligible"


def test_candidates_route_lists_reasons_for_excluded_hospitals(client: TestClient) -> None:
    started = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "CARDIAC", "position": [8.0, 8.0]}]},
    ).json()["transport_ids"][0]
    response = client.get(f"/transports/{started}/candidates")
    assert response.status_code == 200
    assert any(c["reason"] is not None for c in response.json())


def test_policy_switch(client: TestClient) -> None:
    response = client.post("/policy", json={"mode": "auto"})
    assert response.status_code == 200
    assert response.json()["policy"] == "auto"


def test_global_invariant_route(client: TestClient) -> None:
    response = client.get("/invariant")
    assert response.status_code == 200
    assert "capacity_warnings" in response.json()


def test_websocket_receives_a_hospital_message_after_beds_report(client: TestClient) -> None:
    # One BedsReported event fans out to several broadcast kinds ("event"
    # first, always) — search a small, bounded window rather than assuming
    # "hospital" is the very first message.
    with client.websocket_connect("/events/live") as websocket:
        client.post("/hospitals/Hospital_1/beds", json={"bed_type": "ICU", "total": 3})
        assert "hospital" in _batch_kinds(websocket)


def test_websocket_receives_an_alert_after_every_candidate_declines(client: TestClient) -> None:
    # A patient placed absurdly far from every hospital fails the transport-
    # time window everywhere, so start_batch finds zero candidates and logs
    # NoAcceptingFacility immediately (no real network delay to wait out).
    with client.websocket_connect("/events/live") as websocket:
        client.post(
            "/transports/batch",
            json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [10000.0, 10000.0]}]},
        )
        assert "alert" in _batch_kinds(websocket)


def test_reset_broadcasts_a_wipe_and_fresh_hospital_views(client: TestClient) -> None:
    """/demo/reset clears the event log without appending anything, so the
    hub has nothing to react to. Before this was fixed, a connected browser
    kept rendering the pre-reset world forever — most visibly, the bed
    capacity bars stayed frozen at their old counts."""
    # Take real beds first, so a wipe is actually observable.
    client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    )
    assert any(
        free < total
        for hospital in client.get("/hospitals").json()
        for total, free in [(hospital["beds_total"]["ICU"], hospital["free"]["ICU"])]
        if "ICU" in hospital["beds_total"]
    ), "expected the batch above to hold at least one ICU bed"

    with client.websocket_connect("/events/live") as websocket:
        client.post("/demo/reset")
        message = websocket.receive_json()

    assert message["kind"] == "batch"
    kinds = [sub["kind"] for sub in message["messages"]]
    # Order matters: the client folds these in sequence, so a "reset" landing
    # after the fresh views would blank them right back out.
    assert kinds[0] == "reset"
    hospitals = [sub for sub in message["messages"] if sub["kind"] == "hospital"]
    assert len(hospitals) == 6, "every hospital's post-reset view must be re-sent"
    for hospital in hospitals:
        view = hospital["view"]
        assert view["free"] == view["beds_total"], f"{hospital['hospital_id']} still holds beds after reset"


def test_batch_start_rejects_an_unknown_enum_with_400_not_500(client: TestClient) -> None:
    """A malformed condition used to escape as a bare ValueError, which
    FastAPI turned into a 500 with a traceback rather than a 400 naming the
    offending field."""
    response = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 3, "condition": "NOT_A_CONDITION", "position": [5.0, 5.0]}]},
    )
    assert response.status_code == 400
    assert "NOT_A_CONDITION" in response.json()["detail"]


def test_websocket_pushes_the_transport_list_so_the_table_stays_live(client: TestClient) -> None:
    """The transports table (which ambulance is heading to which hospital)
    could previously only be refreshed by hand — the hub pushed per-transport
    projections but never the row shape the table renders, so the mapping went
    stale on every redirect."""
    with client.websocket_connect("/events/live") as websocket:
        client.post(
            "/transports/batch",
            json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
        )
        message = websocket.receive_json()

    pushed = [sub for sub in message["messages"] if sub["kind"] == "transport_list"]
    assert pushed, "expected a transport_list push"
    rows = pushed[-1]["rows"]
    assert any(row["transport_id"].startswith("BATCH-P1") for row in rows)
    assert {"transport_id", "current_destination", "pending_destination", "status", "position"} <= rows[0].keys()


def test_reset_pushes_an_empty_transport_list(client: TestClient) -> None:
    client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    )
    with client.websocket_connect("/events/live") as websocket:
        client.post("/demo/reset")
        message = websocket.receive_json()

    pushed = [sub for sub in message["messages"] if sub["kind"] == "transport_list"]
    assert pushed and pushed[-1]["rows"] == [], "the table must empty itself on reset"


def test_hospital_view_names_which_transports_hold_which_beds(client: TestClient) -> None:
    """A bar reading 4/6 says nothing about whose beds those are; `holders`
    is the reverse lookup the ledger already had but never exposed."""
    started = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    ).json()["transport_ids"][0]

    holding = {
        hospital["id"]: bed_type
        for hospital in client.get("/hospitals").json()
        for bed_type, holders in hospital["holders"].items()
        if started in holders["reserved"]
    }
    assert holding, f"{started} reserved a bed but no hospital lists it as a holder"
    # A reservation is not an arrival: it must show as reserved, never occupied.
    for hospital in client.get("/hospitals").json():
        for holders in hospital["holders"].values():
            assert started not in holders["occupied"]


def test_transport_list_route_and_hub_push_return_the_same_shape(client: TestClient) -> None:
    """Both read Simulation.transport_list() — this is what stops the route
    and the live push from drifting apart again."""
    client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    )
    with client.websocket_connect("/events/live") as websocket:
        client.post("/hospitals/Hospital_1/beds", json={"bed_type": "ICU", "total": 4})
        client.post(
            "/transports/batch",
            json={"patients": [{"id": "P2", "acuity": 3, "condition": "GENERAL", "position": [9.0, 9.0]}]},
        )
        pushed = None
        while pushed is None:
            message = websocket.receive_json()
            for sub in message["messages"]:
                if sub["kind"] == "transport_list":
                    pushed = sub["rows"]

    route_ids = {row["transport_id"] for row in client.get("/transports").json()}
    assert {row["transport_id"] for row in pushed} <= route_ids
    assert pushed[0].keys() == client.get("/transports").json()[0].keys()


def test_redirect_without_a_target_is_400_under_manual_policy_not_500(client: TestClient) -> None:
    """Omitting the target asks the dispatcher to choose, which only AUTO
    policy allows. Under MANUAL the dispatcher raises, and that used to escape
    as an unhandled 500 — the UI's "Re-plan" button hit it every time."""
    client.post("/policy", json={"mode": "manual"})
    started = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    ).json()["transport_ids"][0]
    response = client.post(f"/transports/{started}/redirect", json={"target": None})
    assert response.status_code == 400
    assert "target is required" in response.json()["detail"]


def test_redirect_without_a_target_is_allowed_under_auto_policy(client: TestClient) -> None:
    client.post("/policy", json={"mode": "auto"})
    started = client.post(
        "/transports/batch",
        json={"patients": [{"id": "P1", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}]},
    ).json()["transport_ids"][0]
    response = client.post(f"/transports/{started}/redirect", json={"target": None})
    assert response.status_code == 200


def test_policy_can_be_read_back(client: TestClient) -> None:
    """POST /policy had no GET counterpart, so a client could set the mode but
    never ask what it was — which is exactly what the Re-plan button needs."""
    assert client.get("/policy").json()["policy"] == "manual"
    client.post("/policy", json={"mode": "auto"})
    assert client.get("/policy").json()["policy"] == "auto"


def test_redirecting_a_stranded_transport_does_not_blow_up(client: TestClient) -> None:
    """A transport that gave up (no current destination *and* no pending one)
    is exactly what an operator wants to re-plan. Doing so used to crash:
    _redirect_before_first_activation_capacity_aware sent a WITHDRAW to the
    abandoned candidate without checking there was one, so Command's
    target_facility got None and Pydantic rejected it."""
    # One hospital, one ICU bed, and more acuity-2 patients than beds: the
    # losers end up stranded with nothing reserved anywhere.
    for hospital_id in ("Hospital_1", "Hospital_2", "Hospital_4", "Hospital_5", "Hospital_6"):
        client.post(f"/hospitals/{hospital_id}/beds", json={"bed_type": "ICU", "total": 0})
    client.post(
        "/transports/batch",
        json={
            "patients": [
                {"id": f"P{i}", "acuity": 2, "condition": "GENERAL", "position": [8.0, 8.0]}
                for i in range(4)
            ]
        },
    )
    stranded = [
        row
        for row in client.get("/transports").json()
        if row["current_destination"] is None and row["pending_destination"] is None
    ]
    assert stranded, "expected at least one transport with nowhere to go"

    # Give it somewhere to go, then re-plan it there.
    client.post("/hospitals/Hospital_1/beds", json={"bed_type": "ICU", "total": 2})
    response = client.post(
        f"/transports/{stranded[0]['transport_id']}/redirect", json={"target": "Hospital_1"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["transport"]["pending_destination"] == "Hospital_1"


def test_a_dispatcher_crash_is_not_reported_as_a_client_error(client: TestClient) -> None:
    """Pydantic's ValidationError subclasses ValueError, so the manual-policy
    handler must catch the dispatcher's own TargetRequired specifically —
    otherwise an internal fault comes back as a 400 and looks like the
    operator's fault."""
    from app.dispatcher import NotEligible, TargetRequired

    assert issubclass(TargetRequired, ValueError)
    assert not issubclass(NotEligible, TargetRequired)


# -- Phase 21: hospital roster CRUD -----------------------------------------


def test_hospitals_can_be_added_and_retired_over_the_api(client: TestClient) -> None:
    body = {
        "id": "Hospital_9", "name": "Lakeside General", "location": [12.0, 20.0],
        "beds_total": {"GENERAL": 8, "ICU": 3},
        "capabilities": ["CATH_LAB", "CT_SCAN"], "ventilators_total": 2,
    }
    created = client.post("/hospitals", json=body)
    assert created.status_code == 201
    assert created.json()["free"] == {"GENERAL": 8, "ICU": 3}
    assert "Hospital_9" in [h["id"] for h in client.get("/hospitals").json()]

    # Duplicate ids are a conflict, not a silent overwrite.
    assert client.post("/hospitals", json=body).status_code == 409

    renamed = client.put("/hospitals/Hospital_9", json={**body, "name": "Lakeside Trauma"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Lakeside Trauma"

    removed = client.delete("/hospitals/Hospital_9")
    assert removed.status_code == 200
    assert "Hospital_9" not in removed.json()["remaining"]
    assert "Hospital_9" not in [h["id"] for h in client.get("/hospitals").json()]
    assert client.delete("/hospitals/Hospital_9").status_code == 404


def test_registering_a_hospital_with_an_unknown_bed_type_is_400(client: TestClient) -> None:
    response = client.post(
        "/hospitals",
        json={
            "id": "Hospital_9", "name": "X", "location": [1.0, 1.0],
            "beds_total": {"NOT_A_BED_TYPE": 3}, "capabilities": [], "ventilators_total": 0,
        },
    )
    assert response.status_code == 400


def test_the_seeded_roster_is_bootstrapped_as_events_not_config(client: TestClient) -> None:
    """seed.MULTI_HOSPITALS is now a bootstrap, not a source of truth: the six
    hospitals must be in the log as HospitalRegistered events, so the whole
    network is reconstructible by replay."""
    client.get("/hospitals")  # force app startup
    registered = [
        event
        for event in client.get("/transports/Hospital_1/events").json()
        if event["type"] == "HospitalRegistered"
    ]
    assert registered, "the seeded roster must be written to the log"
    assert registered[0]["payload"]["id"] == "Hospital_1"


def test_decommissioning_is_visible_on_the_websocket_as_a_roster_push(client: TestClient) -> None:
    with client.websocket_connect("/events/live") as websocket:
        client.delete("/hospitals/Hospital_3")
        message = websocket.receive_json()
    rosters = [sub for sub in message["messages"] if sub["kind"] == "roster"]
    assert rosters, "a roster change must push the whole roster"
    assert "Hospital_3" not in [h["id"] for h in rosters[-1]["hospitals"]]
