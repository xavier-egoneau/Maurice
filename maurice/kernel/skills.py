"""Strict skill discovery and registry loading."""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field as dc_field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import Field, ValidationError

from maurice.kernel.config import SkillRootConfig
from maurice.kernel.contracts import (
    CommandDeclaration,
    MauriceModel,
    SkillManifest,
    SkillOrigin,
    ToolDeclaration,
)
from maurice.kernel.events import EventStore

ToolExecutor = Callable[[dict[str, Any]], Any]


@dataclass
class SkillHooks:
    """Host-provided callbacks and paths injected into each skill's executor context."""
    context_root: str = ""
    content_root: str = ""
    state_root: str = ""
    memory_path: str = ""
    scope: str = ""
    lifecycle: str = ""
    schedule_reminder: Callable[..., Any] | None = dc_field(default=None)
    cancel_job: Callable[..., Any] | None = dc_field(default=None)
    vision_backend: Any = dc_field(default=None)
    agents: dict[str, dict[str, Any]] = dc_field(default_factory=dict)


class SkillState(StrEnum):
    LOADED = "loaded"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    DISABLED_WITH_ERROR = "disabled_with_error"
    MISSING_DEPENDENCY = "missing_dependency"
    MIGRATION_REQUIRED = "migration_required"


class SkillLoadError(RuntimeError):
    pass


class RequiredSkillError(SkillLoadError):
    pass


class SkillRoot(MauriceModel):
    path: str
    origin: SkillOrigin
    mutable: bool = False

    @classmethod
    def from_config(cls, root: SkillRootConfig) -> "SkillRoot":
        return cls(path=root.path, origin=root.origin, mutable=root.mutable)


class LoadedSkill(MauriceModel):
    name: str
    path: str
    root: str
    origin: SkillOrigin
    mutable: bool = False
    state: SkillState
    manifest: SkillManifest | None = None
    prompt: str = ""
    dreams: str = ""
    daily: str = ""
    tools: list[ToolDeclaration] = []
    commands: list[CommandDeclaration] = []
    errors: list[str] = []
    suggested_fixes: list[str] = []
    missing_dependencies: list[str] = []


class SkillContext(MauriceModel):
    """Runtime context passed to each skill's build_executors function."""

    permission_context: Any  # PermissionContext — typed as Any to avoid circular import
    registry: Any | None = None  # SkillRegistry — filled by build_executor_map
    event_store: Any | None = None  # EventStore
    skill_config: dict[str, Any] = {}  # this skill's config slice (set by build_executor_map)
    all_skill_configs: dict[str, dict[str, Any]] = {}  # full SkillsConfig.skills
    skill_roots: list[Any] = []  # list[SkillRoot | SkillRootConfig]
    enabled_skills: list[str] = []
    agent_id: str = "main"
    session_id: str | None = None
    hooks: SkillHooks = Field(default_factory=SkillHooks)
    extra: dict[str, Any] = {}  # deprecated — use hooks

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "populate_by_name": True}


class SkillRegistry(MauriceModel):
    skills: dict[str, LoadedSkill]
    tools: dict[str, ToolDeclaration]
    commands: dict[str, CommandDeclaration] = {}

    def loaded(self) -> dict[str, LoadedSkill]:
        return {
            name: skill
            for name, skill in self.skills.items()
            if skill.state == SkillState.LOADED
        }

    def build_executor_map(self, ctx: SkillContext) -> dict[str, ToolExecutor]:
        """Dynamically load and call each loaded skill's build_executors(ctx)."""
        executors: dict[str, ToolExecutor] = {}
        for name, skill in self.loaded().items():
            if not skill.manifest or not skill.manifest.tools_module:
                continue
            try:
                mod = import_skill_module(skill, skill.manifest.tools_module)
            except ImportError:
                continue
            builder = getattr(mod, "build_executors", None)
            if builder is None:
                continue
            skill_ctx = ctx.model_copy(update={
                "registry": self,
                "skill_config": ctx.all_skill_configs.get(name, {}),
            })
            try:
                executors.update(builder(skill_ctx))
            except Exception:
                pass
        return executors


