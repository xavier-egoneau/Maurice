from __future__ import annotations

from maurice.kernel.config import SkillRootConfig
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoader, SkillRoot, SkillState
from maurice.system_skills.skills.tools import create, list_skills, reload


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (workspace / "skills").mkdir(parents=True)
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def roots(tmp_path):
    return [
        SkillRoot(path="maurice/system_skills", origin="system", mutable=False),
        SkillRoot(
            path=str(tmp_path / "workspace" / "skills"),
            origin="user",
            mutable=True,
        ),
    ]


def test_skills_create_writes_user_skill_under_workspace(tmp_path) -> None:
    permission_context = context(tmp_path)

    result = create(
        {"name": "notes_helper", "description": "Helps with notes."},
        permission_context,
        roots(tmp_path),
    )

    skill_dir = tmp_path / "workspace" / "skills" / "notes_helper"
    registry = SkillLoader(roots(tmp_path), enabled_skills=["notes_helper"]).load()

    assert result.ok
    assert (skill_dir / "skill.md").is_file()
    assert not (skill_dir / "skill.yaml").exists()
    assert not (skill_dir / "prompt.md").exists()
    assert not (skill_dir / "tools.py").exists()
    assert (skill_dir / "daily.md").is_file()
    assert registry.skills["notes_helper"].state == SkillState.LOADED
    assert registry.skills["notes_helper"].daily


def test_skills_create_prefers_context_user_root(tmp_path) -> None:
    permission_context = context(tmp_path)
    global_skills = tmp_path / "home" / ".maurice" / "skills"
    project_skills = tmp_path / "workspace" / "skills"
    global_skills.mkdir(parents=True)
    project_skills.mkdir(parents=True, exist_ok=True)
    mixed_roots = [
        SkillRoot(path=str(global_skills), origin="user", mutable=True),
        SkillRoot(path=str(project_skills), origin="user", mutable=True),
    ]

    result = create({"name": "local_notes"}, permission_context, mixed_roots)

    assert result.ok
    assert result.data["path"] == str(project_skills / "local_notes")
    assert not (global_skills / "local_notes").exists()
    assert (project_skills / "local_notes" / "skill.md").is_file()


def test_skills_create_can_add_code_scaffold(tmp_path) -> None:
    permission_context = context(tmp_path)

    result = create(
        {"name": "calendar_notes", "description": "Reads calendar notes.", "with_code": True},
        permission_context,
        roots(tmp_path),
    )

    skill_dir = tmp_path / "workspace" / "skills" / "calendar_notes"
    registry = SkillLoader(roots(tmp_path), enabled_skills=["calendar_notes"]).load()

    assert result.ok
    assert (skill_dir / "tools.py").is_file()
    assert registry.skills["calendar_notes"].manifest.dreams.input_builder == "tools.build_dream_input"


def test_skills_create_rejects_collision_with_system_skill(tmp_path) -> None:
    permission_context = context(tmp_path)

    result = create(
        {"name": "memory"},
        permission_context,
        roots(tmp_path),
    )

    assert not result.ok
    assert result.error.code == "skill_collision"
    assert not (tmp_path / "workspace" / "skills" / "memory").exists()


def test_skills_create_rejects_user_root_outside_workspace(tmp_path) -> None:
    permission_context = context(tmp_path)
    unsafe_roots = [
        SkillRootConfig(path=str(tmp_path / "elsewhere"), origin="user", mutable=True)
    ]

    result = create({"name": "unsafe"}, permission_context, unsafe_roots)

    assert not result.ok
    assert result.error.code == "permission_denied"


def test_skills_list_and_reload_return_registry_snapshot(tmp_path) -> None:
    permission_context = context(tmp_path)
    create({"name": "notes_helper"}, permission_context, roots(tmp_path))

    listed = list_skills({}, roots(tmp_path), enabled_skills=["notes_helper"])
    reloaded = reload({}, roots(tmp_path), enabled_skills=["notes_helper"])

    assert listed.ok
    assert listed.data["skills"][0]["name"] == "filesystem" or listed.data["skills"]
    assert any(skill["name"] == "notes_helper" for skill in reloaded.data["skills"])
    assert reloaded.summary == "Skills reloaded for future turns."
