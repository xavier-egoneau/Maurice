from __future__ import annotations

import json

from maurice.kernel.events import EventStore
from maurice.kernel.session import SessionStore, new_correlation_id


def test_event_store_appends_and_filters_jsonl_events(tmp_path) -> None:
    store = EventStore(tmp_path / "agents" / "main" / "events.jsonl")
    correlation_id = new_correlation_id("turn")

    store.emit(
        name="turn.started",
        kind="progress",
        origin="kernel",
        agent_id="main",
        session_id="sess_1",
        correlation_id=correlation_id,
        payload={"message": "hello"},
    )
    store.emit(
        name="tool.completed",
        origin="skill:filesystem",
        agent_id="main",
        session_id="sess_1",
        correlation_id=correlation_id,
        payload={"tool": "filesystem.read"},
    )
    store.emit(
        name="turn.started",
        origin="kernel",
        agent_id="coding",
        session_id="sess_2",
    )

    events = store.read_all(agent_id="main", correlation_id=correlation_id)

    assert [event.name for event in events] == ["turn.started", "tool.completed"]
    assert all(event.correlation_id == correlation_id for event in events)


def test_event_store_returns_empty_list_for_missing_stream(tmp_path) -> None:
    assert EventStore(tmp_path / "missing.jsonl").read_all() == []


def test_session_store_persists_messages_and_turns(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create("main", session_id="sess_1")
    turn = store.start_turn("main", session.id)

    store.append_message(
        "main",
        session.id,
        role="user",
        content="Salut Maurice",
        correlation_id=turn.correlation_id,
    )
    store.complete_turn("main", session.id, turn.correlation_id)

    loaded = store.load("main", session.id)

    assert loaded.messages[0].content == "Salut Maurice"
    assert loaded.messages[0].correlation_id == turn.correlation_id
    assert loaded.turns[0].status == "completed"


def test_session_store_lists_sessions_newest_first(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create("main", session_id="first")
    second = store.create("main", session_id="second")
    store.append_message("main", "first", role="user", content="hello")

    sessions = store.list("main")

    assert [session.id for session in sessions] == ["first", "second"]
    assert sessions[0].updated_at >= second.updated_at


def test_session_store_load_normalizes_legacy_tool_call_messages(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create("main", session_id="legacy")
    path = store.session_path("main", "legacy")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["messages"] = [
        {
            "role": "assistant",
            "content": "",
            "created_at": "2024-01-01T00:00:00Z",
            "correlation_id": "turn_1",
            "metadata": {
                "tool_call": True,
                "tool_call_id": "call_1",
                "tool_name": "filesystem.read",
                "tool_arguments": {"path": "notes.md"},
            },
        },
        {
            "role": "user",
            "content": "read",
            "created_at": "2024-01-01T00:00:01Z",
            "correlation_id": "turn_1",
            "metadata": {},
        },
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load("main", "legacy")
    store.save(loaded)
    reloaded = store.load("main", "legacy")

    assert reloaded.messages[0].role == "tool_call"
    assert reloaded.messages[0].metadata == {
        "tool_call_id": "call_1",
        "tool_name": "filesystem.read",
        "tool_arguments": {"path": "notes.md"},
    }
    assert "tool_call\": true" not in path.read_text(encoding="utf-8")


def test_session_reset_keeps_skill_storage_untouched(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create("main", session_id="sess_1")
    turn = store.start_turn("main", session.id)
    store.append_message(
        "main",
        session.id,
        role="assistant",
        content="ok",
        correlation_id=turn.correlation_id,
    )

    skill_storage = tmp_path / "skills" / "memory" / "memory.sqlite"
    skill_storage.parent.mkdir(parents=True)
    skill_storage.write_text("memory data", encoding="utf-8")

    reset = store.reset("main", session.id)

    assert reset.messages == []
    assert reset.turns == []
    assert skill_storage.read_text(encoding="utf-8") == "memory data"
