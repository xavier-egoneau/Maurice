from __future__ import annotations

from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import (
    PermissionContext,
    agent_profile_requires_confirmation,
    evaluate_permission,
)


def test_safe_profile_asks_for_workspace_write(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    evaluation = evaluate_permission(
        "safe",
        "fs.write",
        {"paths": [str(workspace / "notes" / "today.md")]},
        context,
    )

    assert evaluation.requires_approval
    assert evaluation.rememberable


def test_safe_profile_denies_workspace_secret_write(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    evaluation = evaluate_permission(
        "safe",
        "fs.write",
        {"paths": [str(workspace / "secrets" / "token.txt")]},
        context,
    )

    assert evaluation.denied


def test_limited_profile_allows_workspace_write_and_denies_runtime_write_path(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))

    workspace_eval = evaluate_permission(
        "limited",
        "fs.write",
        {"paths": [str(workspace / "notes.md")]},
        context,
    )
    runtime_eval = evaluate_permission(
        "limited",
        "fs.write",
        {"paths": [str(runtime / "maurice" / "kernel.py")]},
        context,
    )

    assert workspace_eval.allowed
    assert runtime_eval.denied


def test_limited_profile_allows_active_project_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    workspace.mkdir()
    runtime.mkdir()
    project.mkdir()
    context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        active_project_root=str(project),
    )

    evaluation = evaluate_permission(
        "limited",
        "fs.write",
        {"paths": [str(project / "PLAN.md")]},
        context,
    )

    assert evaluation.allowed


def test_limited_profile_resolves_relative_paths_under_active_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    workspace.mkdir()
    runtime.mkdir()
    project.mkdir()
    context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        active_project_root=str(project),
    )

    allowed = evaluate_permission(
        "limited",
        "fs.write",
        {"paths": ["PLAN.md"]},
        context,
    )
    denied = evaluate_permission(
        "limited",
        "fs.write",
        {"paths": ["secrets/token.txt"]},
        context,
    )

    assert allowed.allowed
    assert denied.denied


def test_runtime_write_defaults_to_proposal_flow_in_limited_profile(tmp_path) -> None:
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )

    evaluation = evaluate_permission(
        "limited",
        "runtime.write",
        {"targets": ["kernel"], "mode": "proposal_only"},
        context,
    )
    direct_apply = evaluate_permission(
        "limited",
        "runtime.write",
        {"targets": ["kernel"], "mode": "apply"},
        context,
    )

    assert evaluation.requires_approval
    assert direct_apply.denied


def test_host_control_is_scoped_by_action(tmp_path) -> None:
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )

    safe_eval = evaluate_permission(
        "safe",
        "host.control",
        {"actions": ["service.status"]},
        context,
    )
    limited_eval = evaluate_permission(
        "limited",
        "host.control",
        {"actions": ["service.status"]},
        context,
    )
    restart_eval = evaluate_permission(
        "limited",
        "host.control",
        {"actions": ["service.restart"]},
        context,
    )
    agent_create_eval = evaluate_permission(
        "limited",
        "host.control",
        {"actions": ["agents.create"]},
        context,
    )
    telegram_eval = evaluate_permission(
        "limited",
        "host.control",
        {"actions": ["telegram.configure"]},
        context,
    )

    assert safe_eval.denied
    assert limited_eval.requires_approval
    assert agent_create_eval.requires_approval
    assert telegram_eval.requires_approval
    assert restart_eval.denied


def test_power_profile_denies_raw_maurice_home_reads(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    maurice_home = tmp_path / ".maurice"
    workspace.mkdir()
    runtime.mkdir()
    maurice_home.mkdir()
    context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        maurice_home_root=str(maurice_home),
    )

    evaluation = evaluate_permission(
        "power",
        "fs.read",
        {"paths": [str(maurice_home / "workspaces" / "abc" / "config" / "agents.yaml")]},
        context,
    )

    assert evaluation.denied


def test_more_permissive_agent_profile_requires_confirmation() -> None:
    assert agent_profile_requires_confirmation("safe", "limited")
    assert not agent_profile_requires_confirmation("safe", "limited", confirmed=True)
    assert not agent_profile_requires_confirmation("power", "safe")


def test_approval_replay_requires_identical_scope_and_arguments(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    approvals = ApprovalStore(tmp_path / "approvals.json", event_store=event_store)
    scope = {"paths": ["/workspace/notes.md"]}
    arguments = {"path": "/workspace/notes.md", "content": "hello"}

    approval = approvals.request(
        agent_id="main",
        session_id="sess_1",
        correlation_id="turn_1",
        tool_name="filesystem.write",
        permission_class="fs.write",
        scope=scope,
        arguments=arguments,
        summary="Write notes.md",
        reason="Safe profile asks before workspace writes.",
        ttl_seconds=60,
        rememberable=True,
    )
    approvals.approve(approval.id)

    assert (
        approvals.approved_for_replay(
            permission_class="fs.write",
            scope=scope,
            tool_name="filesystem.write",
            arguments=arguments,
        )
        is not None
    )
    assert (
        approvals.approved_for_replay(
            permission_class="fs.write",
            scope={"paths": ["/workspace/other.md"]},
            tool_name="filesystem.write",
            arguments=arguments,
        )
        is None
    )
    assert (
        approvals.approved_for_replay(
            permission_class="fs.write",
            scope=scope,
            tool_name="filesystem.write",
            arguments={"path": "/workspace/notes.md", "content": "changed"},
        )
        is None
    )

    assert [event.name for event in event_store.read_all()] == [
        "approval.requested",
        "approval.resolved",
    ]


def test_approval_tool_session_grant_replays_different_arguments_in_same_session(tmp_path) -> None:
    approvals = ApprovalStore(tmp_path / "approvals.json")
    scope = {"hosts": ["*"]}
    approvals.remember_tool_for_session(
        agent_id="main",
        session_id="sess_1",
        tool_name="web.search",
        permission_class="network.outbound",
        scope=scope,
    )

    assert approvals.approved_for_replay(
        permission_class="network.outbound",
        scope=scope,
        tool_name="web.search",
        arguments={"query": "maurice"},
        agent_id="main",
        session_id="sess_1",
    )
    assert approvals.approved_for_replay(
        permission_class="network.outbound",
        scope=scope,
        tool_name="web.search",
        arguments={"query": "autre recherche"},
        agent_id="main",
        session_id="sess_1",
    )
    assert approvals.approved_for_replay(
        permission_class="network.outbound",
        scope=scope,
        tool_name="web.search",
        arguments={"query": "autre recherche"},
        agent_id="main",
        session_id="sess_2",
    ) is None
