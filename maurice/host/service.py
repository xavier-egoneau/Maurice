"""Host installation checks and local service inspection."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from maurice import __version__
from maurice.host.credentials import credentials_path, ensure_workspace_credentials_migrated
from maurice.host.paths import (
    agents_config_path,
    host_config_path,
    kernel_config_path,
    workspace_skills_config_path,
)
from maurice.host.workspace import ensure_workspace_content_migrated
from maurice.kernel.config import ConfigBundle, load_workspace_config
from maurice.kernel.contracts import AgentConfig, Event, MauriceModel
from maurice.kernel.events import EventStore


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
    checks.append(
        HostCheck(
            name="credentials_store",
            state="ok" if ensure_workspace_credentials_migrated(workspace).exists() else "warn",
            summary=str(credentials_path()),
        )
    )

    return ServiceStatusReport(ok=all(check.state != "error" for check in checks), checks=checks)


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
    required_dirs = ["agents", "skills", "sessions", "content"]
    missing = [name for name in required_dirs if not (workspace / name).is_dir()]
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
