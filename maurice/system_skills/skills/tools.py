"""Skill authoring system skill tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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
        scope=ctx.hooks.scope or None,
    )


def skill_authoring_tool_executors(
    context: PermissionContext,
    roots: list[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: list[str] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    return {
        "skills.create": lambda arguments: create(arguments, context, roots),
        "skills.list": lambda arguments: list_skills(arguments, roots, enabled_skills=enabled_skills, scope=scope),
        "skills.reload": lambda arguments: reload(arguments, roots, enabled_skills=enabled_skills, scope=scope),
        "maurice.system_skills.skills.tools.create": lambda arguments: create(arguments, context, roots),
        "maurice.system_skills.skills.tools.list_skills": lambda arguments: list_skills(arguments, roots, enabled_skills=enabled_skills, scope=scope),
        "maurice.system_skills.skills.tools.reload": lambda arguments: reload(arguments, roots, enabled_skills=enabled_skills, scope=scope),
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
    with_code = bool(arguments.get("with_code", False))

    workspace_skills = (Path(context.variables()["$workspace"]) / "skills").resolve()
    user_root = _user_skill_root(roots, workspace_skills=workspace_skills)
    if user_root is None:
        return _error("missing_user_root", "No mutable user skill root configured.")

    collision = _find_existing_skill(name, roots)
    if collision is not None:
        return _error("skill_collision", f"Skill already exists: {name}")

    skill_dir = (Path(user_root.path).expanduser().resolve() / name).resolve()
    if not _is_relative_to(skill_dir, workspace_skills):
        return _error("permission_denied", "User skills must be created under workspace/skills.")

    skill_dir.mkdir(parents=True, exist_ok=False)
    _write_skill_skeleton(skill_dir, name, description, with_code=with_code)

    artifacts = [
        {"type": "file", "path": str(skill_dir / "skill.md")},
        {"type": "file", "path": str(skill_dir / "dreams.md")},
        {"type": "file", "path": str(skill_dir / "daily.md")},
    ]
    if with_code:
        artifacts.append({"type": "file", "path": str(skill_dir / "tools.py")})
    return ToolResult(
        ok=True,
        summary=f"User skill created: {name}",
        data={"name": name, "path": str(skill_dir)},
        trust="local_mutable",
        artifacts=artifacts,
        events=[{"name": "skills.created", "payload": {"name": name, "path": str(skill_dir)}}],
        error=None,
    )


def list_skills(
    _arguments: dict[str, Any],
    roots: list[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: list[str] | None = None,
    scope: str | None = None,
) -> ToolResult:
    try:
        registry = SkillLoader(roots, enabled_skills=enabled_skills, scope=_normalized_scope(scope)).load()
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
    scope: str | None = None,
) -> ToolResult:
    try:
        registry = SkillLoader(roots, enabled_skills=enabled_skills, scope=_normalized_scope(scope)).load()
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


def _write_skill_skeleton(skill_dir: Path, name: str, description: str, *, with_code: bool) -> None:
    (skill_dir / "skill.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        "Describe when Maurice should use this skill, what sources it may read, "
        "and what boundaries it must respect.\n"
        "\n"
        "## Autonomy\n"
        "\n"
        "A shareable Maurice skill should be autonomous: document required "
        "binaries, credentials, local config files, install/setup commands, and "
        "validation steps. Do not rely on hidden setup from the author's machine.\n"
        "\n"
        "Keep this file as the human/agent instructions for the skill. Put dream "
        "synthesis rules in `dreams.md` and morning digest rules in `daily.md`.\n",
        encoding="utf-8",
    )
    (skill_dir / "dreams.md").write_text(
        "This skill does not contribute concrete dream signals yet.\n"
        "\n"
        "Use this file to explain what the dreaming pass should notice, connect, "
        "or propose for this skill.\n",
        encoding="utf-8",
    )
    (skill_dir / "daily.md").write_text(
        "This skill does not contribute to the daily yet.\n"
        "\n"
        "Use this file to explain what belongs in the morning digest for this "
        "skill, and when the skill should stay silent.\n",
        encoding="utf-8",
    )
    if with_code:
        (skill_dir / "tools.py").write_text(
            '"""Optional code hooks for this user skill."""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from datetime import UTC, datetime\n"
            "from typing import Any\n"
            "\n"
            "from maurice.kernel.contracts import DreamInput\n"
            "from maurice.kernel.permissions import PermissionContext\n"
            "\n"
            "\n"
            "def tool_declarations() -> list[dict[str, Any]]:\n"
            "    \"\"\"Return callable chat tools for this skill.\n"
            "\n"
            "    Leave this empty when the skill only contributes instructions or dreaming.\n"
            "    If you add declarations, also return matching executors from build_executors().\n"
            "    Use permission_class='integration.read' for read-only integrations and\n"
            "    permission_class='integration.write' for sync or maintenance actions.\n"
            "    \"\"\"\n"
            "    return []\n"
            "\n"
            "\n"
            "def build_executors(ctx: Any) -> dict[str, Any]:\n"
            "    del ctx\n"
            "    return {}\n"
            "\n"
            "\n"
            "def build_dream_input(\n"
            "    context: PermissionContext,\n"
            "    *,\n"
            "    config: dict[str, Any] | None = None,\n"
            "    all_skill_configs: dict[str, dict[str, Any]] | None = None,\n"
            ") -> DreamInput:\n"
            "    del context, config, all_skill_configs\n"
            "    return DreamInput(\n"
            f"        skill={name!r},\n"
            "        trust=\"skill_generated\",\n"
            "        freshness={\"generated_at\": datetime.now(UTC), \"expires_at\": None},\n"
            "        signals=[],\n"
            "        limits=[\"No coded dream signals yet. Add setup diagnostics before sharing.\"],\n"
            "    )\n",
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
            "available_in": skill.manifest.available_in if skill.manifest else [],
        }
        for name, skill in sorted(registry.skills.items())
    ]


def _normalized_scope(scope: str | None):
    return scope if scope in {"local", "global"} else None


def _user_skill_root(
    roots: list[SkillRoot | SkillRootConfig],
    *,
    workspace_skills: Path | None = None,
) -> SkillRoot | None:
    fallback: SkillRoot | None = None
    for root in roots:
        skill_root = root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
        if skill_root.origin != "user" or not skill_root.mutable:
            continue
        if fallback is None:
            fallback = skill_root
        if workspace_skills is None:
            return skill_root
        root_path = Path(skill_root.path).expanduser().resolve()
        if root_path == workspace_skills or _is_relative_to(root_path, workspace_skills):
            return skill_root
    return fallback


def _find_existing_skill(name: str, roots: list[SkillRoot | SkillRootConfig]) -> Path | None:
    for root in roots:
        skill_root = root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
        candidate = Path(skill_root.path).expanduser().resolve() / name
        if any((candidate / manifest).exists() for manifest in ("skill.yaml", "skill.md", "SKILL.md")):
            return candidate
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
