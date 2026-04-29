"""Skill authoring system skill tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from maurice.kernel.config import SkillRootConfig
from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoadError, SkillLoader, SkillRoot

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def build_executors(ctx: Any) -> dict[str, Any]:
    return skill_authoring_tool_executors(
        ctx.permission_context,
        ctx.skill_roots,
        enabled_skills=ctx.enabled_skills or None,
    )


def skill_authoring_tool_executors(
    context: PermissionContext,
    roots: list[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "skills.create": lambda arguments: create(arguments, context, roots),
        "skills.list": lambda arguments: list_skills(arguments, roots, enabled_skills=enabled_skills),
        "skills.reload": lambda arguments: reload(arguments, roots, enabled_skills=enabled_skills),
        "maurice.system_skills.skills.tools.create": lambda arguments: create(arguments, context, roots),
        "maurice.system_skills.skills.tools.list_skills": lambda arguments: list_skills(arguments, roots, enabled_skills=enabled_skills),
        "maurice.system_skills.skills.tools.reload": lambda arguments: reload(arguments, roots, enabled_skills=enabled_skills),
    }


def create(
    arguments: dict[str, Any],
    context: PermissionContext,
    roots: list[SkillRoot | SkillRootConfig],
) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        return _error(
            "invalid_arguments",
            "skills.create requires a lowercase snake_case skill name.",
        )

    description = arguments.get("description", f"User skill {name}.")
    if not isinstance(description, str):
        return _error("invalid_arguments", "skills.create description must be a string.")

    user_root = _user_skill_root(roots)
    if user_root is None:
        return _error("missing_user_root", "No mutable user skill root configured.")

    collision = _find_existing_skill(name, roots)
    if collision is not None:
        return _error("skill_collision", f"Skill already exists: {name}")

    skill_dir = (Path(user_root.path).expanduser().resolve() / name).resolve()
    workspace_skills = Path(context.variables()["$workspace"]) / "skills"
    if not _is_relative_to(skill_dir, workspace_skills.resolve()):
        return _error("permission_denied", "User skills must be created under workspace/skills.")

    skill_dir.mkdir(parents=True, exist_ok=False)
    _write_skill_skeleton(skill_dir, name, description)

    return ToolResult(
        ok=True,
        summary=f"User skill created: {name}",
        data={"name": name, "path": str(skill_dir)},
        trust="local_mutable",
        artifacts=[
            {"type": "file", "path": str(skill_dir / "skill.yaml")},
            {"type": "file", "path": str(skill_dir / "prompt.md")},
            {"type": "file", "path": str(skill_dir / "tools.py")},
            {"type": "file", "path": str(skill_dir / "dreams.md")},
        ],
        events=[{"name": "skills.created", "payload": {"name": name, "path": str(skill_dir)}}],
        error=None,
    )


def list_skills(
    _arguments: dict[str, Any],
    roots: list[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: list[str] | None = None,
) -> ToolResult:
    try:
        registry = SkillLoader(roots, enabled_skills=enabled_skills).load()
    except SkillLoadError as exc:
        return _error("skill_load_failed", str(exc))
    return ToolResult(
        ok=True,
        summary=f"Discovered {len(registry.skills)} skills.",
        data={"skills": _registry_snapshot(registry)},
        trust="skill_generated",
        artifacts=[],
        events=[{"name": "skills.listed", "payload": {"count": len(registry.skills)}}],
        error=None,
    )


def reload(
    _arguments: dict[str, Any],
    roots: list[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: list[str] | None = None,
) -> ToolResult:
    try:
        registry = SkillLoader(roots, enabled_skills=enabled_skills).load()
    except SkillLoadError as exc:
        return _error("skill_reload_failed", str(exc))
    return ToolResult(
        ok=True,
        summary="Skills reloaded for future turns.",
        data={"skills": _registry_snapshot(registry)},
        trust="skill_generated",
        artifacts=[],
        events=[{"name": "skills.reloaded", "payload": {"count": len(registry.skills)}}],
        error=None,
    )


def _write_skill_skeleton(skill_dir: Path, name: str, description: str) -> None:
    manifest = {
        "name": name,
        "version": "0.1.0",
        "origin": "user",
        "mutable": True,
        "required": False,
        "description": description,
        "config_namespace": f"skills.{name}",
        "requires": {"binaries": [], "credentials": []},
        "dependencies": {"skills": [], "optional_skills": []},
        "permissions": [],
        "tools": [],
        "backend": None,
        "storage": None,
        "dreams": {"attachment": "dreams.md"},
        "events": {"state_publisher": None},
    }
    (skill_dir / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (skill_dir / "prompt.md").write_text(
        f"{description}\n",
        encoding="utf-8",
    )
    (skill_dir / "tools.py").write_text(
        '"""User skill tools."""\n',
        encoding="utf-8",
    )
    (skill_dir / "dreams.md").write_text(
        "This user skill does not provide dream inputs yet.\n",
        encoding="utf-8",
    )


def _registry_snapshot(registry) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "state": skill.state,
            "origin": skill.manifest.origin if skill.manifest else None,
            "mutable": skill.manifest.mutable if skill.manifest else None,
            "path": skill.path,
            "tools": [tool.name for tool in skill.tools],
            "errors": skill.errors,
            "suggested_fixes": skill.suggested_fixes,
            "missing_dependencies": skill.missing_dependencies,
        }
        for name, skill in sorted(registry.skills.items())
    ]


def _user_skill_root(roots: list[SkillRoot | SkillRootConfig]) -> SkillRoot | None:
    for root in roots:
        skill_root = root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
        if skill_root.origin == "user" and skill_root.mutable:
            return skill_root
    return None


def _find_existing_skill(name: str, roots: list[SkillRoot | SkillRootConfig]) -> Path | None:
    for root in roots:
        skill_root = root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
        candidate = Path(skill_root.path).expanduser().resolve() / name / "skill.yaml"
        if candidate.exists():
            return candidate.parent
    return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": False},
    )
