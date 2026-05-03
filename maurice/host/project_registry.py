"""Project registries for projects Maurice has explicitly seen."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from maurice.host.paths import maurice_home


REGISTRY_FILE = "projects.json"
MAX_PROJECTS = 50


def machine_registry_path() -> Path:
    return maurice_home() / REGISTRY_FILE


def registry_path(agent_workspace: str | Path) -> Path:
    return Path(agent_workspace).expanduser().resolve() / REGISTRY_FILE


def record_machine_project(project_root: str | Path) -> None:
    _record_project(machine_registry_path(), project_root)


def list_machine_projects() -> list[dict[str, str]]:
    return _list_projects(machine_registry_path())


def record_known_project(agent_workspace: str | Path, project_root: str | Path) -> None:
    """Remember a project root that was active for this agent.

    This is intentionally event-driven: callers pass a concrete active project.
    The registry never scans arbitrary parent directories.
    """
    _record_project(registry_path(agent_workspace), project_root)


def record_seen_project(agent_workspace: str | Path, project_root: str | Path) -> None:
    """Record a concrete active project in both machine and agent registries."""
    record_machine_project(project_root)
    record_known_project(agent_workspace, project_root)


def list_known_projects(agent_workspace: str | Path) -> list[dict[str, str]]:
    return _list_projects(registry_path(agent_workspace))


def known_project_by_name(agent_workspace: str | Path, name: str) -> Path | None:
    wanted = name.strip()
    if not wanted:
        return None
    for project in list_known_projects(agent_workspace):
        if project["name"] == wanted:
            return Path(project["path"]).expanduser().resolve()
    return None


def _record_project(path: Path, project_root: str | Path) -> None:
    project = Path(project_root).expanduser().resolve()
    payload = _read_payload(path)
    projects = _projects_by_path(payload)
    existing = projects.get(str(project), {})
    projects[str(project)] = {
        **existing,
        "name": project.name,
        "path": str(project),
        "last_seen_at": datetime.now(UTC).isoformat(),
    }
    ordered = sorted(
        projects.values(),
        key=lambda item: str(item.get("last_seen_at") or ""),
        reverse=True,
    )[:MAX_PROJECTS]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"projects": ordered}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _list_projects(path: Path) -> list[dict[str, str]]:
    payload = _read_payload(path)
    projects = [
        {
            "name": str(project["name"]),
            "path": str(project["path"]),
            "last_seen_at": str(project.get("last_seen_at") or ""),
        }
        for project in payload.get("projects", [])
        if isinstance(project, dict)
        and isinstance(project.get("name"), str)
        and isinstance(project.get("path"), str)
    ]
    return sorted(projects, key=lambda item: item["last_seen_at"], reverse=True)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"projects": []}
    return payload if isinstance(payload, dict) else {"projects": []}


def _projects_by_path(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    projects: dict[str, dict[str, str]] = {}
    for project in payload.get("projects", []):
        if not isinstance(project, dict):
            continue
        path = project.get("path")
        name = project.get("name")
        if not isinstance(path, str) or not isinstance(name, str):
            continue
        projects[str(Path(path).expanduser().resolve())] = {
            "name": name,
            "path": str(Path(path).expanduser().resolve()),
            "last_seen_at": str(project.get("last_seen_at") or ""),
        }
    return projects
