from __future__ import annotations

import json

from maurice.host.project_registry import record_seen_project
from maurice.host.runtime import run_one_turn
from maurice.host.workspace import initialize_workspace


def test_natural_project_switch_sets_active_project_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "workspace"
    project = tmp_path / "test_deepssek_v4"
    project.mkdir()
    initialize_workspace(workspace, tmp_path / "runtime", permission_profile="limited")
    record_seen_project(workspace / "agents" / "main", project)
    captured = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs) -> None:
            captured["active_project_root"] = kwargs["permission_context"].active_project_root
            captured["system_prompt"] = kwargs["system_prompt"]

        def run_turn(self, **kwargs):
            captured["message"] = kwargs["message"]
            return None

    monkeypatch.setattr("maurice.host.runtime.AgentLoop", FakeAgentLoop)

    run_one_turn(
        workspace_root=workspace,
        message="ok mets toi sur le projet test_deepssek_v4",
        session_id="default",
        agent_id="main",
    )

    assert captured["active_project_root"] == str(project.resolve())
    assert f"Active project root: {project.resolve()}" in captured["system_prompt"]
    state = json.loads((workspace / "agents" / "main" / ".dev_state.json").read_text(encoding="utf-8"))
    assert state["active_project_path"] == str(project.resolve())


def test_natural_project_reference_can_use_fuzzy_known_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "workspace"
    project = tmp_path / "test_deepssek_v4"
    project.mkdir()
    initialize_workspace(workspace, tmp_path / "runtime", permission_profile="limited")
    record_seen_project(workspace / "agents" / "main", project)
    captured = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs) -> None:
            captured["active_project_root"] = kwargs["permission_context"].active_project_root

        def run_turn(self, **_kwargs):
            return None

    monkeypatch.setattr("maurice.host.runtime.AgentLoop", FakeAgentLoop)

    run_one_turn(
        workspace_root=workspace,
        message="fais une critique du projet deepseek test v4 et utilise un worker",
        session_id="default",
        agent_id="main",
    )

    assert captured["active_project_root"] == str(project.resolve())
