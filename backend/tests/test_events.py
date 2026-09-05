"""Phase 1: EventStore append/replay/subscribe, and restart-safe persistence."""
import pytest

from app.events import Event, EventStore, EventType


def _transport_started(transport_id: str = "AMB-101", ts_ms: int = 0) -> Event:
    return Event(
        transport_id=transport_id,
        epoch=1,
        ts_ms=ts_ms,
        type=EventType.TRANSPORT_STARTED,
        facility_id=None,
        payload={"destination": "Hospital_A"},
    )


def test_append_assigns_an_increasing_seq_and_returns_a_persisted_copy(event_store: EventStore) -> None:
    first = event_store.append(_transport_started())
    second = event_store.append(_transport_started(ts_ms=10))

    assert first.seq == 1
    assert second.seq == 2


def test_append_rejects_an_already_persisted_event(event_store: EventStore) -> None:
    persisted = event_store.append(_transport_started())

    with pytest.raises(ValueError):
        event_store.append(persisted)


def test_replay_returns_events_in_seq_order(event_store: EventStore) -> None:
    event_store.append(_transport_started("AMB-101", ts_ms=0))
    event_store.append(_transport_started("AMB-101", ts_ms=10))
    event_store.append(_transport_started("AMB-101", ts_ms=20))

    replayed = event_store.replay()

    assert [e.ts_ms for e in replayed] == [0, 10, 20]
    assert [e.seq for e in replayed] == [1, 2, 3]


def test_replay_filters_by_transport_id(event_store: EventStore) -> None:
    event_store.append(_transport_started("AMB-101"))
    event_store.append(_transport_started("AMB-202"))
    event_store.append(_transport_started("AMB-101", ts_ms=10))

    replayed = event_store.replay("AMB-101")

    assert [e.transport_id for e in replayed] == ["AMB-101", "AMB-101"]


def test_payload_round_trips_through_json(event_store: EventStore) -> None:
    original = Event(
        transport_id="AMB-101",
        epoch=2,
        ts_ms=5,
        type=EventType.CUTOVER_SCHEDULED,
        facility_id="Hospital_B",
        payload={"cutover_at": 4500, "targets": ["Hospital_A", "Hospital_B"], "nested": {"ok": True}},
    )

    event_store.append(original)
    replayed = event_store.replay()[0]

    assert replayed.payload == original.payload
    assert replayed.type is EventType.CUTOVER_SCHEDULED


def test_subscribe_receives_each_appended_event_immediately(event_store: EventStore) -> None:
    received: list[Event] = []
    event_store.subscribe(received.append)

    event_store.append(_transport_started())
    event_store.append(_transport_started(ts_ms=10))

    assert len(received) == 2
    assert received[0].seq == 1
    assert received[1].seq == 2


def test_subscribers_are_notified_in_registration_order(event_store: EventStore) -> None:
    calls: list[str] = []
    event_store.subscribe(lambda e: calls.append("first"))
    event_store.subscribe(lambda e: calls.append("second"))

    event_store.append(_transport_started())

    assert calls == ["first", "second"]


def test_restart_re_reads_a_file_backed_database(tmp_path) -> None:
    db_path = str(tmp_path / "diverttrack_test.db")

    first_run = EventStore(db_path)
    first_run.append(_transport_started("AMB-101"))
    first_run.append(_transport_started("AMB-101", ts_ms=10))

    second_run = EventStore(db_path)
    replayed = second_run.replay("AMB-101")

    assert [e.ts_ms for e in replayed] == [0, 10]
    assert [e.seq for e in replayed] == [1, 2]
