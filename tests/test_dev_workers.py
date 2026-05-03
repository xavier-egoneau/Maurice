from __future__ import annotations

import json

from maurice.host.agents import update_agent
from maurice.host.paths import kernel_config_path
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import read_yaml_file, write_yaml_file
from maurice.kernel.contracts import PermissionClass, PermissionDecision
from maurice.kernel.permissions import PermissionContext, evaluate_permission
from maurice.kernel.providers import MockProvider
from maurice.kernel.skills import SkillContext, SkillLoader, SkillRoot, SkillRegistry
from maurice.system_skills.dev.tools import spawn_workers


def test_loader_registers_dev_spawn_workers_tool() -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()

    declaration = registry.tools["dev.spawn_workers"]

    assert declaration.permission.permission_class == "agent.spawn"
    assert declaration.permission.scope["agents"] == ["dev_worker"]
    assert declaration.permission.scope["max_parallel"] == 5


def test_limited_profile_allows_only_scoped_dev_workers(tmp_path) -> None:
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
        active_project_root=str(tmp_path / "project"),
    )

    allowed = evaluate_permission(
        "limited",
        PermissionClass.AGENT_SPAWN,
        {"agents": ["dev_worker"], "max_parallel": 5, "max_depth": 1},
        context,
    )
    too_many = evaluate_permission(
        "limited",
        PermissionClass.AGENT_SPAWN,
        {"agents": ["dev_worker"], "max_parallel": 6, "max_depth": 1},
        context,
    )
    wrong_agent = evaluate_permission(
        "limited",
        PermissionClass.AGENT_SPAWN,
        {"agents": ["random_worker"], "max_parallel": 1, "max_depth": 1},
        context,
    )

    assert allowed.decision == PermissionDecision.ALLOW
    assert too_many.decision == PermissionDecision.DENY
    assert wrong_agent.decision == PermissionDecision.DENY


def test_spawn_workers_uses_worker_model_chain_and_minimal_file_context(tmp_path, monkeypatch) -> None:
    workspace = initialize_workspace(tmp_path / "workspace", tmp_path / "runtime", permission_profile="limited")
    project = tmp_path / "project"
    project.mkdir()
    (project / "target.py").write_text("VALUE = 1\n", encoding="utf-8")

    kernel_path = kernel_config_path(workspace)
    kernel = read_yaml_file(kernel_path)
    kernel["kernel"]["models"] = {
        "default": "parent_mock",
        "entries": {
            "parent_mock": {"provider": "mock", "name": "parent"},
            "worker_mock": {"provider": "mock", "name": "worker"},
        },
    }
    write_yaml_file(kernel_path, kernel)
    update_agent(
        workspace,
        agent_id="main",
        model_chain=["parent_mock"],
        worker_model_chain=["worker_mock"],
        skills=["dev"],
    )

    captured: dict[str, object] = {}

    def fake_provider(bundle, message, credentials, *, agent=None):
        captured["chain"] = list(agent.model_chain)
        captured["message"] = message
        return (
            MockProvider(
                [
                    {
                        "type": "text_delta",
                        "delta": (
                            "status: completed\n"
                            "summary: worker done\n"
                            "changed_files: none\n"
                            "verification: not_run: inspection only\n"
                            "blocker: none\n"
                            "impact_on_other_tasks: none\n"
                            "suggested_next_worker_task: none\n"
                        ),
                    },
                    {"type": "status", "status": "completed"},
                ]
            ),
            {"name": "worker"},
        )

    monkeypatch.setattr("maurice.system_skills.dev.tools._provider_and_model_for_config", fake_provider)

    result = spawn_workers(
        {
            "workers": [
                {
                    "task": "Inspect target value",
                    "context_summary": "Only inspect the target file.",
                    "relevant_files": ["target.py"],
                    "write_paths": ["target.py"],
                    "expected_output": "Short result.",
                }
            ],
            "max_workers": 1,
            "max_tool_iterations": 2,
            "max_seconds_per_worker": 30,
        },
        SkillContext(
            permission_context=PermissionContext(
                workspace_root=str(workspace),
                runtime_root=str(tmp_path / "runtime"),
                agent_workspace_root=str(workspace / "agents" / "main"),
                active_project_root=str(project),
            ),
            registry=SkillRegistry(skills={}, tools={}),
            all_skill_configs={},
            skill_roots=[],
            enabled_skills=["dev"],
            agent_id="main",
        ),
    )

    assert result.ok is True
    assert result.data["workers"][0]["status"] == "completed"
    assert result.data["workers"][0]["worker_status"] == "completed"
    assert result.data["workers"][0]["model"] == "worker"
    assert captured["chain"] == ["worker_mock"]
    assert "VALUE = 1" in str(captured["message"])
    assert "Only inspect the target file." in str(captured["message"])
    assert "status: completed | blocked | needs_arbitration | obsolete" in str(captured["message"])
    assert "impact_on_other_tasks" in str(captured["message"])


