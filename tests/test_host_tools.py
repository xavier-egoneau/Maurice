from __future__ import annotations

from maurice.host.workspace import initialize_workspace
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.host.tools import logs, status


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
