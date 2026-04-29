"""Filesystem system skill tools."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext


def build_executors(ctx: Any) -> dict[str, Any]:
    return filesystem_tool_executors(ctx.permission_context)


def filesystem_tool_executors(context: PermissionContext) -> dict[str, Any]:
    return {
        "filesystem.list": lambda arguments: list_entries(arguments, context),
        "filesystem.read": lambda arguments: read_text(arguments, context),
        "filesystem.write": lambda arguments: write_text(arguments, context),
        "filesystem.mkdir": lambda arguments: make_directory(arguments, context),
        "filesystem.move": lambda arguments: move_path(arguments, context),
        "maurice.system_skills.filesystem.tools.list_entries": lambda arguments: list_entries(arguments, context),
        "maurice.system_skills.filesystem.tools.read_text": lambda arguments: read_text(arguments, context),
        "maurice.system_skills.filesystem.tools.write_text": lambda arguments: write_text(arguments, context),
        "maurice.system_skills.filesystem.tools.make_directory": lambda arguments: make_directory(arguments, context),
        "maurice.system_skills.filesystem.tools.move_path": lambda arguments: move_path(arguments, context),
    }


def list_entries(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    raw_path = arguments.get("path")
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if not path.exists():
        return _error("not_found", _not_found_summary(raw_path, context))
    if not path.is_dir():
        return _error("not_directory", f"Je l'ai trouve, mais ce n'est pas un dossier : `{path.name}`.")

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
    summary = _list_summary(entries)
    return ToolResult(
        ok=True,
        summary=summary,
        data={"path": str(path), "entries": entries},
        trust="local_mutable",
        artifacts=[{"type": "directory", "path": str(path)}],
        events=[{"name": "filesystem.directory_listed", "payload": {"path": str(path)}}],
        error=None,
    )


def read_text(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    raw_path = arguments.get("path")
    try:
        path = _resolve_path(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if not path.exists():
        return _error("not_found", _not_found_summary(raw_path, context))
    if not path.is_file():
        return _error("not_file", f"Je l'ai trouve, mais ce n'est pas un fichier : `{path.name}`.")

    try:
        content = path.read_text(encoding=arguments.get("encoding", "utf-8"))
    except UnicodeDecodeError as exc:
        return _error("decode_error", f"Could not decode file as text: {exc}")

    return ToolResult(
        ok=True,
        summary=f"J'ai lu `{path.name}`.",
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
        summary=f"C'est écrit dans `{path.name}`.",
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
        summary=f"Dossier prêt : `{path.name}`.",
        data={"path": str(path)},
        trust="local_mutable",
        artifacts=[{"type": "directory", "path": str(path)}],
        events=[{"name": "filesystem.directory_created", "payload": {"path": str(path)}}],
        error=None,
    )


def move_path(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        source = _resolve_path({"path": arguments.get("source_path")}, context)
        target = _resolve_path({"path": arguments.get("target_path")}, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    overwrite = arguments.get("overwrite") is True
    if not source.exists():
        return _error("not_found", _not_found_summary(arguments.get("source_path"), context))
    if target.exists() and not overwrite:
        return _error(
            "target_exists",
            f"`{target.name}` existe deja. Je ne l'ecrase pas sans `overwrite: true`.",
        )
    if target.exists() and overwrite:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    moved = shutil.move(str(source), str(target))
    moved_path = Path(moved).resolve()
    return ToolResult(
        ok=True,
        summary=f"J'ai deplace `{source.name}` vers `{target.name}`.",
        data={"source_path": str(source), "target_path": str(moved_path)},
        trust="local_mutable",
        artifacts=[{"type": "path", "path": str(moved_path)}],
        events=[
            {
                "name": "filesystem.path_moved",
                "payload": {"source_path": str(source), "target_path": str(moved_path)},
            }
        ],
        error=None,
    )


def _resolve_path(arguments: dict[str, Any], context: PermissionContext) -> Path:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("filesystem tools require a non-empty path")
    variables = context.variables()
    workspace = Path(variables["$workspace"])
    default_root = Path(variables.get("$project") or variables.get("$agent_content") or workspace / "content")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        first_part = candidate.parts[0] if candidate.parts else ""
        if first_part == "$project":
            candidate = Path(variables["$project"]).joinpath(*candidate.parts[1:])
        elif first_part == "$agent_content":
            candidate = Path(variables["$agent_content"]).joinpath(*candidate.parts[1:])
        elif first_part == "$agent_workspace":
            candidate = Path(variables["$agent_workspace"]).joinpath(*candidate.parts[1:])
        elif first_part in {"agents", "config", "content", "sessions", "skills"}:
            candidate = workspace / candidate
        elif _looks_like_current_project_name(candidate, variables):
            candidate = Path(variables["$project"])
        else:
            candidate = default_root / candidate
    return candidate.resolve()


def _looks_like_current_project_name(candidate: Path, variables: dict[str, str]) -> bool:
    project = Path(variables.get("$project") or "")
    return len(candidate.parts) == 1 and project.name and candidate.parts[0] == project.name


def _list_summary(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "Dossier trouvé, il est vide."
    if len(entries) == 1:
        entry = entries[0]
        label = "dossier" if entry["type"] == "directory" else "fichier"
        return f"J'ai trouvé le {label} `{entry['name']}`."
    names = ", ".join(f"`{entry['name']}`" for entry in entries[:12])
    suffix = ", ..." if len(entries) > 12 else ""
    return f"J'ai trouvé {len(entries)} éléments : {names}{suffix}."


def _not_found_summary(raw_path: Any, context: PermissionContext) -> str:
    requested = str(raw_path or "").strip() or "ce chemin"
    variables = context.variables()
    project = Path(variables.get("$project") or "")
    if project.name:
        return (
            f"Je ne trouve pas `{requested}` dans le projet actif `{project.name}`. "
            "Pour parler du dossier du projet lui-même, utilise `.`."
        )
    return f"Je ne trouve pas `{requested}`."


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
