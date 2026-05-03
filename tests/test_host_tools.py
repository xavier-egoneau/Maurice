from __future__ import annotations

from maurice.host.workspace import initialize_workspace
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.host.paths import kernel_config_path
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file
from maurice.system_skills.host.tools import (
    agent_create,
    agent_delete,
    agent_list,
    agent_update,
    logs,
    status,
    subagent_run_create,
    subagent_template_create,
    subagent_template_list,
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


def test_host_subagent_template_tools_create_template_and_run(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel_data["kernel"]["models"]["entries"]["ollama_gemma4"] = {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "gemma4",
        "base_url": "http://localhost:11434",
        "credential": "ollama",
        "tier": "middle",
        "capabilities": ["text", "tools"],
        "privacy": "local",
    }
    write_yaml_file(kernel_path, kernel_data)
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    created = subagent_template_create(
        {
            "template_id": "coder",
            "description": "Coding worker",
            "permission_profile": "safe",
            "skills": ["filesystem"],
            "credentials": ["ollama"],
            "model_chain": ["ollama_gemma4"],
        },
        context,
    )
    listed = subagent_template_list({}, context)
    run = subagent_run_create(
        {
            "task": "Run tests.",
            "template_id": "coder",
            "write_paths": ["$workspace/runs/**"],
            "permission_classes": ["fs.read"],
        },
        context,
        agent_id="main",
    )

    assert created.ok is True
    assert listed.data["templates"][0]["model_chain"] == ["ollama_gemma4"]
    assert run.ok is True
    assert run.data["template"]["model_chain"] == ["ollama_gemma4"]
    assert (workspace / "runs" / run.data["run"]["id"] / "mission.json").is_file()


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
