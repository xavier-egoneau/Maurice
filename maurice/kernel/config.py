"""Configuration models and loaders."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from maurice.host.paths import (
    agents_config_path,
    ensure_workspace_config_migrated,
    host_config_path,
    kernel_config_path,
    maurice_home,
    workspace_config_root,
    workspace_skills_config_path,
)
from maurice.kernel.contracts import AgentConfig


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class KernelModelConfig(ConfigModel):
    provider: str = "mock"
    protocol: str | None = None
    name: str = "mock"
    base_url: str | None = None
    credential: str | None = None


class ModelProfileConfig(KernelModelConfig):
    tier: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    privacy: Literal["local", "cloud", "unknown"] = "unknown"


class KernelModelsConfig(ConfigModel):
    default: str = "default"
    entries: dict[str, ModelProfileConfig] = Field(default_factory=dict)


class KernelPermissionsConfig(ConfigModel):
    profile: Literal["safe", "limited", "power"] = "safe"


class KernelApprovalsConfig(ConfigModel):
    mode: Literal["ask", "auto_deny", "auto"] = "ask"
    ttl_seconds: int = Field(default=1800, ge=1)
    remember_ttl_seconds: int = Field(default=600, ge=1)
    classifier_model: str = ""
    classifier_cache_ttl_seconds: int = Field(default=3600, ge=60)


class KernelSchedulerConfig(ConfigModel):
    enabled: bool = True
    dreaming_enabled: bool = True
    dreaming_time: str = "09:00"
    daily_enabled: bool = True
    daily_time: str = "09:30"

    @field_validator("dreaming_time", "daily_time")
    @classmethod
    def time_must_be_hhmm(cls, value: str) -> str:
        normalized = _normalize_time(value)
        if normalized is None:
            raise ValueError("time must look like HH:MM or 9h30")
        return normalized


class KernelEventsConfig(ConfigModel):
    retention_days: int = Field(default=30, ge=1)


class KernelSessionsConfig(ConfigModel):
    retention_days: int = Field(default=30, ge=1)
    compaction: bool = True
    context_window_tokens: int = Field(default=250_000, ge=1_000)
    trim_threshold: float = Field(default=0.60, gt=0.0, lt=1.0)
    summarize_threshold: float = Field(default=0.75, gt=0.0, lt=1.0)
    reset_threshold: float = Field(default=0.90, gt=0.0, lt=1.0)
    keep_recent_turns: int = Field(default=10, ge=1)


class SubagentTemplateConfig(ConfigModel):
    id: str
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    permission_profile: Literal["safe", "limited", "power"] = "safe"
    channels: list[str] = Field(default_factory=list)
    model_chain: list[str] = Field(default_factory=list)


class KernelSubagentsConfig(ConfigModel):
    templates: dict[str, SubagentTemplateConfig] = Field(default_factory=dict)


class KernelConfig(ConfigModel):
    models: KernelModelsConfig = Field(default_factory=KernelModelsConfig)
    permissions: KernelPermissionsConfig = Field(default_factory=KernelPermissionsConfig)
    approvals: KernelApprovalsConfig = Field(default_factory=KernelApprovalsConfig)
    skills: list[str] = Field(default_factory=list)
    scheduler: KernelSchedulerConfig = Field(default_factory=KernelSchedulerConfig)
    subagents: KernelSubagentsConfig = Field(default_factory=KernelSubagentsConfig)
    events: KernelEventsConfig = Field(default_factory=KernelEventsConfig)
    sessions: KernelSessionsConfig = Field(default_factory=KernelSessionsConfig)


def _normalize_time(value: str) -> str | None:
    raw = str(value or "").strip().lower()
    if "h" in raw:
        hour, _, minute = raw.partition("h")
        minute = minute or "00"
    elif ":" in raw:
        hour, _, minute = raw.partition(":")
    else:
        return None
    if not hour.isdigit() or not minute.isdigit():
        return None
    hour_int = int(hour)
    minute_int = int(minute)
    if hour_int > 23 or minute_int > 59:
        return None
    return f"{hour_int:02d}:{minute_int:02d}"


class GatewayConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=18791, ge=1, le=65535)


class HostDevelopmentConfig(ConfigModel):
    web_agent_switching: bool = False


class SkillRootConfig(ConfigModel):
    path: str
    origin: Literal["system", "user"]
    mutable: bool


class HostConfig(ConfigModel):
    runtime_root: str
    workspace_root: str
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    development: HostDevelopmentConfig = Field(default_factory=HostDevelopmentConfig)
    skill_roots: list[SkillRootConfig] = Field(default_factory=list)
    channels: dict[str, Any] = Field(default_factory=dict)

    @field_validator("runtime_root", "workspace_root")
    @classmethod
    def path_must_not_be_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("path must not be empty")
        return value


class AgentsConfig(ConfigModel):
    agents: dict[str, AgentConfig] = Field(default_factory=dict)


class SkillsConfig(ConfigModel):
    skills: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ConfigBundle(ConfigModel):
    host: HostConfig
    kernel: KernelConfig
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)


def read_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def write_yaml_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def load_workspace_config(workspace_root: str | Path) -> ConfigBundle:
    root = Path(workspace_root).expanduser().resolve()
    ensure_workspace_config_migrated(root)
    _recover_orphaned_workspace_config(root)
    _migrate_model_schema(root)

    host_data = read_yaml_file(host_config_path(root)).get("host", {})
    kernel_data = read_yaml_file(kernel_config_path(root)).get("kernel", {})
    agents_data = read_yaml_file(agents_config_path(root))
    skills_data = read_yaml_file(workspace_skills_config_path(root))

    bundle = ConfigBundle(
        host=HostConfig.model_validate(host_data),
        kernel=KernelConfig.model_validate(kernel_data),
        agents=AgentsConfig.model_validate(agents_data),
        skills=SkillsConfig.model_validate(skills_data),
    )
    _normalize_model_profiles(bundle)
    _normalize_context_window(bundle)
    return bundle


def _recover_orphaned_workspace_config(root: Path) -> None:
    """Merge agents from older state roots that still point at this workspace.

    Earlier development builds could leave more than one host-owned config root
    for the same physical workspace. The current root remains authoritative, but
    missing agents from those older roots should not disappear from the web chat.
    """
    workspaces_root = maurice_home() / "workspaces"
    current_config_root = workspace_config_root(root)
    if not workspaces_root.exists():
        return

    agents_path = agents_config_path(root)
    host_path = host_config_path(root)
    kernel_path = kernel_config_path(root)
    agents_data = read_yaml_file(agents_path)
    host_data = read_yaml_file(host_path)
    kernel_data = read_yaml_file(kernel_path)
    current_agents = agents_data.setdefault("agents", {})
    if not isinstance(current_agents, dict):
        return

    changed_agents = False
    changed_host = False
    changed_kernel = False
    for candidate_config_root in sorted(workspaces_root.glob("*/config")):
        if candidate_config_root.resolve() == current_config_root.resolve():
            continue
        source_host_data = read_yaml_file(candidate_config_root / "host.yaml")
        if _config_workspace_root(source_host_data) != root:
            continue
        source_agents = read_yaml_file(candidate_config_root / "agents.yaml").get("agents")
        if not isinstance(source_agents, dict):
            continue

        recovered_ids: set[str] = set()
        for agent_id, raw_agent in source_agents.items():
            if agent_id in current_agents or not isinstance(raw_agent, dict):
                continue
            if not _recoverable_agent_workspace(root, agent_id, raw_agent):
                continue
            current_agents[agent_id] = raw_agent
            recovered_ids.add(str(agent_id))
            changed_agents = True

        if not recovered_ids:
            continue
        changed_kernel = _merge_source_model_profiles(
            kernel_data,
            read_yaml_file(candidate_config_root / "kernel.yaml"),
        ) or changed_kernel
        changed_host = _merge_source_agent_channels(host_data, source_host_data, recovered_ids) or changed_host

    if changed_agents:
        write_yaml_file(agents_path, agents_data)
    if changed_kernel:
        write_yaml_file(kernel_path, kernel_data)
    if changed_host:
        write_yaml_file(host_path, host_data)


def _config_workspace_root(host_data: dict[str, Any]) -> Path | None:
    host = host_data.get("host")
    if not isinstance(host, dict):
        return None
    workspace_root = host.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        return None
    return Path(workspace_root).expanduser().resolve()


def _recoverable_agent_workspace(root: Path, agent_id: Any, raw_agent: dict[str, Any]) -> bool:
    configured = raw_agent.get("workspace")
    workspace = Path(configured).expanduser().resolve() if isinstance(configured, str) and configured else root / "agents" / str(agent_id)
    if not workspace.exists():
        return False
    try:
        workspace.relative_to(root)
    except ValueError:
        return False
    return True


def _merge_source_model_profiles(target_kernel_data: dict[str, Any], source_kernel_data: dict[str, Any]) -> bool:
    source_kernel = source_kernel_data.get("kernel")
    if not isinstance(source_kernel, dict):
        return False
    source_models = source_kernel.get("models")
    if not isinstance(source_models, dict):
        return False
    source_entries = source_models.get("entries")
    if not isinstance(source_entries, dict):
        return False

    target_kernel = target_kernel_data.setdefault("kernel", {})
    if not isinstance(target_kernel, dict):
        return False
    target_models = target_kernel.setdefault("models", {})
    if not isinstance(target_models, dict):
        target_models = {}
        target_kernel["models"] = target_models
    target_entries = target_models.setdefault("entries", {})
    if not isinstance(target_entries, dict):
        target_entries = {}
        target_models["entries"] = target_entries

    changed = False
    for profile_id, payload in source_entries.items():
        if profile_id not in target_entries and isinstance(payload, dict):
            target_entries[profile_id] = payload
            changed = True
    return changed


def _merge_source_agent_channels(
    target_host_data: dict[str, Any],
    source_host_data: dict[str, Any],
    recovered_agent_ids: set[str],
) -> bool:
    source_host = source_host_data.get("host")
    source_channels = source_host.get("channels") if isinstance(source_host, dict) else None
    if not isinstance(source_channels, dict):
        return False

    target_host = target_host_data.setdefault("host", {})
    if not isinstance(target_host, dict):
        return False
    target_channels = target_host.setdefault("channels", {})
    if not isinstance(target_channels, dict):
        target_channels = {}
        target_host["channels"] = target_channels

    changed = False
    for name, channel in source_channels.items():
        if name in target_channels or not isinstance(channel, dict):
            continue
        if channel.get("agent") not in recovered_agent_ids:
            continue
        target_channels[name] = channel
        changed = True
    return changed


def default_model_config(bundle: ConfigBundle) -> dict[str, Any]:
    return _default_model_profile(bundle).model_dump(mode="json")


def model_profile_id(model: dict[str, Any]) -> str:
    provider = _slug_part(str(model.get("provider") or "mock"))
    name = _slug_part(str(model.get("name") or model.get("protocol") or "mock"))
    candidate = f"{provider}_{name}".strip("_")
    return candidate or "model"


def model_profile_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "provider": model.get("provider") or "mock",
        "protocol": model.get("protocol"),
        "name": model.get("name") or "mock",
        "base_url": model.get("base_url"),
        "credential": model.get("credential"),
        "tier": model.get("tier"),
        "capabilities": list(model.get("capabilities") or _infer_model_capabilities(model)),
        "privacy": model.get("privacy") or _infer_model_privacy(model),
    }
    return ModelProfileConfig.model_validate(payload).model_dump(mode="json")


def model_legacy_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "provider": model.get("provider") or "mock",
        "protocol": model.get("protocol"),
        "name": model.get("name") or "mock",
        "base_url": model.get("base_url"),
        "credential": model.get("credential"),
    }
    return KernelModelConfig.model_validate(payload).model_dump(mode="json")


def _slug_part(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or "model"


def _normalize_model_profiles(bundle: ConfigBundle) -> None:
    models = bundle.kernel.models
    if not models.entries:
        profile = model_profile_payload({})
        profile_id = model_profile_id(profile)
        models.entries[profile_id] = ModelProfileConfig.model_validate(profile)
        models.default = profile_id
    if not models.default:
        models.default = next(iter(models.entries), model_profile_id(model_profile_payload({})))
    if models.default not in models.entries:
        profile = model_profile_payload({})
        models.entries[models.default] = ModelProfileConfig.model_validate(profile)
    for model_id, profile in list(models.entries.items()):
        payload = model_profile_payload(profile.model_dump(mode="json"))
        models.entries[model_id] = ModelProfileConfig.model_validate(payload)


def _default_model_profile(bundle: ConfigBundle) -> ModelProfileConfig:
    return bundle.kernel.models.entries.get(bundle.kernel.models.default) or ModelProfileConfig.model_validate(
        model_profile_payload({})
    )


def _migrate_model_schema(root: Path) -> None:
    kernel_path = kernel_config_path(root)
    agents_path = agents_config_path(root)
    kernel_data = read_yaml_file(kernel_path)
    agents_data = read_yaml_file(agents_path)
    kernel = kernel_data.setdefault("kernel", {})
    if not isinstance(kernel, dict):
        return

    changed_kernel = False
    changed_agents = False
    models = kernel.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        kernel["models"] = models
        changed_kernel = True
    entries = models.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        models["entries"] = entries
        changed_kernel = True

    legacy_model = kernel.pop("model", None)
    if isinstance(legacy_model, dict):
        profile_id = _add_raw_model_profile(entries, legacy_model)
        models.setdefault("default", profile_id)
        changed_kernel = True

    agents = agents_data.get("agents")
    if isinstance(agents, dict):
        for raw_agent in agents.values():
            if not isinstance(raw_agent, dict):
                continue
            legacy_agent_model = raw_agent.pop("model", None)
            if isinstance(legacy_agent_model, dict):
                profile_id = _add_raw_model_profile(entries, legacy_agent_model)
                chain = raw_agent.get("model_chain")
                if not isinstance(chain, list) or not chain:
                    raw_agent["model_chain"] = [profile_id]
                changed_kernel = True
                changed_agents = True

    if not entries:
        payload = model_profile_payload({})
        entries[model_profile_id(payload)] = payload
        changed_kernel = True
    default_id = models.get("default")
    if not isinstance(default_id, str) or default_id not in entries:
        models["default"] = next(iter(entries))
        changed_kernel = True
    for profile_id, payload in list(entries.items()):
        if isinstance(payload, dict):
            normalized = model_profile_payload(payload)
            if payload != normalized:
                entries[profile_id] = normalized
                changed_kernel = True
    if changed_kernel:
        write_yaml_file(kernel_path, kernel_data)
    if changed_agents:
        write_yaml_file(agents_path, agents_data)


def _add_raw_model_profile(entries: dict[str, Any], model: dict[str, Any]) -> str:
    payload = model_profile_payload(model)
    profile_id = model_profile_id(payload)
    entries[profile_id] = payload
    return profile_id


def _infer_model_capabilities(model: dict[str, Any]) -> list[str]:
    capabilities = {"text"}
    name = str(model.get("name") or "").lower()
    provider = str(model.get("provider") or "").lower()
    protocol = str(model.get("protocol") or "").lower()
    if provider in {"api", "auth", "openai", "ollama"}:
        capabilities.add("tools")
    if "vision" in name or "llava" in name or "gemma4" in name or "gpt-4o" in name:
        capabilities.add("vision")
    if protocol == "chatgpt_codex":
        capabilities.add("tools")
    return sorted(capabilities)


def _infer_model_privacy(model: dict[str, Any]) -> Literal["local", "cloud", "unknown"]:
    provider = str(model.get("provider") or "").lower()
    base_url = str(model.get("base_url") or "").strip()
    if provider == "mock":
        return "local"
    if base_url:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return "local"
        return "cloud"
    if provider == "ollama":
        return "local"
    if provider in {"api", "auth", "openai"}:
        return "cloud"
    return "unknown"


def _normalize_context_window(bundle: ConfigBundle) -> None:
    sessions = bundle.kernel.sessions
    if sessions.context_window_tokens != 100_000:
        return
    model = _default_model_profile(bundle)
    provider = (model.provider or "").lower()
    protocol = (model.protocol or "").lower()
    credential = (model.credential or "").lower()
    base_url = (model.base_url or "").lower()
    if (
        provider in {"openai", "auth", "chatgpt"}
        or protocol == "chatgpt_codex"
        or credential in {"openai", "chatgpt"}
        or "api.openai.com" in base_url
        or "chatgpt.com" in base_url
    ):
        sessions.context_window_tokens = 250_000