def import_skill_module(skill: LoadedSkill, module_path: str) -> Any:
    """Import a skill module by package path or relative to a user skill folder."""
    try:
        return importlib.import_module(module_path)
    except ImportError as exc:
        module_file = _relative_skill_module_path(skill, module_path)
        if module_file is None:
            raise exc
        return _import_module_from_path(skill, module_path, module_file)


def _relative_skill_module_path(skill: LoadedSkill, module_path: str) -> Path | None:
    if not skill.mutable or skill.origin != "user":
        return None
    relative = Path(*module_path.split(".")).with_suffix(".py")
    module_file = Path(skill.path).expanduser().resolve() / relative
    if not module_file.is_file():
        return None
    return module_file


def _import_module_from_path(skill: LoadedSkill, module_path: str, module_file: Path) -> Any:
    module_name = (
        "maurice_user_skill_"
        + skill.name.replace("-", "_")
        + "_"
        + str(abs(hash(str(module_file))))
        + "_"
        + module_path.replace(".", "_")
    )
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load user skill module: {module_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class SkillLoader:
    def __init__(
        self,
        roots: Iterable[SkillRoot | SkillRootConfig],
        *,
        enabled_skills: Iterable[str] | None = None,
        required_skills: Iterable[str] | None = None,
        available_credentials: Iterable[str] | None = None,
        scope: Literal["local", "global"] | None = None,
        event_store: EventStore | None = None,
        agent_id: str = "main",
        session_id: str = "startup",
    ) -> None:
        self.roots = [
            root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
            for root in roots
        ]
        self.enabled_skills = set(enabled_skills) if enabled_skills is not None else None
        self.required_skills = set(required_skills or [])
        self.available_credentials = set(available_credentials or [])
        self.scope = scope
        self.event_store = event_store
        self.agent_id = agent_id
        self.session_id = session_id

    def load(self) -> SkillRegistry:
        discovered = self._discover()
        collisions = self._find_collisions(discovered)
        if collisions:
            raise SkillLoadError(
                "Skill name collision: " + ", ".join(sorted(collisions.keys()))
            )

        loaded_by_name: dict[str, LoadedSkill] = {}
        for skill_dir, root in discovered:
            loaded = self._load_one(skill_dir, root)
            loaded_by_name[loaded.name] = loaded

        for loaded in list(loaded_by_name.values()):
            self._resolve_dependencies(loaded, loaded_by_name)

        tools: dict[str, ToolDeclaration] = {}
        commands: dict[str, CommandDeclaration] = {}
        for loaded in loaded_by_name.values():
            if loaded.state != SkillState.LOADED:
                self._emit_skill_event(loaded)
                continue
            registered_for_skill = []
            registered_commands_for_skill = []
            for tool in loaded.tools:
                if tool.name in tools:
                    loaded.state = SkillState.DISABLED_WITH_ERROR
                    loaded.errors.append(f"Tool name collision: {tool.name}")
                    loaded.suggested_fixes.append(
                        f"Rename `{tool.name}` in {loaded.name}/skill.yaml so each tool name is unique."
                    )
                    for registered_tool in registered_for_skill:
                        tools.pop(registered_tool, None)
                    loaded.tools = []
                    if loaded.manifest and (
                        loaded.manifest.required or loaded.name in self.required_skills
                    ):
                        raise RequiredSkillError(f"Required skill {loaded.name} has a tool collision: {tool.name}")
                    break
                tools[tool.name] = tool
                registered_for_skill.append(tool.name)
            if loaded.state == SkillState.LOADED:
                for command in loaded.commands:
                    command_names = [command.name, *command.aliases]
                    collision = next((name for name in command_names if name in commands), None)
                    if collision:
                        loaded.state = SkillState.DISABLED_WITH_ERROR
                        loaded.errors.append(f"Command name collision: {collision}")
                        loaded.suggested_fixes.append(
                            f"Rename `{collision}` in {loaded.name}/skill.yaml so each command name is unique."
                        )
                        for registered_tool in registered_for_skill:
                            tools.pop(registered_tool, None)
                        for registered_command in registered_commands_for_skill:
                            commands.pop(registered_command, None)
                        loaded.tools = []
                        loaded.commands = []
                        if loaded.manifest and (
                            loaded.manifest.required or loaded.name in self.required_skills
                        ):
                            raise RequiredSkillError(f"Required skill {loaded.name} has a command collision: {collision}")
                        break
                    for name in command_names:
                        commands[name] = command
                        registered_commands_for_skill.append(name)
            self._emit_skill_event(loaded)

        return SkillRegistry(skills=loaded_by_name, tools=tools, commands=commands)

    def _discover(self) -> list[tuple[Path, SkillRoot]]:
        discovered: list[tuple[Path, SkillRoot]] = []
        for root in self.roots:
            root_path = Path(root.path).expanduser()
            if not root_path.exists():
                continue
            for skill_dir in sorted(path for path in root_path.iterdir() if path.is_dir()):
                if _skill_manifest_path(skill_dir) is not None:
                    discovered.append((skill_dir, root))
        return discovered

    def _find_collisions(
        self, discovered: list[tuple[Path, SkillRoot]]
    ) -> dict[str, list[Path]]:
        names: dict[str, list[Path]] = {}
        for skill_dir, _root in discovered:
            name = self._manifest_name(skill_dir) or skill_dir.name
            names.setdefault(name, []).append(skill_dir)
        return {name: paths for name, paths in names.items() if len(paths) > 1}

    def _load_one(self, skill_dir: Path, root: SkillRoot) -> LoadedSkill:
        try:
            manifest_path = _skill_manifest_path(skill_dir)
            manifest = self._read_manifest(skill_dir, root)
            required = manifest.required or manifest.name in self.required_skills
            if self.enabled_skills is not None and manifest.name not in self.enabled_skills:
                return LoadedSkill(
                    name=manifest.name,
                    path=str(skill_dir),
                    root=root.path,
                    origin=root.origin,
                    mutable=root.mutable,
                    state=SkillState.DISABLED,
                    manifest=manifest,
                )
            if self.scope is not None and self.scope not in manifest.available_in:
                loaded = LoadedSkill(
                    name=manifest.name,
                    path=str(skill_dir),
                    root=root.path,
                    origin=root.origin,
                    mutable=root.mutable,
                    state=SkillState.UNAVAILABLE,
                    manifest=manifest,
                    errors=[f"Skill unavailable in {self.scope} scope."],
                    suggested_fixes=[
                        f"Use this skill in one of: {', '.join(manifest.available_in)}."
                    ],
                )
                if required:
                    raise RequiredSkillError(
                        f"Required skill {manifest.name} unavailable in {self.scope} scope"
                    )
                return loaded

            missing = self._missing_runtime_dependencies(manifest)
            if missing:
                loaded = LoadedSkill(
                    name=manifest.name,
                    path=str(skill_dir),
                    root=root.path,
                    origin=root.origin,
                    mutable=root.mutable,
                    state=SkillState.MISSING_DEPENDENCY,
                    manifest=manifest,
                    errors=["Missing required dependencies."],
                    suggested_fixes=[
                        "Install missing binaries or configure missing credentials before enabling this skill."
                    ],
                    missing_dependencies=missing,
                )
                if required:
                    raise RequiredSkillError("; ".join(missing))
                return loaded

            return LoadedSkill(
                name=manifest.name,
                path=str(skill_dir),
                root=root.path,
                origin=root.origin,
                mutable=root.mutable,
                state=SkillState.LOADED,
                manifest=manifest,
                prompt=self._prompt_text(skill_dir, manifest_path),
                dreams=self._read_optional_text(
                    skill_dir / manifest.dreams.attachment
                    if manifest.dreams and manifest.dreams.attachment
                    else skill_dir / "dreams.md"
                ),
                daily=self._read_optional_text(
                    skill_dir / manifest.daily.attachment
                    if manifest.daily and manifest.daily.attachment
                    else skill_dir / "daily.md"
                ),
                tools=self._tool_declarations(manifest),
                commands=self._command_declarations(manifest),
            )
        except RequiredSkillError:
            raise
        except Exception as exc:
            fallback_name = self._manifest_name(skill_dir) or skill_dir.name
            return LoadedSkill(
                name=fallback_name,
                path=str(skill_dir),
                root=root.path,
                origin=root.origin,
                mutable=root.mutable,
                state=SkillState.DISABLED_WITH_ERROR,
                errors=[str(exc)],
                suggested_fixes=_suggest_manifest_fixes(exc),
            )

    def _resolve_dependencies(
        self, loaded: LoadedSkill, loaded_by_name: dict[str, LoadedSkill]
    ) -> None:
        if loaded.state != SkillState.LOADED or loaded.manifest is None:
            return
        required_missing = [
            dependency
            for dependency in loaded.manifest.dependencies.skills
            if dependency not in loaded_by_name
            or loaded_by_name[dependency].state != SkillState.LOADED
        ]
        if not required_missing:
            return
        loaded.state = SkillState.MISSING_DEPENDENCY
        loaded.missing_dependencies.extend(required_missing)
        loaded.errors.append("Missing required skill dependencies.")
        loaded.suggested_fixes.append(
            "Enable or install required dependency skills before enabling this skill."
        )
        if loaded.manifest.required or loaded.name in self.required_skills:
            raise RequiredSkillError(
                f"Required skill {loaded.name} missing dependencies: "
                + ", ".join(required_missing)
            )

    def _missing_runtime_dependencies(self, manifest: SkillManifest) -> list[str]:
        missing = []
        for binary in manifest.requires.binaries:
            if shutil.which(binary) is None:
                missing.append(f"binary:{binary}")
        for credential in manifest.requires.credentials:
            if credential not in self.available_credentials:
                missing.append(f"credential:{credential}")
        return missing

    def _tool_declarations(self, manifest: SkillManifest) -> list[ToolDeclaration]:
        declarations = []
        for exported in manifest.tools:
            declarations.append(
                ToolDeclaration(
                    name=exported.name,
                    owner_skill=manifest.name,
                    description=exported.description or manifest.description,
                    input_schema=exported.input_schema,
                    permission={
                        "class": exported.permission_class,
                        "scope": exported.permission_scope,
                    },
                    trust=exported.trust,
                    executor=exported.executor or f"{manifest.name}.tools",
                )
            )
        return declarations

    def _command_declarations(self, manifest: SkillManifest) -> list[CommandDeclaration]:
        declarations = []
        for exported in manifest.commands:
            declarations.append(
                CommandDeclaration(
                    name=exported.name,
                    owner_skill=manifest.name,
                    description=exported.description or manifest.description,
                    handler=exported.handler,
                    renderer=exported.renderer,
                    aliases=exported.aliases,
                    available_in=exported.available_in,
                    project_required=exported.project_required,
                )
            )
        return declarations

    def _emit_skill_event(self, loaded: LoadedSkill) -> None:
        if self.event_store is None:
            return
        event_name = "skill.loaded" if loaded.state == SkillState.LOADED else "skill.failed"
        self.event_store.emit(
            name=event_name,
            origin="kernel",
            agent_id=self.agent_id,
            session_id=self.session_id,
            payload={
                "skill": loaded.name,
                "state": loaded.state,
                "errors": loaded.errors,
                "suggested_fixes": loaded.suggested_fixes,
                "missing_dependencies": loaded.missing_dependencies,
            },
        )

    @staticmethod
    def _read_manifest(skill_dir: Path, root: SkillRoot) -> SkillManifest:
        path = _skill_manifest_path(skill_dir)
        if path is None:
            raise FileNotFoundError(f"No skill manifest found in {skill_dir}")
        if path.name == "skill.yaml":
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            data = _declarative_manifest(skill_dir, root, path)
        return SkillManifest.model_validate(data)

    @staticmethod
    def _manifest_name(skill_dir: Path) -> str | None:
        path = _skill_manifest_path(skill_dir)
        if path is None:
            return None
        if path.name != "skill.yaml":
            return _normalize_skill_name(skill_dir.name)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        name = data.get("name")
        return name if isinstance(name, str) else None

    @staticmethod
    def _read_optional_text(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _prompt_text(skill_dir: Path, manifest_path: Path | None) -> str:
        prompt_path = skill_dir / "prompt.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        if manifest_path is not None and manifest_path.name in {"skill.md", "SKILL.md"}:
            return _skill_md_body(manifest_path)
        return ""


def _suggest_manifest_fixes(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        suggestions = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            error_type = error.get("type", "")
            if error_type == "missing":
                suggestions.append(f"Add required field `{location}` to skill.yaml.")
            elif error_type == "extra_forbidden":
                suggestions.append(f"Remove unsupported field `{location}` from skill.yaml.")
            elif error_type in {"literal_error", "enum"}:
                suggestions.append(f"Use a supported value for `{location}` in skill.yaml.")
            else:
                suggestions.append(f"Fix `{location}` in skill.yaml: {error.get('msg', 'invalid value')}.")
        return _unique(suggestions)
    if isinstance(exc, yaml.YAMLError):
        return ["Fix skill.yaml syntax so it is valid YAML."]
    return ["Review skill.yaml against the SkillManifest contract and reload skills."]


def _skill_manifest_path(skill_dir: Path) -> Path | None:
    yaml_manifest = skill_dir / "skill.yaml"
    if yaml_manifest.is_file():
        return yaml_manifest
    for name in ("skill.md", "SKILL.md"):
        path = skill_dir / name
        if path.is_file():
            return path
    return None


def _declarative_manifest(skill_dir: Path, root: SkillRoot, path: Path) -> dict[str, Any]:
    name = _normalize_skill_name(skill_dir.name)
    description = _skill_md_description(path) or f"User skill {name}."
    dreams = {"attachment": "dreams.md"} if (skill_dir / "dreams.md").is_file() else None
    daily = {"attachment": "daily.md"} if (skill_dir / "daily.md").is_file() else None
    input_builder = skill_dir / "tools.py"
    if dreams is not None and input_builder.is_file():
        dreams["input_builder"] = "tools.build_dream_input"
    tools = _declarative_tool_exports(skill_dir, name)
    return {
        "name": name,
        "version": "0.1.0",
        "origin": root.origin,
        "mutable": root.mutable,
        "required": False,
        "description": description,
        "config_namespace": f"skills.{name}",
        "available_in": ["local", "global"],
        "requires": {"binaries": [], "credentials": []},
        "dependencies": {"skills": [], "optional_skills": []},
        "permissions": [],
        "tools": tools,
        "tools_module": "tools" if tools else None,
        "backend": None,
        "storage": None,
        "dreams": dreams,
        "daily": daily,
        "events": {"state_publisher": None},
    }


def _declarative_tool_exports(skill_dir: Path, skill_name: str) -> list[dict[str, Any]]:
    tools_path = skill_dir / "tools.py"
    if not tools_path.is_file():
        return []
    try:
        module = _import_module_from_path(
            LoadedSkill(
                name=skill_name,
                path=str(skill_dir),
                root=str(skill_dir.parent),
                origin=SkillOrigin.USER,
                mutable=True,
                state=SkillState.LOADED,
            ),
            "tools",
            tools_path,
        )
    except Exception:
        return []
    declarations = getattr(module, "tool_declarations", None)
    if callable(declarations):
        try:
            raw = declarations()
        except Exception:
            raw = []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def _skill_md_description(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if stripped.startswith("description:"):
                return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped != "---":
            return stripped[:200]
    return ""


def _skill_md_body(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1:]).lstrip() + ("\n" if index + 1 < len(lines) else "")
    return "\n".join(lines) + ("\n" if lines else "")


def _normalize_skill_name(name: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in name.strip().lower()
    )
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized or "user_skill"


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values
