from __future__ import annotations

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.filesystem.tools import (
    list_entries,
    make_directory,
    move_path,
    read_text,
    write_text,
)


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def test_filesystem_write_read_list_and_mkdir(tmp_path) -> None:
    permission_context = context(tmp_path)
    workspace = tmp_path / "workspace"

    mkdir_result = make_directory({"path": "notes"}, permission_context)
    write_result = write_text(
        {"path": "notes/today.md", "content": "hello"}, permission_context
    )
    read_result = read_text({"path": "notes/today.md"}, permission_context)
    list_result = list_entries({"path": "notes"}, permission_context)

    assert mkdir_result.ok
    assert write_result.data["bytes"] == 5
    assert read_result.data["content"] == "hello"
    assert list_result.data["entries"][0]["name"] == "today.md"
    assert (
        workspace / "agents" / "main" / "content" / "notes" / "today.md"
    ).read_text(encoding="utf-8") == "hello"


def test_filesystem_write_returns_diff_artifact(tmp_path) -> None:
    permission_context = context(tmp_path)
    write_text({"path": "notes/today.md", "content": "hello\n"}, permission_context)

    result = write_text({"path": "notes/today.md", "content": "hello\nworld\n"}, permission_context)

    diff = next(artifact for artifact in result.artifacts if artifact.type == "diff")
    assert diff.data["insertions"] == 1
    assert diff.data["deletions"] == 0
    assert "+world" in diff.data["diff"]
    assert diff.data["before_exists"] is True


def test_filesystem_relative_paths_use_active_project_when_available(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = workspace / "agents" / "main" / "content" / "test"
    project.mkdir(parents=True)
    runtime.mkdir()
    permission_context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
        active_project_root=str(project),
    )

    write_result = write_text({"path": "notes.md", "content": "hello"}, permission_context)
    list_result = list_entries({"path": "."}, permission_context)

    assert write_result.ok
    assert (project / "notes.md").read_text(encoding="utf-8") == "hello"
    assert list_result.ok
    assert "notes.md" in list_result.summary


def test_filesystem_move_directory_inside_active_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = workspace / "agents" / "main" / "content" / "test"
    nested = project / "test" / "src"
    nested.mkdir(parents=True)
    runtime.mkdir()
    permission_context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
        active_project_root=str(project),
    )

    result = move_path(
        {"source_path": "test/src", "target_path": "src"},
        permission_context,
    )

    assert result.ok
    assert not nested.exists()
    assert (project / "src").is_dir()
    assert result.data["target_path"] == str((project / "src").resolve())


def test_filesystem_move_refuses_existing_target_without_overwrite(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = workspace / "agents" / "main" / "content" / "test"
    (project / "test" / "src").mkdir(parents=True)
    (project / "src").mkdir()
    runtime.mkdir()
    permission_context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
        active_project_root=str(project),
    )

    result = move_path(
        {"source_path": "test/src", "target_path": "src"},
        permission_context,
    )

    assert not result.ok
    assert result.error.code == "target_exists"
    assert (project / "test" / "src").is_dir()


def test_filesystem_active_project_name_resolves_to_project_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = workspace / "agents" / "main" / "content" / "test"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("hello", encoding="utf-8")
    runtime.mkdir()
    permission_context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
        active_project_root=str(project),
    )

    result = list_entries({"path": "test"}, permission_context)

    assert result.ok
    assert result.data["path"] == str(project.resolve())
    assert "notes.md" in result.summary


def test_filesystem_relative_paths_use_agent_content_without_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    agent_content = workspace / "agents" / "main" / "content"
    agent_content.mkdir(parents=True)
    runtime.mkdir()
    permission_context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
    )

    write_result = write_text({"path": "notes.md", "content": "hello"}, permission_context)

    assert write_result.ok
    assert (agent_content / "notes.md").read_text(encoding="utf-8") == "hello"


def test_filesystem_explicit_workspace_dirs_still_resolve_from_workspace(tmp_path) -> None:
    permission_context = context(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir()
    (workspace / "config" / "note.md").write_text("config note", encoding="utf-8")

    result = read_text({"path": "config/note.md"}, permission_context)

    assert result.ok
    assert result.data["path"] == str((workspace / "config" / "note.md").resolve())


def test_filesystem_read_missing_file_returns_tool_error(tmp_path) -> None:
    result = read_text({"path": "missing.md"}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "not_found"
    assert "Je ne trouve pas" in result.summary


def test_filesystem_invalid_path_returns_tool_error(tmp_path) -> None:
    result = read_text({}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_arguments"
