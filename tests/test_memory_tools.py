from __future__ import annotations

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.memory.tools import get, remember, search


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def test_memory_remember_search_and_get(tmp_path) -> None:
    permission_context = context(tmp_path)

    remembered = remember(
        {
            "content": "Maurice keeps memory outside the kernel.",
            "tags": ["architecture", "memory"],
            "source": "test",
        },
        permission_context,
    )
    searched = search({"query": "kernel", "limit": 5}, permission_context)
    fetched = get({"id": remembered.data["id"]}, permission_context)

    assert remembered.ok
    assert searched.data["memories"][0]["id"] == remembered.data["id"]
    assert fetched.data["memory"]["content"] == "Maurice keeps memory outside the kernel."
    assert (tmp_path / "workspace" / "agents" / "main" / "memory" / "memory.sqlite").is_file()


def test_memory_can_use_context_memory_path(tmp_path) -> None:
    permission_context = context(tmp_path)
    memory_path = tmp_path / "project" / ".maurice" / "memory.sqlite"

    remembered = remember(
        {"content": "Local project memory.", "tags": ["local"]},
        permission_context,
        memory_path=memory_path,
    )

    assert remembered.ok
    assert remembered.artifacts[0].path == str(memory_path.resolve())
    assert memory_path.is_file()
    assert not (
        tmp_path / "workspace" / "agents" / "main" / "memory" / "memory.sqlite"
    ).exists()


def test_memory_search_missing_query_returns_tool_error(tmp_path) -> None:
    result = search({}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_arguments"


def test_memory_get_missing_id_returns_not_found(tmp_path) -> None:
    result = get({"id": "mem_missing"}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "not_found"
