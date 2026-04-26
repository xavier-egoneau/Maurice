from __future__ import annotations

from pathlib import Path

from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.workspace import initialize_workspace
from maurice.kernel.events import EventStore
from maurice.kernel.runs import RunStore
from maurice.kernel.scheduler import JobStore, utc_now


def test_monitoring_snapshot_collects_generic_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    event_store = EventStore(workspace / "agents" / "main" / "events.jsonl")
    JobStore(workspace / "agents" / "main" / "jobs.json").schedule(
        name="dreaming.run",
        owner="skill:dreaming",
        run_at=utc_now(),
        payload={"agent_id": "main"},
    )
    RunStore(
        workspace / "agents" / "main" / "runs.json",
        workspace_root=workspace,
        event_store=event_store,
    ).create(
        parent_agent_id="main",
        task="Test snapshot",
        write_scope={"paths": []},
        permission_scope={"classes": []},
    )

    snapshot = build_monitoring_snapshot(workspace, event_limit=5)

    assert snapshot.runtime.workspace_root == str(workspace.resolve())
    assert snapshot.agents[0].id == "main"
    assert snapshot.jobs.total == 1
    assert snapshot.jobs.by_status["scheduled"] == 1
    assert snapshot.runs.total == 1
    assert snapshot.runs.by_status["created"] == 1
    assert any(skill.name == "filesystem" for skill in snapshot.skills)
    assert snapshot.events


def test_read_event_tail_limits_events(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    store = EventStore(workspace / "agents" / "main" / "events.jsonl")
    store.emit(name="first", origin="test", agent_id="main", session_id="default")
    store.emit(name="second", origin="test", agent_id="main", session_id="default")

    events = read_event_tail(workspace, limit=1)

    assert [event.name for event in events] == ["second"]
