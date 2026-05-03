"""Host installation checks and local service inspection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from maurice import __version__
from maurice.host.client import _desired_server_meta, _server_meta_compatible
from maurice.host.context import MauriceContext, resolve_global_context
from maurice.host.credentials import (
    credentials_path,
    ensure_workspace_credentials_migrated,
    load_workspace_credentials,
)
from maurice.host.paths import (
    agents_config_path,
    host_config_path,
    kernel_config_path,
    workspace_skills_config_path,
)
from maurice.host.project_registry import list_known_projects, list_machine_projects
from maurice.host.workspace import ensure_workspace_content_migrated, ensure_workspace_memory_migrated
from maurice.kernel.config import ConfigBundle, load_workspace_config
from maurice.kernel.contracts import AgentConfig, Event, MauriceModel
from maurice.kernel.events import EventStore
from maurice.kernel.skills import SkillLoader


ServiceState = Literal["ok", "warn", "error"]


class HostCheck(MauriceModel):
    name: str
    state: ServiceState
    summary: str


class InstallReport(MauriceModel):
    ok: bool
    checks: list[HostCheck]


class ServiceStatusReport(MauriceModel):
    ok: bool
    checks: list[HostCheck]


class DoctorReport(MauriceModel):
    ok: bool
    checks: list[HostCheck]


def check_install(*, runtime_root: str | Path, workspace_root: str | Path | None = None) -> InstallReport:
    checks = [
        _python_check(),
        _path_check("runtime_root", runtime_root, must_be_dir=True),
        HostCheck(
            name="package",
            state="ok",
            summary=f"maurice {__version__} import OK",
        ),
    ]
    if workspace_root is not None:
        checks.extend(_workspace_checks(workspace_root))
    return InstallReport(ok=all(check.state != "error" for check in checks), checks=checks)


def inspect_doctor(
    *,
    runtime_root: str | Path,
    workspace_root: str | Path | None = None,
) -> DoctorReport:
    checks = [
        _python_check(),
        _path_check("runtime_root", runtime_root, must_be_dir=True),
        HostCheck(
            name="package",
            state="ok",
            summary=f"maurice {__version__} import OK",
        ),
    ]
    if workspace_root is None:
        return DoctorReport(ok=all(check.state != "error" for check in checks), checks=checks)

    checks.extend(_workspace_checks(workspace_root))
    if any(check.name == "workspace_config" and check.state == "error" for check in checks):
        return DoctorReport(ok=False, checks=checks)

    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    default_agent = _default_agent(bundle)
    checks.extend(_agent_checks(bundle, default_agent))
    checks.append(_model_profiles_check(bundle))
    checks.append(_credentials_check(workspace))
    checks.append(_skills_check(bundle))
    checks.append(_project_registry_check(bundle))
    checks.append(_legacy_paths_check(workspace))
    checks.append(
        HostCheck(
            name="scheduler",
            state="ok" if bundle.kernel.scheduler.enabled else "warn",
            summary="enabled" if bundle.kernel.scheduler.enabled else "disabled",
        )
    )
    checks.append(
        HostCheck(
            name="gateway",
            state="ok",
            summary=f"{bundle.host.gateway.host}:{bundle.host.gateway.port}",
        )
    )
    checks.append(_telegram_check(bundle))
    checks.append(_daemon_context_check(resolve_global_context(workspace, agent=default_agent, bundle=bundle)))
    return DoctorReport(ok=all(check.state != "error" for check in checks), checks=checks)


def inspect_service_status(workspace_root: str | Path) -> ServiceStatusReport:
    checks = _workspace_checks(workspace_root)
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    default_agent = _default_agent(bundle)
    active_agents = [agent for agent in bundle.agents.agents.values() if agent.status == "active"]

    checks.append(
        HostCheck(
            name="default_agent",
            state="ok" if default_agent is not None else "error",
            summary=default_agent.id if default_agent is not None else "no active default agent",
        )
    )
    checks.append(
        HostCheck(
            name="agents",
            state="ok" if active_agents else "error",
            summary=f"{len(active_agents)} active / {len(bundle.agents.agents)} configured",
        )
    )
    checks.append(
        HostCheck(
            name="scheduler",
            state="ok" if bundle.kernel.scheduler.enabled else "warn",
            summary="enabled" if bundle.kernel.scheduler.enabled else "disabled",
        )
    )
    checks.append(
        HostCheck(
            name="gateway",
            state="ok",
            summary=f"{bundle.host.gateway.host}:{bundle.host.gateway.port}",
        )
    )
    checks.append(_daemon_context_check(resolve_global_context(workspace, agent=default_agent, bundle=bundle)))
    checks.append(
        HostCheck(
            name="credentials_store",
            state="ok" if ensure_workspace_credentials_migrated(workspace).exists() else "warn",
            summary=str(credentials_path()),
        )
    )

    return ServiceStatusReport(ok=all(check.state != "error" for check in checks), checks=checks)


def _agent_checks(bundle: ConfigBundle, default_agent: AgentConfig | None) -> list[HostCheck]:
    active_agents = [agent for agent in bundle.agents.agents.values() if agent.status == "active"]
    return [
        HostCheck(
            name="default_agent",
            state="ok" if default_agent is not None else "error",
            summary=default_agent.id if default_agent is not None else "no active default agent",
        ),
        HostCheck(
            name="agents",
            state="ok" if active_agents else "error",
            summary=f"{len(active_agents)} active / {len(bundle.agents.agents)} configured",
        ),
    ]


def _model_profiles_check(bundle: ConfigBundle) -> HostCheck:
    entries = bundle.kernel.models.entries
    default = bundle.kernel.models.default
    missing_chains = []
    for agent in bundle.agents.agents.values():
        for profile_id in agent.model_chain:
            if profile_id not in entries:
                missing_chains.append(f"{agent.id}:{profile_id}")
    if missing_chains:
        return HostCheck(
            name="model_profiles",
            state="error",
            summary="unknown profile(s): " + ", ".join(missing_chains),
        )
    return HostCheck(
        name="model_profiles",
        state="ok",
        summary=f"{len(entries)} configured; default={default}",
    )


def _credentials_check(workspace: Path) -> HostCheck:
    store = load_workspace_credentials(workspace)
    return HostCheck(
        name="credentials",
        state="ok",
        summary=f"{len(store.credentials)} configured; store={credentials_path()}",
    )


def _skills_check(bundle: ConfigBundle) -> HostCheck:
    try:
        registry = SkillLoader(
            bundle.host.skill_roots,
            enabled_skills=bundle.kernel.skills or None,
            scope="global",
        ).load()
    except Exception as exc:
        return HostCheck(name="skills", state="error", summary=str(exc))
    loaded = len(registry.loaded())
    total = len(registry.skills)
    unavailable = sum(1 for skill in registry.skills.values() if skill.state != "loaded")
    return HostCheck(
        name="skills",
        state="ok" if loaded else "error",
        summary=f"{loaded} loaded / {total} discovered; {unavailable} not loaded",
    )


def _project_registry_check(bundle: ConfigBundle) -> HostCheck:
    machine_count = len(list_machine_projects())
    agent_counts = {
        agent.id: len(list_known_projects(agent.workspace))
        for agent in bundle.agents.agents.values()
    }
    summary = f"machine={machine_count}; agents=" + ", ".join(
        f"{agent_id}:{count}" for agent_id, count in sorted(agent_counts.items())
    )
    return HostCheck(name="project_registry", state="ok", summary=summary)


def _legacy_paths_check(workspace: Path) -> HostCheck:
    legacy_paths = [
        workspace / "content",
        workspace / "artifacts",
        workspace / "memory",
        workspace / "config",
        workspace / "credentials.yaml",
    ]
    existing = [str(path) for path in legacy_paths if path.exists()]
    return HostCheck(
        name="legacy_paths",
        state="warn" if existing else "ok",
        summary="present: " + ", ".join(existing) if existing else "none",
    )


def _telegram_check(bundle: ConfigBundle) -> HostCheck:
    telegram_channels = [
        name
        for name, channel in bundle.host.channels.items()
        if isinstance(channel, dict)
        and channel.get("adapter") == "telegram"
        and channel.get("enabled", True)
    ]
    return HostCheck(
        name="telegram",
        state="ok",
        summary=f"{len(telegram_channels)} enabled channel(s)",
    )


def _daemon_context_check(ctx: MauriceContext) -> HostCheck:
    try:
        meta = json.loads(ctx.server_meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HostCheck(
            name="daemon_context",
            state="warn",
            summary="not running",
        )
    except json.JSONDecodeError:
        return HostCheck(
            name="daemon_context",
            state="error",
            summary=f"invalid metadata: {ctx.server_meta_path}",
        )
    if not _server_meta_compatible(meta, _desired_server_meta(ctx)):
        return HostCheck(
            name="daemon_context",
            state="error",
            summary="incompatible metadata",
        )
    return HostCheck(
        name="daemon_context",
        state="ok",
        summary=f"{ctx.scope}/{ctx.lifecycle} pid={meta.get('pid', '?')}",
    )


def read_service_logs(workspace_root: str | Path, *, agent_id: str | None = None, limit: int = 20) -> list[Event]:
    bundle = load_workspace_config(workspace_root)
    agent = _agent_for_logs(bundle, agent_id)
    event_stream = (
        Path(agent.event_stream).expanduser()
        if agent.event_stream
        else Path(bundle.host.workspace_root) / "agents" / agent.id / "events.jsonl"
    )
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return EventStore(event_stream).read_all()[-limit:]


def _python_check() -> HostCheck:
    version = sys.version_info
    state: ServiceState = "ok" if version >= (3, 12) else "error"
    return HostCheck(
        name="python",
        state=state,
        summary=f"{version.major}.{version.minor}.{version.micro} (requires >= 3.12)",
    )


def _workspace_checks(workspace_root: str | Path) -> list[HostCheck]:
    workspace = Path(workspace_root).expanduser().resolve()
    checks = [_path_check("workspace_root", workspace, must_be_dir=True)]
    try:
        bundle = load_workspace_config(workspace)
    except Exception as exc:
        checks.append(
            HostCheck(
                name="workspace_config",
                state="error",
                summary=str(exc),
            )
        )
        return checks

    ensure_workspace_content_migrated(workspace)
    ensure_workspace_memory_migrated(workspace)
    required_dirs = ["agents", "skills", "sessions"]
    for agent in bundle.agents.agents.values():
        agent_root = Path(agent.workspace).expanduser().resolve()
        required_dirs.extend(
            str(agent_root / relative)
            for relative in ("content", "memory", "dreams", "reminders")
        )
    missing = [
        name
        for name in required_dirs
        if not (Path(name) if Path(name).is_absolute() else workspace / name).is_dir()
    ]
    required_files = [
        workspace_skills_config_path(workspace),
        host_config_path(workspace),
        kernel_config_path(workspace),
        agents_config_path(workspace),
    ]
    missing_files = [str(path) for path in required_files if not path.is_file()]
    checks.append(
        HostCheck(
            name="workspace_dirs",
            state="error" if missing or missing_files else "ok",
            summary=(
                "missing: " + ", ".join([*missing, *missing_files])
                if missing or missing_files
                else "all required dirs and config files present"
            ),
        )
    )
    checks.append(_path_check("configured_runtime_root", Path(bundle.host.runtime_root), must_be_dir=True))
    return checks


def _path_check(name: str, path: str | Path, *, must_be_dir: bool) -> HostCheck:
    resolved = Path(path).expanduser().resolve()
    exists = resolved.is_dir() if must_be_dir else resolved.exists()
    return HostCheck(
        name=name,
        state="ok" if exists else "error",
        summary=str(resolved) if exists else f"missing: {resolved}",
    )


def _default_agent(bundle: ConfigBundle) -> AgentConfig | None:
    for agent in bundle.agents.agents.values():
        if agent.default and agent.status == "active":
            return agent
    agent = bundle.agents.agents.get("main")
    if agent is not None and agent.status == "active":
        return agent
    return None


def _agent_for_logs(bundle: ConfigBundle, agent_id: str | None) -> AgentConfig:
    if agent_id:
        try:
            agent = bundle.agents.agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"unknown agent: {agent_id}") from exc
    else:
        agent = _default_agent(bundle)
        if agent is None:
            raise ValueError("no active default agent configured")
    return agent
