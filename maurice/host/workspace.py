"""Workspace initialization."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from maurice.host.credentials import ensure_workspace_credentials_migrated
from maurice.host.paths import (
    agents_config_path,
    ensure_workspace_config_migrated,
    host_config_path,
    kernel_config_path,
    workspace_skills_config_path,
)
from maurice.kernel.config import KernelConfig, write_yaml_file


WORKSPACE_DIRS = (
    "agents/main",
    "agents/main/content",
    "agents/main/memory",
    "agents/main/dreams",
    "agents/main/reminders",
    "skills",
    "sessions",
)

DEFAULT_SEARXNG_URL = "http://localhost:18080"


def default_web_search_config() -> dict[str, str]:
    return {"search_provider": "searxng", "base_url": DEFAULT_SEARXNG_URL}


def initialize_workspace(
    workspace_root: str | Path,
    runtime_root: str | Path,
    permission_profile: Literal["safe", "limited", "power"] = "safe",
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    runtime = Path(runtime_root).expanduser().resolve()

    if workspace == runtime:
        raise ValueError("workspace root and runtime root must be distinct")

    for relative in WORKSPACE_DIRS:
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    ensure_workspace_content_migrated(workspace)
    ensure_agent_memory_migrated(workspace, agent_id="main")
    ensure_agent_user_file(workspace / "agents" / "main")
    ensure_workspace_config_migrated(workspace)

    host_config = {
        "host": {
            "runtime_root": str(runtime),
            "workspace_root": str(workspace),
            "gateway": {"host": "127.0.0.1", "port": 18791},
            "development": {"web_agent_switching": False},
            "skill_roots": [
                {
                    "path": str(runtime / "maurice" / "system_skills"),
                    "origin": "system",
                    "mutable": False,
                },
                {
                    "path": str(workspace / "skills"),
                    "origin": "user",
                    "mutable": True,
                },
            ],
            "channels": {
                "local_http": {
                    "adapter": "local_http",
                    "enabled": True,
                    "agent": "main",
                    "credential": None,
                }
            },
        }
    }
    kernel = KernelConfig()
    kernel.permissions.profile = permission_profile
    kernel_config = {"kernel": kernel.model_dump(mode="json")}
    agents_config = {
        "agents": {
            "main": {
                "id": "main",
                "default": True,
                "workspace": str(workspace / "agents" / "main"),
                "skills": kernel.skills,
                "credentials": [],
                "permission_profile": permission_profile,
                "status": "active",
                "channels": [],
                "event_stream": str(workspace / "agents" / "main" / "events.jsonl"),
            }
        }
    }
    skills_config = {"skills": {"web": default_web_search_config()}}

    write_yaml_file(host_config_path(workspace), host_config)
    write_yaml_file(kernel_config_path(workspace), kernel_config)
    write_yaml_file(agents_config_path(workspace), agents_config)
    write_yaml_file(workspace_skills_config_path(workspace), skills_config)
    ensure_workspace_credentials_migrated(workspace)

    return workspace


def ensure_workspace_content_migrated(workspace_root: str | Path) -> Path:
    """Move legacy workspace-global content into the main agent workspace."""
    workspace = Path(workspace_root).expanduser().resolve()
    agent_root = workspace / "agents" / "main"
    agent_content = agent_root / "content"
    agent_content.mkdir(parents=True, exist_ok=True)

    legacy = workspace / "artifacts"
    content = workspace / "content"
    if legacy.exists():
        for item in legacy.iterdir():
            destination = agent_content / item.name
            if destination.exists():
                continue
            shutil.move(str(item), str(destination))
        try:
            legacy.rmdir()
        except OSError:
            pass

    if content.exists():
        _move_tree_contents(content / "dreams", agent_root / "dreams")
        _move_tree_contents(content / "reminders", agent_root / "reminders")
        for item in list(content.iterdir()):
            destination = agent_content / item.name
            if destination.exists():
                continue
            shutil.move(str(item), str(destination))
        try:
            content.rmdir()
        except OSError:
            pass
    return agent_content


def ensure_agent_user_file(agent_workspace: str | Path) -> Path:
    """Create the per-agent USER.md file if it is missing."""
    agent_root = Path(agent_workspace).expanduser().resolve()
    agent_root.mkdir(parents=True, exist_ok=True)
    user_path = agent_root / "USER.md"
    if not user_path.exists():
        user_path.write_text("", encoding="utf-8")
    return user_path


def ensure_agent_memory_migrated(
    workspace_root: str | Path,
    *,
    agent_id: str = "main",
    agent_workspace: str | Path | None = None,
) -> Path:
    """Resolve and migrate durable memory for one agent."""
    workspace = Path(workspace_root).expanduser().resolve()
    agent_root = (
        Path(agent_workspace).expanduser().resolve()
        if agent_workspace is not None
        else workspace / "agents" / agent_id
    )
    memory_dir = agent_root / "memory"
    current = memory_dir / "memory.sqlite"
    memory_dir.mkdir(parents=True, exist_ok=True)

    if agent_id == "main" and not current.exists():
        for legacy in (
            workspace / "memory" / "memory.sqlite",
            workspace / "skills" / "memory" / "memory.sqlite",
        ):
            if legacy.exists():
                legacy.replace(current)
                try:
                    legacy.parent.rmdir()
                except OSError:
                    pass
                break
    return current


def ensure_workspace_memory_migrated(workspace_root: str | Path) -> Path:
    """Backward-compatible wrapper for the main agent memory path."""
    return ensure_agent_memory_migrated(workspace_root, agent_id="main")


def _move_tree_contents(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if target.exists():
            continue
        shutil.move(str(item), str(target))
    try:
        source.rmdir()
    except OSError:
        pass
