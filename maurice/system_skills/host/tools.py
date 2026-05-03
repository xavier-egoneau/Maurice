"""Host system skill tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maurice.host.agents import create_agent, delete_agent, list_agents, update_agent
from maurice.host.credentials import load_workspace_credentials
from maurice.host.paths import host_config_path, kernel_config_path
from maurice.host.secret_capture import request_secret_capture
from maurice.host.service import inspect_service_status, read_service_logs
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.contracts import ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.runs import RunStore


def build_executors(ctx: Any) -> dict[str, Any]:
    return host_tool_executors(
        ctx.permission_context,
        agent_id=ctx.agent_id,
        session_id=ctx.session_id,
    )


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
        "host.agent_list": lambda arguments: agent_list(arguments, context),
        "host.agent_create": lambda arguments: agent_create(arguments, context),
        "host.agent_update": lambda arguments: agent_update(arguments, context),
        "host.agent_delete": lambda arguments: agent_delete(arguments, context),
        "host.subagent_template_list": lambda arguments: subagent_template_list(arguments, context),
        "host.subagent_template_create": lambda arguments: subagent_template_create(arguments, context),
        "host.subagent_run_create": lambda arguments: subagent_run_create(
            arguments,
            context,
            agent_id=agent_id,
        ),
        "host.telegram_bind": lambda arguments: telegram_bind(arguments, context),
        "host.request_secret": lambda arguments: request_secret(
            arguments,
            context,
            agent_id=agent_id,
            session_id=session_id,
        ),
        "maurice.system_skills.host.tools.status": lambda arguments: status(arguments, context),
        "maurice.system_skills.host.tools.logs": lambda arguments: logs(arguments, context),
        "maurice.system_skills.host.tools.credentials": lambda arguments: credentials(arguments, context),
        "maurice.system_skills.host.tools.agent_list": lambda arguments: agent_list(arguments, context),
        "maurice.system_skills.host.tools.agent_create": lambda arguments: agent_create(arguments, context),
        "maurice.system_skills.host.tools.agent_update": lambda arguments: agent_update(arguments, context),
        "maurice.system_skills.host.tools.agent_delete": lambda arguments: agent_delete(arguments, context),
        "maurice.system_skills.host.tools.subagent_template_list": lambda arguments: subagent_template_list(arguments, context),
        "maurice.system_skills.host.tools.subagent_template_create": lambda arguments: subagent_template_create(arguments, context),
        "maurice.system_skills.host.tools.subagent_run_create": lambda arguments: subagent_run_create(
            arguments,
            context,
            agent_id=agent_id,
        ),
        "maurice.system_skills.host.tools.telegram_bind": lambda arguments: telegram_bind(arguments, context),
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
    store = load_workspace_credentials(_workspace_root(context))
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


def agent_list(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    del arguments
    agents = [
        _agent_record(agent)
        for agent in list_agents(_workspace_root(context))
    ]
    return ToolResult(
        ok=True,
        summary=f"{len(agents)} durable agent(s) configured.",
        data={"agents": agents},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.agents.listed", "payload": {"count": len(agents)}}],
        error=None,
    )


def agent_create(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    agent_id = arguments.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return _error("invalid_arguments", "host.agent_create requires agent_id.")
    try:
        agent = create_agent(
            _workspace_root(context),
            agent_id=agent_id.strip(),
            permission_profile=_optional_string(arguments.get("permission_profile")),
            skills=_optional_string_list(arguments.get("skills"), "skills"),
            credentials=_optional_string_list(arguments.get("credentials"), "credentials"),
            channels=_optional_string_list(arguments.get("channels"), "channels"),
            model_chain=_optional_string_list(arguments.get("model_chain"), "model_chain"),
            make_default=bool(arguments.get("make_default", False)),
            confirmed_permission_elevation=True,
        )
    except (ValueError, PermissionError) as exc:
        return _error("agent_create_failed", str(exc))
    return ToolResult(
        ok=True,
        summary=f"Durable agent created: {agent.id}.",
        data={"agent": _agent_record(agent)},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.agent.created", "payload": {"agent_id": agent.id}}],
        error=None,
    )


def agent_update(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    agent_id = arguments.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return _error("invalid_arguments", "host.agent_update requires agent_id.")
    try:
        agent = update_agent(
            _workspace_root(context),
            agent_id=agent_id.strip(),
            permission_profile=_optional_string(arguments.get("permission_profile")),
            skills=_optional_string_list(arguments.get("skills"), "skills"),
            credentials=_optional_string_list(arguments.get("credentials"), "credentials"),
            channels=_optional_string_list(arguments.get("channels"), "channels"),
            model_chain=_optional_string_list(arguments.get("model_chain"), "model_chain"),
            make_default=_optional_bool(arguments.get("make_default")),
            confirmed_permission_elevation=True,
        )
    except (KeyError, ValueError, PermissionError) as exc:
        return _error("agent_update_failed", str(exc))
    return ToolResult(
        ok=True,
        summary=f"Durable agent updated: {agent.id}.",
        data={"agent": _agent_record(agent)},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.agent.updated", "payload": {"agent_id": agent.id}}],
        error=None,
    )


def agent_delete(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    agent_id = arguments.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return _error("invalid_arguments", "host.agent_delete requires agent_id.")
    try:
        agent = delete_agent(
            _workspace_root(context),
            agent_id=agent_id.strip(),
            confirmed=True,
        )
    except (KeyError, ValueError, PermissionError) as exc:
        return _error("agent_delete_failed", str(exc))
    return ToolResult(
        ok=True,
        summary=f"Durable agent deleted: {agent.id}.",
        data={"agent": _agent_record(agent)},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.agent.deleted", "payload": {"agent_id": agent.id}}],
        error=None,
    )


def subagent_template_list(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    del arguments
    bundle = load_workspace_config(_workspace_root(context))
    templates = [
        {
            "id": template.id,
            "description": template.description,
            "skills": list(template.skills),
            "credentials": list(template.credentials),
            "permission_profile": template.permission_profile,
            "model_chain": list(template.model_chain),
        }
        for template in sorted(bundle.kernel.subagents.templates.values(), key=lambda item: item.id)
    ]
    models = [
        {"id": profile_id, **profile.model_dump(mode="json")}
        for profile_id, profile in sorted(bundle.kernel.models.entries.items())
    ]
    return ToolResult(
        ok=True,
        summary=f"{len(templates)} subagent template(s) configured.",
        data={"templates": templates, "models": models},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.subagent_templates.listed", "payload": {"count": len(templates)}}],
        error=None,
    )


def subagent_template_create(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    template_id = arguments.get("template_id")
    if not isinstance(template_id, str) or not template_id.strip():
        return _error("invalid_arguments", "host.subagent_template_create requires template_id.")
    template_id = template_id.strip()
    description = _optional_string(arguments.get("description")) or ""
    permission_profile = _optional_string(arguments.get("permission_profile")) or "safe"
    if permission_profile not in {"safe", "limited", "power"}:
        return _error("invalid_arguments", "permission_profile must be safe, limited or power.")
    skills = _optional_string_list(arguments.get("skills"), "skills") or []
    credentials = _optional_string_list(arguments.get("credentials"), "credentials") or []
    model_chain = _optional_string_list(arguments.get("model_chain"), "model_chain") or []

    workspace = _workspace_root(context)
    bundle = load_workspace_config(workspace)
    if template_id in bundle.kernel.subagents.templates:
        return _error("template_exists", f"Subagent template already exists: {template_id}")
    missing = [profile_id for profile_id in model_chain if profile_id not in bundle.kernel.models.entries]
    if missing:
        return _error("unknown_model_profile", f"Unknown model profile(s): {', '.join(missing)}")

    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    template = {
        "id": template_id,
        "description": description,
        "skills": skills,
        "credentials": credentials,
        "permission_profile": permission_profile,
        "channels": [],
        "model_chain": model_chain,
    }
    kernel_data.setdefault("kernel", {}).setdefault("subagents", {}).setdefault("templates", {})[template_id] = template
    write_yaml_file(kernel_path, kernel_data)
    return ToolResult(
        ok=True,
        summary=f"Subagent template created: {template_id}.",
        data={"template": template},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "host.subagent_template.created", "payload": {"template_id": template_id}}],
        error=None,
    )


def subagent_run_create(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    agent_id: str | None,
) -> ToolResult:
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        return _error("invalid_arguments", "host.subagent_run_create requires task.")
    template_id = arguments.get("template_id")
    if not isinstance(template_id, str) or not template_id.strip():
        return _error("invalid_arguments", "host.subagent_run_create requires template_id.")
    workspace = _workspace_root(context)
    bundle = load_workspace_config(workspace)
    parent_id = _optional_string(arguments.get("parent_agent_id")) or agent_id
    if not parent_id or parent_id not in bundle.agents.agents:
        return _error("unknown_agent", "Unknown parent agent.")
    parent = bundle.agents.agents[parent_id]
    try:
        template = bundle.kernel.subagents.templates[template_id.strip()]
    except KeyError:
        return _error("unknown_template", f"Unknown subagent template: {template_id}")
    missing = [profile_id for profile_id in template.model_chain if profile_id not in bundle.kernel.models.entries]
    if missing:
        return _error("unknown_model_profile", f"Unknown model profile(s): {', '.join(missing)}")

    write_paths = _optional_string_list(arguments.get("write_paths"), "write_paths") or []
    permission_classes = _optional_string_list(arguments.get("permission_classes"), "permission_classes") or []
    constraints = _optional_string_list(arguments.get("constraints"), "constraints") or []
    plan = _optional_string_list(arguments.get("plan"), "plan") or []
    event_stream = Path(parent.event_stream) if parent.event_stream else workspace / "agents" / parent.id / "events.jsonl"
    store = RunStore(
        workspace / "agents" / parent.id / "runs.json",
        workspace_root=workspace,
        event_store=EventStore(event_stream),
    )
    profile = {
        "id": template.id,
        "template": True,
        "description": template.description,
        "skills": list(template.skills),
        "credentials": list(template.credentials),
        "permission_profile": template.permission_profile,
        "channels": list(template.channels),
        "model_chain": list(template.model_chain),
    }
    run = store.create(
        parent_agent_id=parent.id,
        task=task.strip(),
        base_agent=template.id,
        base_agent_profile=profile,
        context_summary=_optional_string(arguments.get("context_summary")) or "",
        relevant_files=[],
        constraints=constraints,
        plan=plan,
        write_scope={"paths": write_paths},
        permission_scope={"classes": permission_classes},
        dependency_policy={"requires_parent_approval": True},
        output_contract={"requires_self_check": bool(arguments.get("requires_self_check", False))},
    )
    return ToolResult(
        ok=True,
        summary=f"Subagent run created: {run.id}.",
        data={"run": run.model_dump(mode="json"), "template": profile},
        trust="local_mutable",
        artifacts=[{"type": "path", "path": str(workspace / "runs" / run.id / "mission.json")}],
        events=[
            {
                "name": "host.subagent_run.created",
                "payload": {"run_id": run.id, "template_id": template.id, "parent_agent_id": parent.id},
            }
        ],
        error=None,
    )


def telegram_bind(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    agent_id = arguments.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return _error("invalid_arguments", "host.telegram_bind requires agent_id.")
    agent_id = agent_id.strip()
    agents = {agent.id: agent for agent in list_agents(_workspace_root(context))}
    if agent_id not in agents:
        return _error("unknown_agent", f"Unknown agent: {agent_id}")

    credential = _optional_string(arguments.get("credential")) or "telegram_bot"
    allowed_users = _optional_int_list(arguments.get("allowed_users"), "allowed_users") or []
    allowed_chats = _optional_int_list(arguments.get("allowed_chats"), "allowed_chats") or []
    workspace = _workspace_root(context)
    host_path = host_config_path(workspace)
    host_data = read_yaml_file(host_path)
    host = host_data.setdefault("host", {})
    channels = host.setdefault("channels", {})
    previous = channels.get("telegram") if isinstance(channels.get("telegram"), dict) else {}
    previous_agent = previous.get("agent") if isinstance(previous, dict) else None
    if not allowed_users and isinstance(previous, dict):
        allowed_users = _optional_int_list(previous.get("allowed_users"), "allowed_users") or []
    if not allowed_chats and isinstance(previous, dict):
        allowed_chats = _optional_int_list(previous.get("allowed_chats"), "allowed_chats") or []

    channels["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": agent_id,
        "credential": credential,
        "allowed_users": allowed_users,
        "allowed_chats": allowed_chats,
        "status": "configured_pending_adapter",
    }
    write_yaml_file(host_path, host_data)

    for agent in agents.values():
        if agent.id == agent_id and "telegram" not in agent.channels:
            update_agent(workspace, agent_id=agent.id, channels=[*agent.channels, "telegram"])
        elif agent.id != agent_id and "telegram" in agent.channels:
            update_agent(
                workspace,
                agent_id=agent.id,
                channels=[channel for channel in agent.channels if channel != "telegram"],
            )

    return ToolResult(
        ok=True,
        summary=f"Telegram is now connected to agent: {agent_id}.",
        data={
            "telegram": channels["telegram"],
            "previous_agent": previous_agent,
            "current_agent": agent_id,
            "credential_configured": credential in load_workspace_credentials(workspace).credentials,
        },
        trust="local_mutable",
        artifacts=[],
        events=[
            {
                "name": "host.telegram.bound",
                "payload": {"agent_id": agent_id, "previous_agent": previous_agent},
            }
        ],
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


def _agent_record(agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "default": agent.default,
        "workspace": agent.workspace,
        "skills": list(agent.skills),
        "credentials": list(agent.credentials),
        "permission_profile": agent.permission_profile,
        "status": agent.status,
        "channels": list(agent.channels),
        "model_chain": list(agent.model_chain),
        "event_stream": agent.event_stream,
    }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected a string value.")
    return value.strip() or None


def _optional_string_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


def _optional_dict(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean value.")
    return value


def _optional_int_list(value: Any, name: str) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ValueError(f"{name} must be a list of integers.")
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
