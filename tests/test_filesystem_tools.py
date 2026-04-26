from __future__ import annotations

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.filesystem.tools import (
    list_entries,
    make_directory,
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


def test_filesystem_read_missing_file_returns_tool_error(tmp_path) -> None:
    result = read_text({"path": "missing.md"}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "not_found"


def test_filesystem_invalid_path_returns_tool_error(tmp_path) -> None:
    result = read_text({}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_arguments"
