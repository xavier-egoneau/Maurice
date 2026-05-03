from __future__ import annotations

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.memory.tools import remember
from maurice.system_skills.workspace_dreaming.tools import build_dream_input


def context(tmp_path, *, agent_id: str = "main") -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    agent_workspace = workspace / "agents" / agent_id
    agent_workspace.mkdir(parents=True, exist_ok=True)
    return PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(agent_workspace),
    )


def test_workspace_dreaming_reads_recent_memory_from_all_agents(tmp_path) -> None:
    main_context = context(tmp_path, agent_id="main")
    paul_context = context(tmp_path, agent_id="paul")
    remember({"content": "Main works on Maurice runtime.", "tags": ["project"]}, main_context)
    remember({"content": "Paul prepares onboarding notes.", "tags": ["notes"]}, paul_context)

    dream_input = build_dream_input(main_context)

    signals = {signal.data["agent_id"]: signal for signal in dream_input.signals}
    assert dream_input.skill == "workspace_dreaming"
    assert "main" in signals
    assert "paul" in signals
    assert "Maurice runtime" in signals["main"].summary
    assert "onboarding notes" in signals["paul"].summary
