"""Declarative setup contracts for skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import Field, field_validator, model_validator

from maurice.kernel.config import SkillRootConfig, _normalize_time, read_yaml_file, write_yaml_file
from maurice.kernel.contracts import MauriceModel
from maurice.kernel.skills import SkillRoot


class SkillSetupOption(MauriceModel):
    value: str
    label: str = ""


class SkillSetupField(MauriceModel):
    key: str
    label: str = ""
    description: str = ""
    type: Literal["boolean", "time", "string", "integer", "number", "choice"] = "string"
    default: Any = None
    required: bool = False
    section: str = "Configuration"
    options: list[SkillSetupOption] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def key_is_simple(cls, value: str) -> str:
        if not value or any(character.isspace() for character in value) or "." in value:
            raise ValueError("setup keys must be non-empty local names without spaces or dots")
        return value


class SkillSetupSchedule(MauriceModel):
    id: str
    label: str = ""
    tool: str
    enabled_key: str = "enabled"
    time_key: str = "time"
    default_enabled: bool = True
    default_time: str = "09:00"
    session_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    deliver: bool = True

    @field_validator("default_time")
    @classmethod
    def default_time_is_local_time(cls, value: str) -> str:
        normalized = _normalize_time(value)
        if normalized is None:
            raise ValueError("default_time must look like HH:MM or 9h30")
        return normalized


class SkillSetupManifest(MauriceModel):
    version: int = 1
    title: str = ""
    description: str = ""
    fields: list[SkillSetupField] = Field(default_factory=list)
    scheduler: list[SkillSetupSchedule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_config_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "config" in data and "fields" not in data:
            data = dict(data)
            data["fields"] = data.pop("config")
        return data


class LoadedSkillSetup(MauriceModel):
    skill: str
    path: str
    setup: SkillSetupManifest


def load_skill_setups(
    skill_roots: Iterable[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: Iterable[str] | None = None,
) -> list[LoadedSkillSetup]:
    enabled = set(enabled_skills) if enabled_skills is not None else None
    loaded: list[LoadedSkillSetup] = []
    seen: set[str] = set()
    for root in skill_roots:
        root_path = Path(root.path).expanduser()
        if not root_path.exists():
            continue
        for skill_dir in sorted(path for path in root_path.iterdir() if path.is_dir()):
            if not _has_skill_manifest(skill_dir):
                continue
            skill = _skill_name(skill_dir)
            if enabled is not None and skill not in enabled:
                continue
            if skill in seen:
                continue
            setup_path = skill_dir / "setup.json"
            if not setup_path.is_file():
                continue
            data = json.loads(setup_path.read_text(encoding="utf-8"))
            loaded.append(
                LoadedSkillSetup(
                    skill=skill,
                    path=str(setup_path),
                    setup=SkillSetupManifest.model_validate(data),
                )
            )
            seen.add(skill)
    return loaded


def ensure_skill_setup_config(
    workspace: Path,
    skill_roots: Iterable[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    setups = load_skill_setups(skill_roots, enabled_skills=enabled_skills)
    path = workspace / "skills.yaml"
    data = read_yaml_file(path)
    skills = data.setdefault("skills", {})
    changed = False
    for loaded in setups:
        config = skills.setdefault(loaded.skill, {})
        if not isinstance(config, dict):
            config = {}
            skills[loaded.skill] = config
            changed = True
        for key, value in default_values(loaded.setup).items():
            if key not in config:
                config[key] = value
                changed = True
    if changed:
        write_yaml_file(path, data)
    return {
        name: value
        for name, value in skills.items()
        if isinstance(value, dict)
    }


def skill_setup_status(
    workspace: Path,
    skill_roots: Iterable[SkillRoot | SkillRootConfig],
    *,
    enabled_skills: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    config_by_skill = ensure_skill_setup_config(workspace, skill_roots, enabled_skills=enabled_skills)
    result = []
    for loaded in load_skill_setups(skill_roots, enabled_skills=enabled_skills):
        config = config_by_skill.get(loaded.skill, {})
        result.append(
            {
                "skill": loaded.skill,
                "title": loaded.setup.title or loaded.skill,
                "description": loaded.setup.description,
                "fields": [
                    {
                        **field.model_dump(mode="json"),
                        "value": config.get(field.key, field.default),
                    }
                    for field in loaded.setup.fields
                ],
                "scheduler": [item.model_dump(mode="json") for item in loaded.setup.scheduler],
            }
        )
    return result


def apply_skill_setup_updates(
    workspace: Path,
    skill_roots: Iterable[SkillRoot | SkillRootConfig],
    updates: dict[str, Any],
    *,
    enabled_skills: Iterable[str] | None = None,
) -> None:
    setups = {loaded.skill: loaded.setup for loaded in load_skill_setups(skill_roots, enabled_skills=enabled_skills)}
    if not setups:
        return
    path = workspace / "skills.yaml"
    data = read_yaml_file(path)
    skills = data.setdefault("skills", {})
    for skill, raw_values in updates.items():
        if skill not in setups or not isinstance(raw_values, dict):
            continue
        known_fields = {field.key: field for field in setups[skill].fields}
        config = skills.setdefault(skill, {})
        if not isinstance(config, dict):
            config = {}
            skills[skill] = config
        for key, value in raw_values.items():
            field = known_fields.get(key)
            if field is None:
                continue
            config[key] = normalize_field_value(field, value)
    write_yaml_file(path, data)


def default_values(setup: SkillSetupManifest) -> dict[str, Any]:
    values = {field.key: normalize_field_value(field, field.default) for field in setup.fields}
    for schedule in setup.scheduler:
        values.setdefault(schedule.enabled_key, schedule.default_enabled)
        values.setdefault(schedule.time_key, schedule.default_time)
    return values


def normalize_field_value(field: SkillSetupField, value: Any) -> Any:
    if field.type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "oui"}
        return bool(value)
    if field.type == "time":
        normalized = _normalize_time(str(value or ""))
        if normalized is None:
            raise ValueError(f"{field.key} must look like HH:MM or 9h30")
        return normalized
    if field.type == "integer":
        return int(value)
    if field.type == "number":
        return float(value)
    if field.type == "choice":
        value = str(value)
        allowed = {option.value for option in field.options}
        if allowed and value not in allowed:
            raise ValueError(f"{field.key} must be one of: {', '.join(sorted(allowed))}")
        return value
    return "" if value is None else str(value)


def _has_skill_manifest(skill_dir: Path) -> bool:
    return (
        (skill_dir / "skill.yaml").is_file()
        or (skill_dir / "skill.md").is_file()
        or (skill_dir / "SKILL.md").is_file()
    )


def _skill_name(skill_dir: Path) -> str:
    path = skill_dir / "skill.yaml"
    if path.is_file():
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            name = data.get("name")
            if isinstance(name, str) and name:
                return name
        except Exception:
            pass
    return skill_dir.name.replace("-", "_")
