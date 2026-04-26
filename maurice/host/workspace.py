"""Workspace initialization."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from maurice.kernel.config import KernelConfig, write_yaml_file


WORKSPACE_DIRS = (
    "agents/main",
    "skills",
    "sessions",
    "artifacts",
    "config",
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

    write_yaml_file(workspace / "config" / "host.yaml", host_config)
    write_yaml_file(workspace / "config" / "kernel.yaml", kernel_config)
    write_yaml_file(workspace / "config" / "agents.yaml", agents_config)
    write_yaml_file(workspace / "config" / "skills.yaml", skills_config)

    credentials_path = workspace / "credentials.yaml"
    if not credentials_path.exists():
        write_yaml_file(credentials_path, {"credentials": {}})

    return workspace
