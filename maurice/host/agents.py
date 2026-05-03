"""Permanent agent config management."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from maurice.host.paths import agents_config_path
from maurice.kernel.config import ConfigBundle, load_workspace_config, write_yaml_file
from maurice.kernel.contracts import AgentConfig
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import agent_profile_requires_confirmation

AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def list_agents(workspace_root: str | Path) -> list[AgentConfig]:
    bundle = load_workspace_config(workspace_root)
    return sorted(bundle.agents.agents.values(), key=lambda agent: agent.id)


def create_agent(
    workspace_root: str | Path,
    *,
    agent_id: str,
    permission_profile: str | None = None,
    skills: list[str] | None = None,
    credentials: list[str] | None = None,
    channels: list[str] | None = None,
    model: dict[str, Any] | None = None,
    make_default: bool = False,
    confirmed_permission_elevation: bool = False,
) -> AgentConfig:
    _validate_agent_id(agent_id)
    bundle = load_workspace_config(workspace_root)
    if agent_id in bundle.agents.agents:
        raise ValueError(f"Agent already exists: {agent_id}")

    profile = permission_profile or bundle.kernel.permissions.profile
    _validate_profile_allowed(
        bundle,
        profile,
        confirmed_permission_elevation=confirmed_permission_elevation,
    )

    workspace = Path(bundle.host.workspace_root)
    agent_workspace = workspace / "agents" / agent_id
    agent_workspace.mkdir(parents=True, exist_ok=False)
    for relative in ("content", "memory", "dreams", "reminders"):
        (agent_workspace / relative).mkdir(parents=True, exist_ok=True)
    agent = AgentConfig(
        id=agent_id,
        default=make_default,
        workspace=str(agent_workspace),
        skills=skills if skills is not None else list(bundle.kernel.skills),
        credentials=credentials or [],
        permission_profile=profile,
        status="active",
        channels=channels or [],
        model=model,
        event_stream=str(agent_workspace / "events.jsonl"),
    )
    bundle.agents.agents[agent_id] = agent
    if make_default:
        _set_only_default(bundle, agent_id)
    _write_agents_config(workspace, bundle)
    _emit_agent_event(agent, "agent.created", {"agent": agent.model_dump(mode="json")})
    return agent


def update_agent(
    workspace_root: str | Path,
    *,
    agent_id: str,
    permission_profile: str | None = None,
    skills: list[str] | None = None,
    credentials: list[str] | None = None,
    channels: list[str] | None = None,
    model: dict[str, Any] | None = None,
    clear_model: bool = False,
    make_default: bool | None = None,
    confirmed_permission_elevation: bool = False,
) -> AgentConfig:
    bundle = load_workspace_config(workspace_root)
    try:
        current = bundle.agents.agents[agent_id]
    except KeyError as exc:
        raise KeyError(f"Unknown agent: {agent_id}") from exc

    profile = permission_profile or current.permission_profile
    _validate_profile_allowed(
        bundle,
        profile,
        confirmed_permission_elevation=confirmed_permission_elevation,
    )
    updated = current.model_copy(
        update={
            "permission_profile": profile,
            "skills": skills if skills is not None else current.skills,
            "credentials": credentials if credentials is not None else current.credentials,
            "channels": channels if channels is not None else current.channels,
            "model": None if clear_model else model if model is not None else current.model,
            "default": make_default if make_default is not None else current.default,
        }
    )
    bundle.agents.agents[agent_id] = updated
    if updated.default:
        _set_only_default(bundle, agent_id)
    _write_agents_config(Path(bundle.host.workspace_root), bundle)
    Path(updated.workspace).mkdir(parents=True, exist_ok=True)
    _emit_agent_event(updated, "agent.updated", {"agent": updated.model_dump(mode="json")})
    return updated


def disable_agent(workspace_root: str | Path, *, agent_id: str) -> AgentConfig:
    return _set_agent_status(workspace_root, agent_id=agent_id, status="disabled")


def archive_agent(workspace_root: str | Path, *, agent_id: str) -> AgentConfig:
    return _set_agent_status(workspace_root, agent_id=agent_id, status="archived")


def delete_agent(
    workspace_root: str | Path,
    *,
    agent_id: str,
    confirmed: bool = False,
) -> AgentConfig:
    if not confirmed:
        raise PermissionError("Deleting an agent is destructive; pass --confirm.")
    bundle = load_workspace_config(workspace_root)
    try:
        agent = bundle.agents.agents[agent_id]
    except KeyError as exc:
        raise KeyError(f"Unknown agent: {agent_id}") from exc
    _ensure_removable(bundle, agent)

    workspace = Path(bundle.host.workspace_root)
    _emit_agent_event(agent, "agent.deleted", {"agent": agent.model_dump(mode="json")})
    del bundle.agents.agents[agent_id]
    _write_agents_config(workspace, bundle)
    shutil.rmtree(agent.workspace, ignore_errors=True)
    return agent


def _validate_agent_id(agent_id: str) -> None:
    if not AGENT_ID_RE.match(agent_id):
        raise ValueError("Agent id must be lowercase snake_case and start with a letter.")
    if agent_id == "default":
        raise ValueError("Agent id cannot be `default`.")


def _set_agent_status(
    workspace_root: str | Path,
    *,
    agent_id: str,
    status: str,
) -> AgentConfig:
    bundle = load_workspace_config(workspace_root)
    try:
        current = bundle.agents.agents[agent_id]
    except KeyError as exc:
        raise KeyError(f"Unknown agent: {agent_id}") from exc
    if status != "active":
        _ensure_removable(bundle, current)
    updated = current.model_copy(update={"status": status, "default": False if status != "active" else current.default})
    bundle.agents.agents[agent_id] = updated
    _write_agents_config(Path(bundle.host.workspace_root), bundle)
    _emit_agent_event(updated, f"agent.{status}", {"agent": updated.model_dump(mode="json")})
    return updated


def _ensure_removable(bundle: ConfigBundle, agent: AgentConfig) -> None:
    active_agents = [
        candidate
        for candidate in bundle.agents.agents.values()
        if candidate.status == "active" and candidate.id != agent.id
    ]
    if not active_agents:
        raise ValueError("Cannot remove the last active agent.")
    if agent.default:
        raise ValueError("Cannot remove the default agent; choose another default first.")


def _validate_profile_allowed(
    bundle: ConfigBundle,
    profile: str,
    *,
    confirmed_permission_elevation: bool,
) -> None:
    if agent_profile_requires_confirmation(
        bundle.kernel.permissions.profile,
        profile,  # type: ignore[arg-type]
        confirmed=confirmed_permission_elevation,
    ):
        raise PermissionError(
            "Agent permission profile is more permissive than the global profile; "
            "pass --confirm-permission-elevation to confirm."
        )


def _set_only_default(bundle: ConfigBundle, agent_id: str) -> None:
    for candidate in bundle.agents.agents.values():
        candidate.default = candidate.id == agent_id


def _write_agents_config(workspace: Path, bundle: ConfigBundle) -> None:
    write_yaml_file(
        agents_config_path(workspace),
        {
            "agents": {
                agent_id: agent.model_dump(mode="json")
                for agent_id, agent in sorted(bundle.agents.agents.items())
            }
        },
    )


def _emit_agent_event(agent: AgentConfig, name: str, payload: dict[str, Any]) -> None:
    event_stream = Path(agent.event_stream) if agent.event_stream else Path(agent.workspace) / "events.jsonl"
    EventStore(event_stream).emit(
        name=name,
        origin="host",
        agent_id=agent.id,
        session_id="config",
        payload=payload,
    )
