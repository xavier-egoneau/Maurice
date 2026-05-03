"""Scoped shell execution tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext

DEFAULT_TIMEOUT_SECONDS = 120
HARD_TIMEOUT_SECONDS = 900
MAX_OUTPUT_CHARS = 60_000
SECRET_ENV_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "PASS", "CREDENTIAL", "AUTH")
SAFE_ENV_NAMES = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "USER",
    "VIRTUAL_ENV",
}


def build_executors(ctx: Any) -> dict[str, Any]:
    return shell_tool_executors(ctx.permission_context)


def shell_tool_executors(context: PermissionContext) -> dict[str, Any]:
    return {
        "shell.exec": lambda arguments: run_shell(arguments, context),
        "maurice.system_skills.exec.tools.run_shell": lambda arguments: run_shell(
            arguments, context
        ),
    }


def run_shell(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return _error("invalid_arguments", "shell.exec requires a non-empty command.")
    try:
        cwd = _resolve_cwd(arguments.get("cwd"), context)
        timeout = _timeout(arguments.get("timeout_seconds"))
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))

    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=_sanitized_env(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _truncate(exc.stdout or "")
        stderr, stderr_truncated = _truncate(exc.stderr or "")
        return ToolResult(
            ok=False,
            summary=f"Command timed out after {timeout}s: `{_summary_command(command)}`.",
            data={
                "command": command,
                "cwd": str(cwd),
                "timeout_seconds": timeout,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
            },
            trust="local_mutable",
            artifacts=[],
            events=[
                {
                    "name": "shell.command_timed_out",
                    "payload": {"command": command, "cwd": str(cwd), "timeout_seconds": timeout},
                }
            ],
            error={"code": "timeout", "message": f"Command timed out after {timeout}s."},
        )
    except OSError as exc:
        return _error("execution_error", f"Could not execute command: {exc}")

    stdout, stdout_truncated = _truncate(completed.stdout)
    stderr, stderr_truncated = _truncate(completed.stderr)
    ok = completed.returncode == 0
    summary = (
        f"Command finished: `{_summary_command(command)}`."
        if ok
        else f"Command failed with exit code {completed.returncode}: `{_summary_command(command)}`."
    )
    return ToolResult(
        ok=ok,
        summary=summary,
        data={
            "command": command,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        },
        trust="local_mutable",
        artifacts=[],
        events=[
            {
                "name": "shell.command_executed",
                "payload": {
                    "command": command,
                    "cwd": str(cwd),
                    "returncode": completed.returncode,
                },
            }
        ],
        error=None
        if ok
        else {
            "code": "nonzero_exit",
            "message": f"Command exited with code {completed.returncode}.",
        },
    )


def _resolve_cwd(raw_cwd: Any, context: PermissionContext) -> Path:
    variables = context.variables()
    workspace = Path(variables["$workspace"])
    project = Path(variables["$project"])
    raw = raw_cwd if isinstance(raw_cwd, str) and raw_cwd.strip() else "$project"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        first_part = candidate.parts[0] if candidate.parts else ""
        if first_part in variables:
            candidate = Path(variables[first_part]).joinpath(*candidate.parts[1:])
        elif first_part in {"agents", "config", "sessions", "skills"}:
            candidate = workspace / candidate
        else:
            candidate = project / candidate
    resolved = candidate.resolve()
    allowed_roots = [workspace.resolve(), project.resolve()]
    if not any(_is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError("cwd must stay inside the active project or workspace.")
    if not resolved.exists():
        raise ValueError(f"cwd does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"cwd is not a directory: {resolved}")
    return resolved


def _timeout(raw_timeout: Any) -> int:
    if raw_timeout is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a positive integer.") from exc
    if timeout < 1:
        raise ValueError("timeout_seconds must be positive.")
    return min(timeout, HARD_TIMEOUT_SECONDS)


def _sanitized_env(cwd: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in SAFE_ENV_NAMES or upper.startswith("LC_"):
            env[name] = value
        elif not any(marker in upper for marker in SECRET_ENV_MARKERS):
            if upper in {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"}:
                env[name] = value
    env["PWD"] = str(cwd)
    return env


def _truncate(value: str, max_chars: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars].rstrip() + "\n... output truncated ...", True


def _summary_command(command: str) -> str:
    compact = " ".join(command.split())
    if len(compact) <= 80:
        return compact
    return compact[:77].rstrip() + "..."


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _error(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="local_mutable",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": retryable},
    )
