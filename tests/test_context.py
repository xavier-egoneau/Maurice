from __future__ import annotations

from pathlib import Path

from maurice.host.context import (
    build_command_callbacks,
    resolve_global_context,
    resolve_local_context,
)
from maurice.host.agents import create_agent
from maurice.host.project import config_path, global_config_path
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import load_workspace_config
from maurice.kernel.session import SessionStore


def test_resolve_local_context_centers_state_on_project(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MAURICE_HOME", str(home / ".maurice"))
    project = tmp_path / "project"
    project.mkdir()
    (home / ".maurice" / "skills").mkdir(parents=True)
    (project / "skills").mkdir()

    global_config_path().parent.mkdir(parents=True, exist_ok=True)
    global_config_path().write_text(
        "provider:\n  type: mock\npermission_profile: safe\n",
        encoding="utf-8",
    )
    config_path(project).parent.mkdir()
    config_path(project).write_text(
        "permission_profile: limited\nskills:\n  - memory\n",
        encoding="utf-8",
    )

    ctx = resolve_local_context(project)

    assert ctx.scope == "local"
    assert ctx.lifecycle == "transient"
    assert ctx.context_root == project.resolve()
    assert ctx.content_root == project.resolve()
    assert ctx.active_project_root == project.resolve()
    assert ctx.state_root == project / ".maurice"
    assert ctx.sessions_path == project / ".maurice" / "sessions"
    assert ctx.events_path == project / ".maurice" / "events.jsonl"
    assert ctx.approvals_path == project / ".maurice" / "approvals.json"
    assert ctx.memory_path == project / ".maurice" / "memory.sqlite"
    assert ctx.run_root == project / ".maurice" / "run"
    assert ctx.server_meta_path == project / ".maurice" / "run" / "server.meta"
    assert ctx.permission_profile == "limited"
    assert ctx.enabled_skills == ["memory"]
    assert any(Path(root.path).name == "system_skills" for root in ctx.skill_roots)
    assert str(home / ".maurice" / "skills") in {root.path for root in ctx.skill_roots}
    assert str(project / "skills") in {root.path for root in ctx.skill_roots}


def test_resolve_local_context_merges_global_and_project_skill_roots(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MAURICE_HOME", str(home / ".maurice"))
    project = tmp_path / "project"
    project.mkdir()
    global_skills = tmp_path / "global-skills"
    project_configured_skills = tmp_path / "project-configured-skills"

    global_config_path().parent.mkdir(parents=True, exist_ok=True)
    global_config_path().write_text(
        f"""host:
  skill_roots:
    - path: {global_skills}
      origin: user
      mutable: true
""",
        encoding="utf-8",
    )
    config_path(project).parent.mkdir()
    config_path(project).write_text(
        f"""host:
  skill_roots:
    - path: {project_configured_skills}
      origin: user
      mutable: true
""",
        encoding="utf-8",
    )

    ctx = resolve_local_context(project)
    root_paths = {Path(root.path).expanduser() for root in ctx.skill_roots}

    assert global_skills in root_paths
    assert project_configured_skills in root_paths


def test_resolve_global_context_uses_workspace_agent_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    bundle = load_workspace_config(workspace)
    agent = bundle.agents.agents["main"]

    ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)

    assert ctx.scope == "global"
    assert ctx.lifecycle == "daemon"
    assert ctx.context_root == workspace.resolve()
    assert ctx.state_root == workspace.resolve()
    assert ctx.content_root == workspace.resolve()
    assert ctx.active_project_root is None
    assert ctx.sessions_path == workspace / "sessions"
    assert ctx.events_path == workspace / "agents" / "main" / "events.jsonl"
    assert ctx.approvals_path == workspace / "agents" / "main" / "approvals.json"
    assert ctx.memory_path == workspace / "agents" / "main" / "memory" / "memory.sqlite"
    assert ctx.run_root == workspace / "run"
    assert ctx.server_meta_path == workspace / "run" / "server.meta"
    assert ctx.permission_profile == "limited"
    assert any(Path(root.path).name == "system_skills" for root in ctx.skill_roots)


def test_resolve_global_context_migrates_legacy_memory_path(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    legacy = workspace / "skills" / "memory" / "memory.sqlite"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("memory data", encoding="utf-8")
    bundle = load_workspace_config(workspace)

    ctx = resolve_global_context(workspace, agent=bundle.agents.agents["main"], bundle=bundle)

    assert ctx.memory_path == workspace / "agents" / "main" / "memory" / "memory.sqlite"
    assert ctx.memory_path.read_text(encoding="utf-8") == "memory data"
    assert not legacy.exists()


def test_resolve_global_context_scopes_memory_to_selected_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime)
    agent = create_agent(workspace, agent_id="paul")
    bundle = load_workspace_config(workspace)

    ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)

    assert ctx.memory_path == workspace / "agents" / "paul" / "memory" / "memory.sqlite"


def test_command_callbacks_reset_and_compact_context_session(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    ctx = resolve_local_context(project)
    callbacks = build_command_callbacks(ctx, model_summary=lambda agent_id: f"model:{agent_id}")

    callbacks["reset_session"]("main", "default")
    store = SessionStore(ctx.sessions_path)
    store.append_message("main", "default", role="user", content="hello")
    store.append_message("main", "default", role="assistant", content="bonjour")

    compacted = callbacks["compact_session"]("main", "default")

    assert "2 messages" in compacted
    assert callbacks["model_summary"]("main") == "model:main"
    assert callbacks["workspace"] == project.resolve()
    assert callbacks["project_root"] == project.resolve()
    assert callbacks["active_project_path"] == project.resolve()
    assert callbacks["memory_path"] == project / ".maurice" / "memory.sqlite"
    assert store.load("main", "default").messages == []


def test_global_command_callbacks_do_not_claim_active_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime)
    bundle = load_workspace_config(workspace)
    ctx = resolve_global_context(workspace, agent=bundle.agents.agents["main"], bundle=bundle)

    callbacks = build_command_callbacks(ctx, agent_workspace=workspace / "agents" / "main")

    assert "active_project_path" not in callbacks
    assert "project_root" not in callbacks
    assert callbacks["agent_workspace"] == workspace / "agents" / "main"


def test_global_command_callbacks_claim_launch_project_when_provided(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = tmp_path / "outside-project"
    (runtime / "maurice").mkdir(parents=True)
    project.mkdir()
    initialize_workspace(workspace, runtime)
    bundle = load_workspace_config(workspace)
    ctx = resolve_global_context(
        workspace,
        agent=bundle.agents.agents["main"],
        bundle=bundle,
        active_project=project,
    )

    callbacks = build_command_callbacks(ctx, agent_workspace=workspace / "agents" / "main")

    assert ctx.active_project_root == project.resolve()
    assert callbacks["active_project_path"] == project.resolve()
    assert callbacks["project_root"] == project.resolve()
    assert callbacks["agent_workspace"] == workspace / "agents" / "main"
