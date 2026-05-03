from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime, timedelta
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

    assert snapshot.logs[-1].message == "job.failed - boom"
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
    assert snapshot.models[0].model == "ollama/llama3.2"


def test_dashboard_hides_terminal_one_shot_automations(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    store = JobStore(workspace / "agents" / "main" / "jobs.json")
    failed = store.schedule(
        name="reminders.fire",
        owner="skill:reminders",
        run_at=datetime.now(UTC),
        payload={"agent_id": "main", "session_id": "reminders"},
    )
    cancelled = store.schedule(
        name="reminders.fire",
        owner="skill:reminders",
        run_at=datetime.now(UTC),
        payload={"agent_id": "main", "session_id": "reminders"},
    )
    recurring = store.schedule(
        name="daily.digest",
        owner="skill:daily",
        run_at=datetime.now(UTC),
        interval_seconds=86400,
        payload={"agent_id": "main", "session_id": "daily"},
    )
    store.fail(failed.id, "boom")
    store.cancel(cancelled.id)

    snapshot = build_dashboard_snapshot(workspace)

    assert [row.job_id for row in snapshot.automations] == [recurring.id]


def test_dashboard_lists_dev_worker_runs_as_sessions(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    worker = workspace / "agents" / "main" / "runs" / "dev_workers" / "devw_1"
    worker.mkdir(parents=True)
    worker.joinpath("status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "task": "Explorer le projet",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    snapshot = build_dashboard_snapshot(workspace)

    row = next(item for item in snapshot.sessions if item.session_id == "worker:devw_1")
    assert row.origin == "Worker dev"
    assert row.status == "actif"
    assert row.last_event == "Explorer le projet"


def test_dashboard_log_rows_include_payload_details(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    EventStore(workspace / "agents" / "main" / "events.jsonl").emit(
        name="channel.poll.failed",
        origin="host.channel.telegram",
        agent_id="main",
        session_id="telegram",
        payload={"error": "Conflict: terminated by other getUpdates request"},
    )

    snapshot = build_dashboard_snapshot(workspace)

    assert snapshot.logs[-1].level == "error"
    assert "Conflict" in snapshot.logs[-1].message


def test_dashboard_keeps_scheduled_one_shot_automation_visible(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = Path(__file__).resolve().parents[1]
    initialize_workspace(workspace, runtime)
    store = JobStore(workspace / "agents" / "main" / "jobs.json")
    reminder = store.schedule(
        name="reminders.fire",
        owner="skill:reminders",
        run_at=datetime.now(UTC) + timedelta(hours=1),
        payload={"agent_id": "main", "session_id": "reminders"},
    )

    snapshot = build_dashboard_snapshot(workspace)

    assert [row.job_id for row in snapshot.automations] == [reminder.id]
