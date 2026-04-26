"""Host-owned filesystem paths for Maurice."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path


def maurice_home() -> Path:
    return Path(os.environ.get("MAURICE_HOME", Path.home() / ".maurice")).expanduser().resolve()


def workspace_key(workspace_root: str | Path) -> str:
    workspace = Path(workspace_root).expanduser().resolve()
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", workspace.name).strip("-").lower() or "workspace"
    digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def workspace_state_root(workspace_root: str | Path) -> Path:
    return maurice_home() / "workspaces" / workspace_key(workspace_root)


def workspace_config_root(workspace_root: str | Path) -> Path:
    return workspace_state_root(workspace_root) / "config"


def workspace_skills_config_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "skills.yaml"


def host_config_path(workspace_root: str | Path) -> Path:
    return workspace_config_root(workspace_root) / "host.yaml"


def kernel_config_path(workspace_root: str | Path) -> Path:
    return workspace_config_root(workspace_root) / "kernel.yaml"


def agents_config_path(workspace_root: str | Path) -> Path:
    return workspace_config_root(workspace_root) / "agents.yaml"


def legacy_config_root(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "config"


def ensure_workspace_config_migrated(workspace_root: str | Path) -> Path:
    """Move host-owned config out of the agent workspace.

    Sensitive host/kernel/agents config now lives under ~/.maurice. User skill
    toggles remain workspace-owned as skills.yaml at the workspace root.
    """
    workspace = Path(workspace_root).expanduser().resolve()
    config_root = workspace_config_root(workspace)
    config_root.mkdir(parents=True, exist_ok=True)

    legacy = legacy_config_root(workspace)
    for filename, destination in (
        ("host.yaml", host_config_path(workspace)),
        ("kernel.yaml", kernel_config_path(workspace)),
        ("agents.yaml", agents_config_path(workspace)),
    ):
        _move_or_archive(legacy / filename, destination)

    _move_or_archive(legacy / "skills.yaml", workspace_skills_config_path(workspace))

    if legacy.exists():
        try:
            legacy.rmdir()
        except OSError:
            pass
    return config_root


def _move_or_archive(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.move(str(source), str(destination))
        return
    archived = source.with_suffix(source.suffix + ".migrated")
    if archived.exists():
        source.unlink()
    else:
        source.rename(archived)
