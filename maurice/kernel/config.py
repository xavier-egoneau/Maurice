"""Configuration models and loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from maurice.kernel.contracts import AgentConfig


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class KernelModelConfig(ConfigModel):
    provider: str = "mock"
    protocol: str | None = None
    name: str = "mock"
    base_url: str | None = None
    credential: str | None = None


class KernelPermissionsConfig(ConfigModel):
    profile: Literal["safe", "limited", "power"] = "safe"


class KernelApprovalsConfig(ConfigModel):
    mode: Literal["ask", "auto_deny"] = "ask"
    ttl_seconds: int = Field(default=1800, ge=1)
    remember_ttl_seconds: int = Field(default=600, ge=1)


class KernelSchedulerConfig(ConfigModel):
    enabled: bool = True


class KernelEventsConfig(ConfigModel):
    retention_days: int = Field(default=30, ge=1)


class KernelSessionsConfig(ConfigModel):
    retention_days: int = Field(default=30, ge=1)
    compaction: bool = True


class KernelConfig(ConfigModel):
    model: KernelModelConfig = Field(default_factory=KernelModelConfig)
    permissions: KernelPermissionsConfig = Field(default_factory=KernelPermissionsConfig)
    approvals: KernelApprovalsConfig = Field(default_factory=KernelApprovalsConfig)
    skills: list[str] = Field(
        default_factory=lambda: [
            "filesystem",
            "memory",
            "dreaming",
            "skills",
            "self_update",
            "web",
            "host",
            "reminders",
            "vision",
        ]
    )
    scheduler: KernelSchedulerConfig = Field(default_factory=KernelSchedulerConfig)
    events: KernelEventsConfig = Field(default_factory=KernelEventsConfig)
    sessions: KernelSessionsConfig = Field(default_factory=KernelSessionsConfig)


class GatewayConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=18791, ge=1, le=65535)


class SkillRootConfig(ConfigModel):
    path: str
    origin: Literal["system", "user"]
    mutable: bool


class HostConfig(ConfigModel):
    runtime_root: str
    workspace_root: str
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
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
    config_root = root / "config"

    host_data = read_yaml_file(config_root / "host.yaml").get("host", {})
    kernel_data = read_yaml_file(config_root / "kernel.yaml").get("kernel", {})
    agents_data = read_yaml_file(config_root / "agents.yaml")
    skills_data = read_yaml_file(config_root / "skills.yaml")

    return ConfigBundle(
        host=HostConfig.model_validate(host_data),
        kernel=KernelConfig.model_validate(kernel_data),
        agents=AgentsConfig.model_validate(agents_data),
        skills=SkillsConfig.model_validate(skills_data),
    )
