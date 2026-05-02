"""Memory system skill tools."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maurice.kernel.contracts import DreamInput, ToolResult
from maurice.kernel.permissions import PermissionContext


def build_executors(ctx: Any) -> dict[str, Any]:
    memory_path = ctx.hooks.memory_path or None
    return memory_tool_executors(ctx.permission_context, memory_path=memory_path)


def memory_tool_executors(
    context: PermissionContext,
    *,
    memory_path: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "memory.remember": lambda arguments: remember(arguments, context, memory_path=memory_path),
        "memory.search": lambda arguments: search(arguments, context, memory_path=memory_path),
        "memory.get": lambda arguments: get(arguments, context, memory_path=memory_path),
        "maurice.system_skills.memory.tools.remember": lambda arguments: remember(
            arguments, context, memory_path=memory_path
        ),
        "maurice.system_skills.memory.tools.search": lambda arguments: search(
            arguments, context, memory_path=memory_path
        ),
        "maurice.system_skills.memory.tools.get": lambda arguments: get(
            arguments, context, memory_path=memory_path
        ),
    }


def remember(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    memory_path: str | Path | None = None,
) -> ToolResult:
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return _error("invalid_arguments", "memory.remember requires non-empty content.")

    tags = arguments.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return _error("invalid_arguments", "memory.remember tags must be strings.")

    source = arguments.get("source")
    if source is not None and not isinstance(source, str):
        return _error("invalid_arguments", "memory.remember source must be a string.")

    memory_id = f"mem_{uuid4().hex}"
    now = datetime.now(UTC).isoformat()
    with _connect(context, memory_path=memory_path) as connection:
        _init_db(connection)
        connection.execute(
            """
            INSERT INTO memories (id, content, tags_json, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, json.dumps(tags), source, now, now),
        )

    return ToolResult(
        ok=True,
        summary=f"Memory stored: {memory_id}",
        data={"id": memory_id, "content": content, "tags": tags, "source": source},
        trust="local_mutable",
        artifacts=[{"type": "sqlite", "path": str(_db_path(context, memory_path=memory_path))}],
        events=[{"name": "memory.remembered", "payload": {"id": memory_id}}],
        error=None,
    )


def search(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    memory_path: str | Path | None = None,
) -> ToolResult:
    query = arguments.get("query")
    if not isinstance(query, str):
        return _error("invalid_arguments", "memory.search requires query.")
    limit = arguments.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        return _error("invalid_arguments", "memory.search limit must be a positive integer.")

    pattern = f"%{query}%"
    with _connect(context, memory_path=memory_path) as connection:
        _init_db(connection)
        rows = connection.execute(
            """
            SELECT id, content, tags_json, source, created_at, updated_at
            FROM memories
            WHERE content LIKE ? OR tags_json LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()

    memories = [_row_to_memory(row) for row in rows]
    return ToolResult(
        ok=True,
        summary=f"Found {len(memories)} memories.",
        data={"query": query, "memories": memories},
        trust="local_mutable",
        artifacts=[{"type": "sqlite", "path": str(_db_path(context, memory_path=memory_path))}],
        events=[{"name": "memory.searched", "payload": {"query": query, "count": len(memories)}}],
        error=None,
    )


def get(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    memory_path: str | Path | None = None,
) -> ToolResult:
    memory_id = arguments.get("id")
    if not isinstance(memory_id, str) or not memory_id:
        return _error("invalid_arguments", "memory.get requires id.")

    with _connect(context, memory_path=memory_path) as connection:
        _init_db(connection)
        row = connection.execute(
            """
            SELECT id, content, tags_json, source, created_at, updated_at
            FROM memories
            WHERE id = ?
            """,
            (memory_id,),
        ).fetchone()

    if row is None:
        return _error("not_found", f"Memory not found: {memory_id}")

    memory = _row_to_memory(row)
    return ToolResult(
        ok=True,
        summary=f"Memory fetched: {memory_id}",
        data={"memory": memory},
        trust="local_mutable",
        artifacts=[{"type": "sqlite", "path": str(_db_path(context, memory_path=memory_path))}],
        events=[{"name": "memory.fetched", "payload": {"id": memory_id}}],
        error=None,
    )


def build_dream_input(context: PermissionContext, *, limit: int = 10) -> DreamInput:
    now = datetime.now(UTC)
    with _connect(context) as connection:
        _init_db(connection)
        rows = connection.execute(
            """
            SELECT id, content, tags_json, source, created_at, updated_at
            FROM memories
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    signals = []
    for row in rows:
        memory = _row_to_memory(row)
        signals.append(
            {
                "id": f"sig_{memory['id']}",
                "type": "memory_review",
                "summary": memory["content"],
                "data": {"memory": memory},
            }
        )
    return DreamInput(
        skill="memory",
        trust="local_mutable",
        freshness={"generated_at": now, "expires_at": None},
        signals=signals,
        limits=["Includes the most recent durable memories only."],
    )


def _connect(
    context: PermissionContext,
    *,
    memory_path: str | Path | None = None,
) -> sqlite3.Connection:
    path = _db_path(context, memory_path=memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY,
          content TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          source TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)"
    )


def _db_path(context: PermissionContext, *, memory_path: str | Path | None = None) -> Path:
    if memory_path:
        return Path(memory_path).expanduser().resolve()
    return Path(context.variables()["$workspace"]) / "skills" / "memory" / "memory.sqlite"


def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "tags": json.loads(row["tags_json"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": False},
    )
