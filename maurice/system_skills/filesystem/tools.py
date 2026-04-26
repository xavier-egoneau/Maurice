"""Filesystem system skill tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext


def filesystem_tool_executors(context: PermissionContext) -> dict[str, Any]:
    return {
        "filesystem.list": lambda arguments: list_entries(arguments, context),
        "filesystem.read": lambda arguments: read_text(arguments, context),
        "filesystem.write": lambda arguments: write_text(arguments, context),
        "filesystem.mkdir": lambda arguments: make_directory(arguments, context),
        "maurice.system_skills.filesystem.tools.list_entries": lambda arguments: list_entries(arguments, context),
        "maurice.system_skills.filesystem.tools.read_text": lambda arguments: read_text(arguments, context),
        "maurice.system_skills.filesystem.tools.write_text": lambda arguments: write_text(arguments, context),
        "maurice.system_skills.filesystem.tools.make_directory": lambda arguments: make_directory(arguments, context),
    }


def list_entries(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if not path.exists():
        return _error("not_found", f"Path not found: {path}")
    if not path.is_dir():
        return _error("not_directory", f"Path is not a directory: {path}")

    entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return ToolResult(
        ok=True,
        summary=f"Listed {len(entries)} entries.",
        data={"path": str(path), "entries": entries},
        trust="local_mutable",
        artifacts=[{"type": "directory", "path": str(path)}],
        events=[{"name": "filesystem.directory_listed", "payload": {"path": str(path)}}],
        error=None,
    )


def read_text(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if not path.exists():
        return _error("not_found", f"File not found: {path}")
    if not path.is_file():
        return _error("not_file", f"Path is not a file: {path}")

    try:
        content = path.read_text(encoding=arguments.get("encoding", "utf-8"))
    except UnicodeDecodeError as exc:
        return _error("decode_error", f"Could not decode file as text: {exc}")

    return ToolResult(
        ok=True,
        summary=f"Read file: {path}",
        data={"path": str(path), "content": content},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(path)}],
        events=[{"name": "filesystem.file_read", "payload": {"path": str(path)}}],
        error=None,
    )


def write_text(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    content = arguments.get("content")
    if not isinstance(content, str):
        return _error("invalid_arguments", "filesystem.write requires string content.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=arguments.get("encoding", "utf-8"))

    return ToolResult(
        ok=True,
        summary=f"Wrote file: {path}",
        data={"path": str(path), "bytes": len(content.encode(arguments.get("encoding", "utf-8")))},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(path)}],
        events=[{"name": "filesystem.file_written", "payload": {"path": str(path)}}],
        error=None,
    )


def make_directory(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    path.mkdir(parents=True, exist_ok=True)
    return ToolResult(
        ok=True,
        summary=f"Created directory: {path}",
        data={"path": str(path)},
        trust="local_mutable",
        artifacts=[{"type": "directory", "path": str(path)}],
        events=[{"name": "filesystem.directory_created", "payload": {"path": str(path)}}],
        error=None,
    )


def _resolve_path(arguments: dict[str, Any], context: PermissionContext) -> Path:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("filesystem tools require a non-empty path")
    workspace = Path(context.variables()["$workspace"])
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve()


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
