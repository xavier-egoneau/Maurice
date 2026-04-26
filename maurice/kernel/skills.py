"""Strict skill discovery and registry loading."""

from __future__ import annotations

import shutil
from enum import StrEnum
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import ValidationError

from maurice.kernel.config import SkillRootConfig
from maurice.kernel.contracts import (
    MauriceModel,
    SkillManifest,
    SkillOrigin,
    ToolDeclaration,
)
from maurice.kernel.events import EventStore


class SkillState(StrEnum):
    LOADED = "loaded"
    DISABLED = "disabled"
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
    tools: list[ToolDeclaration] = []
    errors: list[str] = []
    suggested_fixes: list[str] = []
    missing_dependencies: list[str] = []


class SkillRegistry(MauriceModel):
    skills: dict[str, LoadedSkill]
    tools: dict[str, ToolDeclaration]

    def loaded(self) -> dict[str, LoadedSkill]:
        return {
            name: skill
            for name, skill in self.skills.items()
            if skill.state == SkillState.LOADED
        }


class SkillLoader:
    def __init__(
        self,
        roots: Iterable[SkillRoot | SkillRootConfig],
        *,
        enabled_skills: Iterable[str] | None = None,
        required_skills: Iterable[str] | None = None,
        available_credentials: Iterable[str] | None = None,
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
        for loaded in loaded_by_name.values():
            if loaded.state != SkillState.LOADED:
                self._emit_skill_event(loaded)
                continue
            registered_for_skill = []
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
            self._emit_skill_event(loaded)

        return SkillRegistry(skills=loaded_by_name, tools=tools)

    def _discover(self) -> list[tuple[Path, SkillRoot]]:
        discovered: list[tuple[Path, SkillRoot]] = []
        for root in self.roots:
            root_path = Path(root.path).expanduser()
            if not root_path.exists():
                continue
            for manifest_path in sorted(root_path.glob("*/skill.yaml")):
                discovered.append((manifest_path.parent, root))
        return discovered

    def _find_collisions(
        self, discovered: list[tuple[Path, SkillRoot]]
    ) -> dict[str, list[Path]]:
        names: dict[str, list[Path]] = {}
        for skill_dir, _root in discovered:
            name = self._manifest_name(skill_dir / "skill.yaml") or skill_dir.name
            names.setdefault(name, []).append(skill_dir)
        return {name: paths for name, paths in names.items() if len(paths) > 1}

    def _load_one(self, skill_dir: Path, root: SkillRoot) -> LoadedSkill:
        try:
            manifest = self._read_manifest(skill_dir / "skill.yaml")
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
                prompt=self._read_optional_text(skill_dir / "prompt.md"),
                dreams=self._read_optional_text(
                    skill_dir / manifest.dreams.attachment
                    if manifest.dreams and manifest.dreams.attachment
                    else skill_dir / "dreams.md"
                ),
                tools=self._tool_declarations(manifest),
            )
        except RequiredSkillError:
            raise
        except Exception as exc:
            fallback_name = self._manifest_name(skill_dir / "skill.yaml") or skill_dir.name
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
    def _read_manifest(path: Path) -> SkillManifest:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return SkillManifest.model_validate(data)

    @staticmethod
    def _manifest_name(path: Path) -> str | None:
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


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values
