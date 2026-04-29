from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
from maurice.host.dashboard import build_dashboard_snapshot
from maurice.host.paths import agents_config_path
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import read_yaml_file, write_yaml_file
from maurice.kernel.events import EventStore
from maurice.kernel.scheduler import JobStore, utc_now
from maurice.kernel.session import SessionStore


def test_dashboard_snapshot_uses_user_facing_generic_rows(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    EventStore(workspace / "agents" / "main" / "events.jsonl").emit(
        name="turn.started",
        origin="kernel.loop",
        agent_id="main",
        session_id="default",
    )
    SessionStore(workspace / "sessions").create("main", session_id="default")
    JobStore(workspace / "agents" / "main" / "jobs.json").schedule(
        name="dreaming.run",
        owner="skill:dreaming",
        run_at=utc_now(),
        interval_seconds=3600,
        payload={"agent_id": "main", "session_id": "dreaming"},
    )

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.status.service == "127.0.0.1:18791"
    assert snapshot.status.automatismes == "actifs"
    assert snapshot.agents[0].label == "* main"
    assert snapshot.agents[0].status == "occupe"
    assert snapshot.automations[0].name == "dreaming.run"
    assert snapshot.automations[0].owner_agent == "main"
    assert snapshot.automations[0].enabled is True
    assert snapshot.sessions[0].session_id == "default"
    assert snapshot.sessions[0].origin == "Terminal"
    assert any(skill.name == "filesystem" and skill.source == "system" for skill in snapshot.skills)
    assert snapshot.logs[-1].level == "info"


def test_dashboard_snapshot_marks_errors_for_journal(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    EventStore(workspace / "agents" / "main" / "events.jsonl").emit(
        name="job.failed",
        origin="kernel.scheduler",
        agent_id="main",
        session_id="scheduler",
        payload={"error": "boom"},
    )

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.logs[-1].message == "job.failed"
    assert snapshot.logs[-1].level == "error"


def test_dashboard_hides_completed_one_shot_automations(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    store = JobStore(workspace / "agents" / "main" / "jobs.json")
    job = store.schedule(
        name="reminders.fire",
        owner="skill:reminders",
        run_at=datetime.now(UTC),
        payload={"agent_id": "main", "session_id": "reminders"},
    )
    store.complete(job.id)

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.automations == []


def test_dashboard_keeps_recent_agent_activity_visible(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    store = EventStore(workspace / "agents" / "main" / "events.jsonl")
    store.emit(
        name="turn.started",
        origin="kernel.loop",
        agent_id="main",
        session_id="telegram:42",
    )
    store.emit(
        name="turn.completed",
        origin="kernel.loop",
        agent_id="main",
        session_id="telegram:42",
    )

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.agents[0].status == "occupe"


def test_dashboard_models_use_agent_override(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    agents_path = agents_config_path(workspace)
    agents_data = read_yaml_file(agents_path)
    agents_data["agents"]["main"]["model"] = {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "llama3.2",
        "base_url": "http://localhost:11434",
        "credential": None,
    }
    write_yaml_file(agents_path, agents_data)

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.models[0].provider == "ollama"
    assert snapshot.models[0].model == "ollama/ollama_chat"
