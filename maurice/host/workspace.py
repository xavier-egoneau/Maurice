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
    "skills",
    "sessions",
    "content",
)


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
    ensure_workspace_config_migrated(workspace)

    host_config = {
        "host": {
            "runtime_root": str(runtime),
            "workspace_root": str(workspace),
            "gateway": {"host": "127.0.0.1", "port": 18791},
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
    skills_config = {"skills": {}}

    write_yaml_file(host_config_path(workspace), host_config)
    write_yaml_file(kernel_config_path(workspace), kernel_config)
    write_yaml_file(agents_config_path(workspace), agents_config)
    write_yaml_file(workspace_skills_config_path(workspace), skills_config)
    ensure_workspace_credentials_migrated(workspace)

    return workspace


def ensure_workspace_content_migrated(workspace_root: str | Path) -> Path:
    """Rename the legacy user-facing artifacts directory to content."""
    workspace = Path(workspace_root).expanduser().resolve()
    legacy = workspace / "artifacts"
    content = workspace / "content"
    if legacy.exists():
        if not content.exists():
            legacy.rename(content)
        else:
            for item in legacy.iterdir():
                destination = content / item.name
                if destination.exists():
                    continue
                shutil.move(str(item), str(destination))
            try:
                legacy.rmdir()
            except OSError:
                pass
    content.mkdir(parents=True, exist_ok=True)
    return content
