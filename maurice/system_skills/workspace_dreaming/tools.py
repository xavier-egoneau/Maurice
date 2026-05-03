"""Cross-agent dreaming inputs."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from maurice.kernel.contracts import DreamInput
from maurice.kernel.permissions import PermissionContext

DEFAULT_LIMIT_PER_AGENT = 5


def build_dream_input(context: PermissionContext, *, limit_per_agent: int = DEFAULT_LIMIT_PER_AGENT) -> DreamInput:
    workspace = Path(context.variables()["$workspace"])
    now = datetime.now(UTC)
    signals = []
    for agent_dir in _agent_dirs(workspace):
        agent_id = agent_dir.name
        memories = _recent_memories(agent_dir / "memory" / "memory.sqlite", limit=limit_per_agent)
        projects = _known_projects(agent_dir / "projects.json")
        if not memories and not projects:
            continue
        summary_parts = []
        if memories:
            summary_parts.append("; ".join(memory["content"] for memory in memories[:3]))
        if projects:
            summary_parts.append(
                "projects: " + ", ".join(project.get("name") or project.get("path") or "unknown" for project in projects[:3])
            )
        signals.append(
            {
                "id": f"sig_workspace_agent_{agent_id}",
                "type": "agent_activity",
                "summary": f"{agent_id}: " + " | ".join(summary_parts),
                "data": {
                    "agent_id": agent_id,
                    "memories": memories,
                    "projects": projects,
                },
            }
        )

    return DreamInput(
        skill="workspace_dreaming",
        trust="local_mutable",
        freshness={"generated_at": now, "expires_at": None},
        signals=signals,
        limits=[
            "Reads recent memories and known projects from each agent workspace.",
            f"At most {limit_per_agent} memories per agent.",
        ],
    )


def _agent_dirs(workspace: Path) -> list[Path]:
    agents_dir = workspace / "agents"
    if not agents_dir.exists():
        return []
    return sorted(path for path in agents_dir.iterdir() if path.is_dir())


def _recent_memories(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        with connection:
            rows = connection.execute(
                """
                SELECT id, content, tags_json, source, created_at, updated_at
                FROM memories
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_row_to_memory(row) for row in rows]


def _known_projects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        raw = payload.get("projects", [])
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []
    return [project for project in raw if isinstance(project, dict)]


def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
    try:
        tags = json.loads(row["tags_json"])
    except json.JSONDecodeError:
        tags = []
    return {
        "id": row["id"],
        "content": row["content"],
        "tags": tags if isinstance(tags, list) else [],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
