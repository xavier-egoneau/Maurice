from __future__ import annotations

from maurice.host.workspace import initialize_workspace
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.config import load_workspace_config
from maurice.system_skills.host.tools import (
    agent_create,
    agent_delete,
    agent_list,
    agent_update,
    logs,
    status,
    telegram_bind,
)


def test_host_status_tool_reports_workspace_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    result = status({}, context)

    assert result.ok is True
    assert result.data["ok"] is True
    assert {"name": "default_agent", "state": "ok", "summary": "main"} in result.data["checks"]


def test_host_logs_tool_reads_recent_agent_events(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    EventStore(workspace / "agents" / "main" / "events.jsonl").emit(
        name="test.event",
        origin="test",
        agent_id="main",
        session_id="default",
    )
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    result = logs({"limit": 1}, context)

    assert result.ok is True
    assert result.summary == "Read 1 host log event(s)."
    assert result.data["events"][0]["name"] == "test.event"


def test_host_agent_tools_manage_durable_agents(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    created = agent_create(
        {
            "agent_id": "coding",
            "permission_profile": "safe",
            "skills": ["filesystem", "memory"],
            "credentials": ["llm"],
            "channels": [],
        },
        context,
    )
    listed = agent_list({}, context)
    updated = agent_update(
        {
            "agent_id": "coding",
            "permission_profile": "limited",
            "skills": ["filesystem"],
            "credentials": ["llm"],
            "channels": [],
        },
        context,
    )
    deleted = agent_delete({"agent_id": "coding"}, context)

    assert created.ok is True
    assert created.data["agent"]["id"] == "coding"
    assert any(agent["id"] == "coding" for agent in listed.data["agents"])
    assert updated.ok is True
    assert updated.data["agent"]["permission_profile"] == "limited"
    assert deleted.ok is True


def test_host_telegram_bind_connects_existing_bot_to_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    agent_create({"agent_id": "paul", "permission_profile": "safe"}, context)

    result = telegram_bind(
        {"agent_id": "paul", "credential": "telegram_bot", "allowed_users": [123]},
        context,
    )

    bundle = load_workspace_config(workspace)
    assert result.ok is True
    assert bundle.host.channels["telegram"]["agent"] == "paul"
    assert bundle.host.channels["telegram"]["allowed_users"] == [123]
    assert bundle.agents.agents["paul"].channels == ["telegram"]
    assert "telegram" not in bundle.agents.agents["main"].channels
