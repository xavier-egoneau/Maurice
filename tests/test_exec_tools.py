from __future__ import annotations

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.exec.tools import run_shell


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    workspace.mkdir()
    runtime.mkdir()
    project.mkdir()
    return PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        active_project_root=str(project),
    )


def test_shell_exec_runs_inside_active_project(tmp_path) -> None:
    permission_context = context(tmp_path)

    result = run_shell({"command": "pwd"}, permission_context)

    assert result.ok
    assert result.data["stdout"].strip() == str(tmp_path / "project")


def test_shell_exec_refuses_cwd_outside_project_and_workspace(tmp_path) -> None:
    permission_context = context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    result = run_shell({"command": "pwd", "cwd": str(outside)}, permission_context)

    assert not result.ok
    assert result.error.code == "invalid_arguments"


def test_shell_exec_hides_secret_environment(tmp_path, monkeypatch) -> None:
    permission_context = context(tmp_path)
    monkeypatch.setenv("MAURICE_TEST_SECRET_TOKEN", "do-not-leak")

    result = run_shell(
        {"command": "python3 -c 'import os; print(os.getenv(\"MAURICE_TEST_SECRET_TOKEN\"))'"},
        permission_context,
    )

    assert result.ok
    assert "do-not-leak" not in result.data["stdout"]