def test_spawn_workers_surfaces_worker_blockers_as_orchestration_status(tmp_path, monkeypatch) -> None:
    workspace = initialize_workspace(tmp_path / "workspace", tmp_path / "runtime", permission_profile="limited")
    project = tmp_path / "project"
    project.mkdir()

    def fake_provider(bundle, message, credentials, *, agent=None):
        return (
            MockProvider(
                [
                    {
                        "type": "text_delta",
                        "delta": (
                            "status: needs_arbitration\n"
                            "summary: two incompatible paths are possible\n"
                            "changed_files: none\n"
                            "verification: not_run: waiting for arbitration\n"
                            "blocker: product decision required\n"
                            "impact_on_other_tasks: worker touching gateway config may be obsolete\n"
                            "suggested_next_worker_task: retry after parent chooses config shape\n"
                        ),
                    },
                    {"type": "status", "status": "completed"},
                ]
            ),
            {"name": "mock"},
        )

    monkeypatch.setattr("maurice.system_skills.dev.tools._provider_and_model_for_config", fake_provider)

    result = spawn_workers(
        {"workers": [{"task": "Compare config options"}], "max_workers": 1},
        SkillContext(
            permission_context=PermissionContext(
                workspace_root=str(workspace),
                runtime_root=str(tmp_path / "runtime"),
                agent_workspace_root=str(workspace / "agents" / "main"),
                active_project_root=str(project),
            ),
            registry=SkillRegistry(skills={}, tools={}),
            agent_id="main",
        ),
    )

    worker = result.data["workers"][0]
    assert result.summary == "0/1 dev worker(s) completed."
    assert worker["status"] == "needs_arbitration"
    assert worker["execution_status"] == "completed"
    assert worker["worker_status"] == "needs_arbitration"
    assert "impact_on_other_tasks" in worker["summary"]


def test_spawn_workers_caps_one_call_to_five_workers(tmp_path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace", tmp_path / "runtime", permission_profile="limited")
    project = tmp_path / "project"
    project.mkdir()

    result = spawn_workers(
        {"workers": [{"task": f"task {index}"} for index in range(6)]},
        SkillContext(
            permission_context=PermissionContext(
                workspace_root=str(workspace),
                runtime_root=str(tmp_path / "runtime"),
                agent_workspace_root=str(workspace / "agents" / "main"),
                active_project_root=str(project),
            ),
            registry=SkillRegistry(skills={}, tools={}),
            agent_id="main",
        ),
    )

    assert result.ok is True
    assert len(result.data["workers"]) == 5
    assert result.data["skipped_workers"] == 1


def test_spawn_workers_counts_only_non_stale_running_workers(tmp_path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace", tmp_path / "runtime", permission_profile="limited")
    agent_workspace = workspace / "agents" / "main"
    stale = agent_workspace / "runs" / "dev_workers" / "old"
    fresh = agent_workspace / "runs" / "dev_workers" / "fresh"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (stale / "status.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    (fresh / "status.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    old_time = 1_600_000_000
    (stale / "status.json").touch()
    import os

    os.utime(stale / "status.json", (old_time, old_time))

    from maurice.system_skills.dev.tools import _active_worker_count

    assert _active_worker_count(agent_workspace) == 1
