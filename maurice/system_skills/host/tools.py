"""Host system skill tools."""

from __future__ import annotations

from typing import Any

from maurice.host.credentials import load_credentials
from maurice.host.secret_capture import request_secret_capture
from maurice.host.service import inspect_service_status, read_service_logs
from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext


def host_tool_executors(
    context: PermissionContext,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    return {
        "host.status": lambda arguments: status(arguments, context),
        "host.logs": lambda arguments: logs(arguments, context),
        "host.credentials": lambda arguments: credentials(arguments, context),
        "host.request_secret": lambda arguments: request_secret(
            arguments,
            context,
            agent_id=agent_id,
            session_id=session_id,
        ),
        "maurice.system_skills.host.tools.status": lambda arguments: status(arguments, context),
        "maurice.system_skills.host.tools.logs": lambda arguments: logs(arguments, context),
        "maurice.system_skills.host.tools.credentials": lambda arguments: credentials(arguments, context),
        "maurice.system_skills.host.tools.request_secret": lambda arguments: request_secret(
            arguments,
            context,
            agent_id=agent_id,
            session_id=session_id,
        ),
    }


def status(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    del arguments
    report = inspect_service_status(_workspace_root(context))
    return ToolResult(
        ok=report.ok,
        summary="Host service status is ok." if report.ok else "Host service status has errors.",
        data=report.model_dump(mode="json"),
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.status.inspected", "payload": {"ok": report.ok}}],
        error=None if report.ok else {"code": "service_status_error", "message": "One or more host checks failed."},
    )


def logs(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    agent_id = arguments.get("agent")
    if agent_id is not None and not isinstance(agent_id, str):
        return _error("invalid_arguments", "host.logs agent must be a string.")
    try:
        limit = _positive_int(arguments.get("limit"), default=20, name="limit")
        events = read_service_logs(_workspace_root(context), agent_id=agent_id, limit=limit)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))

    serialized = [event.model_dump(mode="json") for event in events]
    return ToolResult(
        ok=True,
        summary=f"Read {len(serialized)} host log event(s).",
        data={"events": serialized},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.logs.read", "payload": {"count": len(serialized), "agent": agent_id}}],
        error=None,
    )


def credentials(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    del arguments
    store = load_credentials(_workspace_root(context) / "credentials.yaml")
    records = [
        {
            "name": name,
            "type": record.type,
            "provider": getattr(record, "provider", None),
            "base_url": record.base_url,
            "configured": bool(record.value or record.base_url),
        }
        for name, record in sorted(store.credentials.items())
    ]
    return ToolResult(
        ok=True,
        summary=f"{len(records)} credential profile(s) configured. Values are hidden.",
        data={"credentials": records},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.credentials.listed", "payload": {"count": len(records)}}],
        error=None,
    )


def request_secret(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    agent_id: str | None,
    session_id: str | None,
) -> ToolResult:
    if not agent_id or not session_id:
        return _error("missing_context", "Secret capture requires an active agent session.")
    credential = arguments.get("credential")
    if not isinstance(credential, str) or not credential.strip():
        return _error("invalid_arguments", "host.request_secret requires a credential name.")
    provider = arguments.get("provider") or "telegram_bot"
    if not isinstance(provider, str) or not provider.strip():
        return _error("invalid_arguments", "host.request_secret provider must be a string.")
    secret_type = arguments.get("type") or "token"
    if secret_type not in {"api_key", "token", "url", "password", "opaque"}:
        return _error("invalid_arguments", "host.request_secret type is invalid.")
    prompt = arguments.get("prompt") or ""
    if prompt and not isinstance(prompt, str):
        return _error("invalid_arguments", "host.request_secret prompt must be a string.")

    request = request_secret_capture(
        _workspace_root(context),
        agent_id=agent_id,
        session_id=session_id,
        credential=credential.strip(),
        provider=provider.strip(),
        secret_type=secret_type,
        prompt=prompt.strip(),
    )
    return ToolResult(
        ok=True,
        summary=(
            "Secret capture armed. Ask the user to send the secret in their next message; "
            "the host will store it and will not forward that message to the model."
        ),
        data={
            "credential": request.credential,
            "provider": request.provider,
            "session_id": request.session_id,
            "next_message_is_secret": True,
        },
        trust="local_mutable",
        artifacts=[],
        events=[
            {
                "name": "host.secret_capture.requested",
                "payload": {"credential": request.credential, "provider": request.provider},
            }
        ],
        error=None,
    )


def _workspace_root(context: PermissionContext):
    from pathlib import Path

    return Path(context.workspace_root)


def _positive_int(value: Any, *, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1.")
    return value


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data={},
        trust="local_mutable",
        artifacts=[],
        events=[],
        error={"code": code, "message": message},
    )
