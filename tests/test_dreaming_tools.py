from __future__ import annotations

from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoader, SkillRoot
from maurice.system_skills.dreaming.tools import run
from maurice.system_skills.memory.tools import build_dream_input, remember


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def registry(enabled_skills):
    return SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=enabled_skills,
    ).load()


def test_dreaming_run_degrades_without_memory_input(tmp_path) -> None:
    permission_context = context(tmp_path)
    result = run({}, permission_context, registry(["dreaming"]))

    assert result.ok
    assert result.data["report"]["inputs"] == []
    assert result.data["report"]["summary"] == "Dream completed with no skill inputs."
    assert (tmp_path / "workspace" / "content" / "dreams").is_dir()


def test_dreaming_run_consumes_memory_dream_input(tmp_path) -> None:
    permission_context = context(tmp_path)
    remembered = remember(
        {"content": "Review project roadmap later.", "tags": ["project"]},
        permission_context,
    )
    events = EventStore(tmp_path / "events.jsonl")

    result = run(
        {"skills": ["memory"], "max_signals": 5},
        permission_context,
        registry(["dreaming", "memory"]),
        event_store=events,
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    report = result.data["report"]

    assert result.ok
    assert report["inputs"][0]["skill"] == "memory"
    assert report["inputs"][0]["signals"][0]["data"]["memory"]["id"] == remembered.data["id"]
    assert result.data["attachments"]["memory"]
    assert [event.name for event in events.read_all()] == [
        "dream.started",
        "dream.completed",
    ]


def test_dreaming_run_includes_global_memory_by_default(tmp_path) -> None:
    permission_context = context(tmp_path)
    remembered = remember(
        {"content": "Global memory must stay in dream context.", "tags": ["dreaming"]},
        permission_context,
    )

    result = run(
        {"skills": ["reminders"], "max_signals": 5},
        permission_context,
        registry(["dreaming", "memory", "reminders"]),
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    inputs = result.data["report"]["inputs"]
    assert result.ok
    assert [item["skill"] for item in inputs] == ["memory"]
    assert inputs[0]["signals"][0]["data"]["memory"]["id"] == remembered.data["id"]


def test_dreaming_run_can_explicitly_skip_global_memory(tmp_path) -> None:
    permission_context = context(tmp_path)
    remember(
        {"content": "Diagnostic run can skip memory.", "tags": ["dreaming"]},
        permission_context,
    )

    result = run(
        {"skills": ["reminders"], "include_memory": False},
        permission_context,
        registry(["dreaming", "memory", "reminders"]),
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    assert result.ok
    assert result.data["report"]["inputs"] == []


def test_dreaming_run_validates_arguments(tmp_path) -> None:
    result = run(
        {"skills": "memory"},
        context(tmp_path),
        registry(["dreaming", "memory"]),
    )

    assert not result.ok
    assert result.error.code == "invalid_arguments"


def test_dreaming_run_validates_include_memory(tmp_path) -> None:
    result = run(
        {"include_memory": "yes"},
        context(tmp_path),
        registry(["dreaming", "memory"]),
    )

    assert not result.ok
    assert result.error.code == "invalid_arguments"
