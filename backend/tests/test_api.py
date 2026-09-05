"""Phase 8: the HTTP + WebSocket API, via FastAPI's TestClient. Each test
gets a fresh app lifespan (and thus a fresh in-memory EventStore/Simulation)
by entering its own `with TestClient(app)` block."""
import pytest
from fastapi.testclient import TestClient

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


def test_ai_explain_returns_the_unavailable_stub(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/ai/explain/AMB-1")
    assert response.status_code == 200
    assert response.json() == {"error": "AI unavailable"}


def test_ai_recommend_returns_the_unavailable_stub(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    response = client.post("/ai/recommend/AMB-1")
    assert response.status_code == 200
    assert response.json() == {"error": "AI unavailable"}


# -- WebSocket ---------------------------------------------------------------


def test_websocket_receives_at_least_one_message_after_a_redirect(client: TestClient) -> None:
    client.post("/transports", json={"transport_id": "AMB-1", "destination": "Hospital_A"})
    with client.websocket_connect("/events/live") as websocket:
        client.post("/transports/AMB-1/redirect", json={"target": "Hospital_B"})
        message = websocket.receive_json()
        assert message["kind"] in ("event", "transport", "facility", "invariant", "in_flight")
