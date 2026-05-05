"""Auto-split from cli.py."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import re
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlparse
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agent_wizard import (
    clear_agent_creation_wizard,
    handle_agent_creation_wizard,
    _available_skills,
)
from maurice.host.agents import archive_agent, create_agent, delete_agent, disable_agent, list_agents, update_agent
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME, ChatGPTAuthFlow, clear_chatgpt_auth,
    get_valid_chatgpt_access_token, load_chatgpt_auth, save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.command_registry import CommandRegistry, default_command_registry
from maurice.host.client import MauriceClient
from maurice.host.context import LocalConfig, MauriceContext, build_command_callbacks, resolve_global_context
from maurice.host.context_meter import context_summary
from maurice.host.credentials import (
    CredentialRecord, CredentialsStore, credentials_path,
    ensure_workspace_credentials_migrated, load_workspace_credentials, write_workspace_credentials,
)
from maurice.host.dashboard import build_dashboard_snapshot
from maurice.host.delivery import (
    _schedule_reminder_callback, _deliver_reminder_result, _build_daily_digest,
    _deliver_daily_digest, _emit_daily_event, _cancel_job_callback,
    _latest_dream_report, _human_datetime,
)
from maurice.host.autonomy_progress import ProgressCallback, SessionProgressStore
from maurice.host.gateway import GatewayHttpServer, MessageRouter
from maurice.host.git_status import git_changes, git_diff
from maurice.host.migration import inspect_jarvis_workspace, migrate_jarvis_workspace
from maurice.host.model_catalog import chatgpt_model_choices, format_bytes, ollama_model_choices
from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.output import (
    _yes_no, _status_marker, _short, _ansi_padding, _compact_text,
    _supports_color, _color, _print_title, _print_dim,
)
from maurice.host.paths import (
    agents_config_path, ensure_workspace_config_migrated, host_config_path,
    kernel_config_path, maurice_home, workspace_skills_config_path,
)
from maurice.host.project import config_path
from maurice.host.project_registry import record_seen_project
from maurice.host.runtime import (
    run_one_turn, _resolve_agent, _agent_system_prompt, _active_dev_project_path,
    _provider_for_config, _effective_model_config, _model_credential,
    _effective_model_label, _default_agent,
)
from maurice.host.secret_capture import capture_pending_secret, clear_secret_capture
from maurice.host.session_routing import canonical_session_id, is_canonical_session
from maurice.host.self_update import (
    apply_runtime_proposal, list_runtime_proposals, run_proposal_tests, validate_runtime_proposal,
)
from maurice.host.service import check_install, inspect_service_status, read_service_logs
from maurice.host.telegram import (
    _credential_value, _telegram_channel_configured, _telegram_channel_configs,
    _telegram_channel_for_agent, _telegram_offset_path, _validate_telegram_first_message,
    make_telegram_progress_callback,
    _telegram_get_updates, _telegram_bot_username, _telegram_send_message,
    _telegram_send_chat_action, _telegram_api_json, _telegram_update_to_inbound,
    _telegram_set_my_commands,
    _int_list, _read_int_file, _write_int_file, _redact_secret,
    _telegram_sender_ids, _telegram_start_chat_action,
    _telegram_allowed_chats_with_private_users,
)
from maurice.host.workspace import ensure_workspace_content_migrated, initialize_workspace
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import (
    ConfigBundle,
    GatewayRateLimitConfig,
    load_workspace_config,
    model_profile_id,
    model_profile_payload,
    read_yaml_file,
    write_yaml_file,
)
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider, ChatGPTCodexProvider, MockProvider,
    OllamaCompatibleProvider, OpenAICompatibleProvider, UnsupportedProvider,
)
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.contracts import ToolError, ToolResult
from maurice.kernel.skill_setup import apply_skill_setup_updates, skill_setup_status
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.host.agent_runtime import global_registry as _agent_runtime_registry
from maurice.kernel.tool_labels import tool_action_label
from maurice.system_skills.reminders.tools import fire_reminder


MAX_WEB_UPLOAD_BYTES = 10_000_000
MAX_WEB_UPLOADS = 6
IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

def _gateway_local_message(
    workspace_root: Path,
    *,
    message: str,
    peer_id: str,
    agent_id: str | None,
    session_id: str | None,
) -> None:
    router, agent, _bundle = _gateway_router_for(workspace_root, agent_id)
    result = router.handle(
        {
            "channel": "local",
            "peer_id": peer_id,
            "text": message,
            "agent_id": agent.id,
            "session_id": session_id,
        }
    )
    print(result.outbound.text)



def _gateway_serve(
    workspace_root: Path,
    *,
    agent_id: str | None,
    host: str | None,
    port: int | None,
) -> None:
    server = _build_gateway_http_server(workspace_root, agent_id=agent_id, host=host, port=port)
    bound_host, bound_port = server.address
    print(f"Maurice gateway listening on http://{bound_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Maurice gateway stopped")
    finally:
        server.shutdown()



def _gateway_serve_until_stopped(
    workspace_root: Path,
    agent_id: str | None,
    poll_seconds: float,
    stop_event: threading.Event,
    *,
    telegram_pollers: "_WebTelegramPollers | None" = None,
) -> None:
    server = _build_gateway_http_server(
        workspace_root,
        agent_id=agent_id,
        host=None,
        port=None,
        telegram_pollers=telegram_pollers,
    )
    server.server.timeout = max(poll_seconds, 0.1)
    bound_host, bound_port = server.address
    print(f"Maurice gateway listening on http://{bound_host}:{bound_port}")
    try:
        while not stop_event.is_set():
            server.server.handle_request()
    finally:
        server.server.server_close()
        print("Maurice gateway stopped")



def _build_gateway_http_server(
    workspace_root: Path,
    *,
    agent_id: str | None,
    host: str | None,
    port: int | None,
    active_project: Path | None = None,
    telegram_pollers: "_WebTelegramPollers | None" = None,
) -> GatewayHttpServer:
    router, agent, bundle = _gateway_router_for(
        workspace_root,
        agent_id,
        active_project=active_project,
    )
    channels = ChannelAdapterRegistry.from_config(
        bundle.host.channels,
        default_agent_id=agent.id,
    )
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else Path(bundle.host.workspace_root) / "agents" / agent.id / "events.jsonl"
    )
    workspace = Path(bundle.host.workspace_root)
    if active_project is not None:
        record_seen_project(agent.workspace, active_project)
    work_root = active_project.expanduser().resolve() if active_project is not None else _gateway_git_root(workspace, agent.id)
    return GatewayHttpServer(
        host=host or bundle.host.gateway.host,
        port=bundle.host.gateway.port if port is None else port,
        router=router,
        channels=channels,
        event_store=EventStore(event_stream),
        web_agents=lambda: _gateway_web_agents(workspace),
        web_session_list=lambda agent_id: _gateway_web_session_list(
            workspace,
            agent_id,
        ),
        web_session_history=lambda agent_id, session_id: _gateway_web_session_history(
            workspace,
            agent_id,
            session_id,
        ),
        web_session_reset=lambda agent_id, session_id: _gateway_web_session_reset(
            workspace,
            agent_id,
            session_id,
        ),
        web_git_status=lambda: git_changes(work_root),
        web_git_diff=lambda path: git_diff(work_root, path),
        web_model_status=lambda agent_id: _gateway_model_status(workspace, agent_id),
        web_model_update=lambda agent_id, model: _gateway_model_update(workspace, agent_id, model),
        web_agent_config=lambda agent_id: _gateway_agent_config_status(workspace, agent_id),
        web_agent_update=lambda agent_id, payload: _gateway_agent_config_update_and_sync_telegram(
            workspace,
            agent_id,
            payload,
            telegram_pollers=telegram_pollers,
        ),
        web_agent_switching=bundle.host.development.web_agent_switching,
        web_uploads=lambda agent_id, session_id, attachments: _store_web_uploads(
            _gateway_prov_root(workspace, agent_id) / "uploads",
            session_id=session_id,
            attachments=attachments,
        ),
        web_attachment=_read_web_attachment,
        web_turn_cancel=lambda agent_id, session_id: _cancel_turn_for_context(
            resolve_global_context(
                workspace,
                agent=_resolve_agent(bundle, agent_id),
                bundle=bundle,
                active_project=active_project,
            ),
            agent_id,
            session_id,
        ),
        web_secret_capture=lambda agent_id, session_id, _peer_id, text: _capture_web_secret(
            workspace,
            event_store=EventStore(event_stream),
            agent_id=agent_id,
            session_id=session_id,
            text=text,
        ),
    )


def _build_gateway_http_server_for_context(
    ctx: MauriceContext,
    *,
    agent_id: str | None,
    host: str,
    port: int,
    web_token: str | None = None,
    telegram_pollers: "_WebTelegramPollers | None" = None,
) -> GatewayHttpServer:
    if ctx.scope == "global":
        return _build_gateway_http_server(
            ctx.context_root,
            agent_id=agent_id,
            host=host,
            port=port,
            active_project=ctx.active_project_root,
            telegram_pollers=telegram_pollers,
        )
    router = _gateway_router_for_local_context(ctx)
    return GatewayHttpServer(
        host=host,
        port=port,
        router=router,
        event_store=EventStore(ctx.events_path),
        web_agents=lambda: _gateway_web_agents_for_context(ctx),
        web_session_list=lambda agent_id: _gateway_web_session_list_for_context(
            ctx,
            agent_id,
        ),
        web_session_history=lambda agent_id, session_id: _gateway_web_session_history_for_context(
            ctx,
            agent_id,
            session_id,
        ),
        web_session_reset=lambda agent_id, session_id: _gateway_web_session_reset_for_context(
            ctx,
            agent_id,
            session_id,
        ),
        web_git_status=lambda: git_changes(ctx.content_root),
        web_git_diff=lambda path: git_diff(ctx.content_root, path),
        web_model_status=lambda agent_id: _local_model_status(ctx, agent_id),
        web_model_update=lambda agent_id, model: _local_model_update(ctx, agent_id, model),
        web_agent_config=lambda agent_id: _local_agent_config_status(ctx, agent_id),
        web_agent_update=lambda agent_id, payload: _local_agent_config_update(ctx, agent_id, payload),
        web_agent_switching=False,
        web_uploads=lambda _agent_id, session_id, attachments: _store_web_uploads(
            ctx.state_root / "prov" / "uploads",
            session_id=session_id,
            attachments=attachments,
        ),
        web_attachment=_read_web_attachment,
        web_turn_cancel=lambda agent_id, session_id: _cancel_turn_for_context(
            ctx,
            agent_id,
            session_id,
        ),
        web_secret_capture=lambda agent_id, session_id, _peer_id, text: _capture_web_secret(
            ctx.context_root,
            event_store=EventStore(ctx.events_path),
            agent_id=agent_id,
            session_id=session_id,
            text=text,
        ),
        web_token=web_token,
    )



def _gateway_telegram_poll(
    workspace_root: Path,
    *,
    agent_id: str | None,
    once: bool,
    poll_seconds: float,
) -> None:
    bundle = load_workspace_config(workspace_root)
    selected = _telegram_channel_for_agent(bundle, agent_id)
    if selected is None:
        raise SystemExit("Telegram channel is not configured. Run onboarding first.")
    channel_name, telegram = selected
    if not telegram.get("enabled", True):
        raise SystemExit("Telegram channel is disabled.")
    credential_name = str(telegram.get("credential") or "telegram_bot")
    workspace = Path(bundle.host.workspace_root)
    token = _credential_value(workspace, credential_name)
    if not token:
        raise SystemExit("Telegram bot token is missing. Run onboarding first.")
    target_agent = agent_id or str(telegram.get("agent") or "main")
    router, agent, bundle = _gateway_router_for(workspace, target_agent)
    event_store = EventStore(Path(agent.event_stream))
    offset_path = _telegram_offset_path(Path(bundle.host.workspace_root), agent.id, channel_name)
    allowed_users = _int_list(telegram.get("allowed_users"))
    allowed_chats = _int_list(telegram.get("allowed_chats"))
    _sync_telegram_commands(token, router)
    print(f"Maurice Telegram polling for @{_telegram_bot_username(token) or 'bot'} -> agent {agent.id}")
    try:
        while True:
            handled = _telegram_poll_once(
                token=token,
                router=router,
                event_store=event_store,
                workspace=workspace,
                offset_path=offset_path,
                agent_id=agent.id,
                allowed_users=allowed_users,
                allowed_chats=allowed_chats,
                timeout_seconds=10 if not once else 0,
            )
            if once:
                print(f"Telegram poll complete: {handled} message(s) routed.")
                return
            time.sleep(max(poll_seconds, 0.1))
    except KeyboardInterrupt:
        print("Maurice Telegram polling stopped")


def _capture_web_secret(
    workspace: Path,
    *,
    event_store: EventStore,
    agent_id: str,
    session_id: str,
    text: str,
) -> str | None:
    captured = capture_pending_secret(
        workspace,
        agent_id=agent_id,
        session_id=session_id,
        value=text,
    )
    if captured is None:
        return None
    event_store.emit(
        name="host.secret_capture.completed",
        kind="audit",
        origin="host.channel.web",
        agent_id=agent_id,
        session_id=session_id,
        payload={"credential": captured.credential, "provider": captured.provider},
    )
    return f"Secret enregistre sous `{captured.credential}`. Tu peux continuer."



def _gateway_web_agents(workspace: Path) -> list[dict[str, Any]]:
    bundle = load_workspace_config(workspace)
    agents = []
    for agent in sorted(bundle.agents.agents.values(), key=lambda item: item.id):
        if agent.status != "active":
            continue
        model = _effective_model_config(bundle, agent)
        agents.append(
            {
                "id": agent.id,
                "provider": model.get("provider") or "",
                "model": model.get("name") or "",
                "default": agent.default,
            }
        )
    return agents


def _gateway_web_agents_for_context(ctx: MauriceContext) -> list[dict[str, Any]]:
    if ctx.scope == "global":
        return _gateway_web_agents(ctx.context_root)
    provider = ctx.config.provider if isinstance(ctx.config, LocalConfig) else {}
    return [
        {
            "id": "main",
            "provider": str(provider.get("type") or "mock"),
            "model": str(provider.get("model") or "mock"),
            "default": True,
            "scope": "local",
        }
    ]



def _gateway_web_session_history(workspace: Path, agent_id: str, session_id: str) -> list[dict[str, Any]]:
    return _session_history(SessionStore(workspace / "sessions"), agent_id, session_id)


def _gateway_web_session_list(workspace: Path, agent_id: str) -> list[dict[str, Any]]:
    return _session_list(SessionStore(workspace / "sessions"), agent_id)


def _gateway_web_session_history_for_context(
    ctx: MauriceContext,
    agent_id: str,
    session_id: str,
) -> list[dict[str, Any]]:
    return _session_history(SessionStore(ctx.sessions_path), agent_id, session_id)


def _gateway_web_session_list_for_context(ctx: MauriceContext, agent_id: str) -> list[dict[str, Any]]:
    return _session_list(SessionStore(ctx.sessions_path), agent_id)


def _session_list(store: SessionStore, agent_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
    rows = []
    for session in store.list(agent_id):
        if not is_canonical_session(session.id, agent_id):
            continue
        messages = _session_history(store, agent_id, session.id)
        visible = [m for m in messages if m.get("role") in {"user", "assistant"}]
        channel = session.id.split(":", 1)[0] if ":" in session.id else "session"
        first_user = next((m for m in visible if m.get("role") == "user" and m.get("content")), None)
        last_visible = next((m for m in reversed(visible) if m.get("content")), None)
        title = _session_title(session.id, first_user, last_visible)
        last_message = str((last_visible or {}).get("content") or "").strip()
        rows.append(
            {
                "id": session.id,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(visible),
                "title": title,
                "last_message": _compact_text(last_message, 140),
                "channel": channel,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _session_title(session_id: str, first_user: dict[str, Any] | None, last_visible: dict[str, Any] | None) -> str:
    for message in (first_user, last_visible):
        content = str((message or {}).get("content") or "").strip()
        if content:
            first_line = content.splitlines()[0].strip()
            if first_line:
                return _compact_text(first_line, 64)
    return session_id


def _session_history(store: SessionStore, agent_id: str, session_id: str) -> list[dict[str, Any]]:
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        return []
    tool_activity = _session_tool_activity(session.messages)
    # ui_messages is append-only and preserves pre-compaction history.
    # session.messages holds current (possibly compacted) content.
    # Merge: ui_messages has old messages; append anything from session.messages
    # not already there (messages added after the last compaction).
    if session.ui_messages:
        ui_ts = {m.created_at for m in session.ui_messages}
        extra = [m for m in session.messages if m.created_at not in ui_ts]
        source = session.ui_messages + extra
    else:
        source = session.messages
    messages = []
    for message in source:
        if (
            message.metadata.get("internal") is True
            or message.metadata.get("autonomy_internal") is True
            or _looks_like_internal_gateway_message(message.content)
        ):
            continue
        # compaction_notice: show as a system notice, not filtered out
        if message.metadata.get("compaction_notice"):
            notice = {
                "role": "system",
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "metadata": message.metadata,
            }
            if messages and (messages[-1].get("metadata") or {}).get("compaction_notice"):
                messages[-1] = notice
            else:
                messages.append(notice)
            continue
        if message.role not in {"user", "assistant"}:
            continue
        messages.append(
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "metadata": message.metadata,
            }
        )
        if message.role == "assistant" and message.correlation_id in tool_activity:
            messages[-1]["tools"] = tool_activity[message.correlation_id]
    return messages


def _session_tool_activity(session_messages: list[Any]) -> dict[str, list[dict[str, Any]]]:
    activity_by_correlation: dict[str, list[dict[str, Any]]] = {}
    activity_by_call: dict[str, dict[str, Any]] = {}
    for message in session_messages:
        if message.role not in {"tool_call", "tool"}:
            continue
        correlation_id = message.correlation_id
        if not correlation_id:
            continue
        metadata = message.metadata or {}
        tool_call_id = str(metadata.get("tool_call_id") or "")
        tool_name = str(metadata.get("tool_name") or "")
        if message.role == "tool_call":
            arguments = metadata.get("tool_arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            entry = {
                "id": tool_call_id,
                "name": tool_name,
                "label": tool_action_label(tool_name, arguments),
                "status": "running",
                "ok": None,
                "summary": "",
                "created_at": message.created_at.isoformat(),
            }
            activity_by_correlation.setdefault(correlation_id, []).append(entry)
            if tool_call_id:
                activity_by_call[tool_call_id] = entry
            continue
        entry = activity_by_call.get(tool_call_id)
        if entry is None:
            entry = {
                "id": tool_call_id,
                "name": tool_name,
                "label": tool_action_label(tool_name),
                "created_at": message.created_at.isoformat(),
            }
            activity_by_correlation.setdefault(correlation_id, []).append(entry)
            if tool_call_id:
                activity_by_call[tool_call_id] = entry
        ok = metadata.get("ok")
        entry["ok"] = ok
        entry["status"] = "ok" if ok is True else "failed" if ok is False else "done"
        entry["summary"] = message.content.splitlines()[0][:100] if message.content else ""
        entry["completed_at"] = message.created_at.isoformat()
        artifacts = metadata.get("artifacts")
        if isinstance(artifacts, list) and artifacts:
            entry["artifacts"] = artifacts
            diff = _tool_diff_from_artifacts(artifacts)
            if diff:
                entry["diff"] = diff
    return activity_by_correlation


def _tool_diff_from_artifacts(artifacts: list[Any]) -> dict[str, Any] | None:
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("type") != "diff":
            continue
        data = artifact.get("data")
        if not isinstance(data, dict):
            data = {}
        diff = data.get("diff")
        if not isinstance(diff, str) or not diff:
            continue
        return {
            "path": str(data.get("path") or artifact.get("path") or ""),
            "diff": diff,
            "truncated": bool(data.get("truncated")),
            "insertions": int(data.get("insertions") or 0),
            "deletions": int(data.get("deletions") or 0),
        }
    return None



def _looks_like_internal_gateway_message(content: str) -> bool:
    normalized = str(content).lstrip()
    return normalized.startswith("Commande interne `/dev`") or normalized.startswith("Continue le mode dev")



def _gateway_web_session_reset(workspace: Path, agent_id: str, session_id: str) -> bool:
    return _reset_session_store(SessionStore(workspace / "sessions"), agent_id, session_id)


def _gateway_web_session_reset_for_context(ctx: MauriceContext, agent_id: str, session_id: str) -> bool:
    return _reset_session_store(SessionStore(ctx.sessions_path), agent_id, session_id)


def _reset_session_store(store: SessionStore, agent_id: str, session_id: str) -> bool:
    try:
        store.reset(agent_id, session_id)
        return True
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)
        return False



def _telegram_poll_until_stopped(
    workspace_root: Path,
    agent_id: str | None,
    poll_seconds: float,
    stop_event: threading.Event,
    *,
    channel_name: str = "telegram",
) -> None:
    bundle = load_workspace_config(workspace_root)
    telegram = bundle.host.channels.get(channel_name)
    if not isinstance(telegram, dict) or not telegram.get("enabled", True):
        print(f"Telegram channel {channel_name} is not configured.")
        return
    credential_name = str(telegram.get("credential") or "telegram_bot")
    workspace = Path(bundle.host.workspace_root)
    token = _credential_value(workspace, credential_name)
    if not token:
        print("Telegram bot token is missing.")
        return
    target_agent = agent_id or str(telegram.get("agent") or "main")
    router, agent, bundle = _gateway_router_for(workspace, target_agent)
    event_store = EventStore(Path(agent.event_stream))
    offset_path = _telegram_offset_path(Path(bundle.host.workspace_root), agent.id, channel_name)
    allowed_users = _int_list(telegram.get("allowed_users"))
    allowed_chats = _int_list(telegram.get("allowed_chats"))
    _sync_telegram_commands(token, router)
    print(f"Telegram polling started for @{_telegram_bot_username(token) or 'bot'} -> agent {agent.id}")
    while not stop_event.is_set():
        try:
            _telegram_poll_once(
                token=token,
                router=router,
                event_store=event_store,
                workspace=workspace,
                offset_path=offset_path,
                agent_id=agent.id,
                allowed_users=allowed_users,
                allowed_chats=allowed_chats,
                timeout_seconds=0,
            )
        except Exception as exc:
            event_store.emit(
                name="channel.poll.failed",
                kind="progress",
                origin="host.channel.telegram",
                agent_id=agent.id,
                session_id="telegram",
                payload={"error": _redact_secret(str(exc), token)},
            )
            print(f"Telegram polling error: {_redact_secret(str(exc), token)}")
        stop_event.wait(poll_seconds)
    print("Telegram polling stopped")


def _sync_telegram_commands(token: str, router: MessageRouter) -> None:
    try:
        _telegram_set_my_commands(
            token,
            router.command_registry.telegram_bot_commands(
                scope="global",
                agent_id=router.default_agent_id,
                has_active_project=False,
            ),
        )
    except Exception as exc:
        print(f"Telegram command menu sync failed: {_redact_secret(str(exc), token)}")


class _WebTelegramPollers:
    def __init__(
        self,
        workspace: Path,
        *,
        agent_id: str | None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.agent_id = agent_id
        self.poll_seconds = max(poll_seconds, 0.1)
        self._lock = threading.Lock()
        self._workers: dict[str, tuple[tuple[Any, ...], threading.Event, threading.Thread]] = {}

    def sync(self) -> None:
        bundle = load_workspace_config(self.workspace)
        wanted: dict[str, tuple[Any, ...]] = {}
        for channel_name, config in _telegram_channel_configs(bundle):
            if config.get("enabled", True) is False:
                continue
            channel_agent = str(config.get("agent") or "main")
            if self.agent_id and channel_agent != self.agent_id:
                continue
            signature = (
                channel_agent,
                str(config.get("credential") or "telegram_bot"),
                tuple(_int_list(config.get("allowed_users"))),
                tuple(_int_list(config.get("allowed_chats"))),
            )
            wanted[channel_name] = signature

        with self._lock:
            for channel_name, (signature, stop_event, thread) in list(self._workers.items()):
                if wanted.get(channel_name) == signature and thread.is_alive():
                    continue
                stop_event.set()
                del self._workers[channel_name]

            for channel_name, signature in wanted.items():
                if channel_name in self._workers:
                    continue
                stop_event = threading.Event()
                thread = threading.Thread(
                    target=_telegram_poll_until_stopped,
                    args=(self.workspace, self.agent_id, self.poll_seconds, stop_event),
                    kwargs={"channel_name": channel_name},
                    daemon=True,
                )
                self._workers[channel_name] = (signature, stop_event, thread)
                thread.start()

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for _signature, stop_event, _thread in workers:
            stop_event.set()
        for _signature, _stop_event, thread in workers:
            thread.join(timeout=5)



def _telegram_poll_once(
    *,
    token: str,
    router: MessageRouter,
    event_store: EventStore,
    workspace: Path,
    offset_path: Path,
    agent_id: str,
    allowed_users: list[int],
    allowed_chats: list[int],
    timeout_seconds: int,
) -> int:
    offset = _read_int_file(offset_path)
    updates = _telegram_get_updates(token, offset=offset, timeout_seconds=timeout_seconds)
    max_update_id = offset - 1
    handled = 0
    for update in updates:
        update_id = int(update.get("update_id") or 0)
        max_update_id = max(max_update_id, update_id)
        inbound = _telegram_update_to_inbound(
            update,
            agent_id=agent_id,
            allowed_users=allowed_users,
            allowed_chats=allowed_chats,
        )
        if inbound is None:
            continue
        chat_id = int(inbound["metadata"]["chat_id"])
        session_id = canonical_session_id(agent_id)
        inbound["session_id"] = session_id
        _remember_telegram_session_target(workspace, agent_id, session_id, inbound)
        captured = capture_pending_secret(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            value=str(inbound["text"]),
        )
        if captured is None:
            legacy_session_id = f"{inbound['channel']}:{inbound['peer_id']}"
            if legacy_session_id != session_id:
                captured = capture_pending_secret(
                    workspace,
                    agent_id=agent_id,
                    session_id=legacy_session_id,
                    value=str(inbound["text"]),
                )
        if captured is not None:
            _telegram_send_message(
                token,
                chat_id,
                f"Secret enregistre sous `{captured.credential}`. Tu peux continuer.",
            )
            event_store.emit(
                name="host.secret_capture.completed",
                kind="audit",
                origin="host.channel.telegram",
                agent_id=agent_id,
                session_id=session_id,
                payload={"credential": captured.credential, "provider": captured.provider},
            )
            handled += 1
            continue
        stop_typing = _telegram_start_chat_action(token, chat_id)
        try:
            result = router.handle(inbound)
        finally:
            stop_typing()
        _telegram_send_message(token, chat_id, _telegram_response_text(result.outbound.text, result.outbound.metadata))
        event_store.emit(
            name="channel.delivery.succeeded",
            kind="progress",
            origin="host.channel.telegram",
            agent_id=result.outbound.agent_id,
            session_id=result.outbound.session_id,
            correlation_id=result.outbound.correlation_id,
            payload={"channel": "telegram", "peer_id": result.inbound.peer_id, "chat_id": chat_id},
        )
        handled += 1
    if max_update_id >= offset:
        _write_int_file(offset_path, max_update_id + 1)
    return handled


def _remember_telegram_session_target(
    workspace: Path,
    agent_id: str,
    session_id: str,
    inbound: dict[str, Any],
) -> None:
    metadata = inbound.get("metadata") if isinstance(inbound.get("metadata"), dict) else {}
    chat_id = metadata.get("chat_id")
    if not isinstance(chat_id, int):
        return
    store = SessionStore(workspace / "sessions")
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        session = store.create(agent_id, session_id=session_id)
    session.metadata["telegram"] = {
        "chat_id": chat_id,
        "peer_id": str(inbound.get("peer_id") or ""),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    store.save(session)


def _telegram_response_text(text: str, metadata: dict[str, Any]) -> str:
    summary = context_summary(metadata.get("context") if isinstance(metadata, dict) else None)
    if not summary:
        return text
    return f"{text}\n\n{summary}"


def _gateway_prov_root(workspace: Path, agent_id: str) -> Path:
    try:
        agent = load_workspace_config(workspace).agents.agents[agent_id]
        return Path(agent.workspace) / ".maurice" / "prov"
    except KeyError:
        return workspace / "agents" / agent_id / ".maurice" / "prov"


def _store_web_uploads(
    root: Path,
    *,
    session_id: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(attachments) > MAX_WEB_UPLOADS:
        raise ValueError("too_many_uploads")
    session_slug = _safe_slug(session_id or "web")
    upload_dir = root / session_slug
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict):
            raise ValueError("invalid_upload")
        mime_type = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        if mime_type not in IMAGE_MIME_EXTENSIONS:
            raise ValueError("unsupported_upload_type")
        raw = _decode_upload_data(str(attachment.get("data") or ""))
        if not raw:
            raise ValueError("empty_upload")
        if len(raw) > MAX_WEB_UPLOAD_BYTES:
            raise ValueError("upload_too_large")
        original_name = str(attachment.get("name") or f"image-{index}{IMAGE_MIME_EXTENSIONS[mime_type]}")
        stem = _safe_slug(Path(original_name).stem or f"image-{index}")
        filename = f"{index:02d}-{stem}{IMAGE_MIME_EXTENSIONS[mime_type]}"
        path = upload_dir / filename
        path.write_bytes(raw)
        stored.append(
            {
                "name": original_name,
                "path": str(path.resolve()),
                "mime_type": mime_type,
                "bytes": len(raw),
            }
        )
    return stored


def _decode_upload_data(value: str) -> bytes:
    raw = value.strip()
    if "," in raw and raw[:64].lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_upload_data") from exc


def _read_web_attachment(path_value: str) -> tuple[str, bytes] | None:
    path = Path(path_value).expanduser().resolve()
    parts = path.parts
    if "prov" not in parts or "uploads" not in parts or not path.is_file():
        return None
    mime_type = _image_mime_for_path(path)
    if mime_type is None:
        return None
    raw = path.read_bytes()
    if len(raw) > MAX_WEB_UPLOAD_BYTES:
        return None
    return mime_type, raw


def _image_mime_for_path(path: Path) -> str | None:
    suffix = path.suffix.lower()
    for mime_type, extension in IMAGE_MIME_EXTENSIONS.items():
        if suffix == extension:
            return mime_type
    if suffix == ".jpeg":
        return "image/jpeg"
    return None


def _safe_slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    normalized = normalized.strip("._-")
    return normalized[:80] or "upload"


def _cancel_requested(cancel_event: Any | None) -> bool:
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _cancel_turn_for_context(ctx: MauriceContext, agent_id: str, session_id: str) -> bool:
    return MauriceClient(ctx).cancel_turn(agent_id=agent_id, session_id=session_id)


def _gateway_rate_limit_config(ctx: MauriceContext) -> GatewayRateLimitConfig:
    bundle = ctx.config
    if isinstance(bundle, ConfigBundle):
        return bundle.host.gateway.rate_limit
    return GatewayRateLimitConfig()


def _make_telegram_progress_factory(workspace: Path, bundle: ConfigBundle, agent_id: str):
    """Return a make_channel_progress_callback factory for Telegram, or None if unconfigured."""
    def factory(channel: str, peer_id: str, source_metadata: dict) -> "ProgressCallback | None":
        if channel != "telegram":
            return None
        chat_id = source_metadata.get("chat_id")
        if not isinstance(chat_id, int):
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                return None
        telegram_cfg = _telegram_channel_for_agent(bundle, agent_id)
        if telegram_cfg is None:
            return None
        _cred_name, cfg = telegram_cfg
        credential_name = cfg.get("credential") or "telegram_bot"
        try:
            token = _credential_value(workspace, credential_name)
        except Exception:
            return None
        if not token:
            return None
        return make_telegram_progress_callback(token, chat_id)
    return factory



def _gateway_router_for_local_context(ctx: MauriceContext) -> MessageRouter:
    event_store = EventStore(ctx.events_path)

    def run_gateway_turn(**kwargs):
        return _run_turn_via_local_server(
            ctx,
            message=kwargs["message"],
            session_id=kwargs["session_id"],
            agent_id=kwargs.get("agent_id") or "main",
            source_channel=kwargs.get("source_channel"),
            source_peer_id=kwargs.get("source_peer_id"),
            source_metadata=_source_metadata_with_active_project(
                ctx,
                kwargs.get("source_metadata"),
            ),
            limits=kwargs.get("limits"),
            message_metadata=kwargs.get("message_metadata"),
            cancel_event=kwargs.get("cancel_event"),
        )

    skill_registry = SkillLoader(
        ctx.skill_roots,
        enabled_skills=ctx.enabled_skills,
        scope=ctx.scope,
        event_store=event_store,
        agent_id="main",
        session_id="gateway",
    ).load()
    command_registry = CommandRegistry.from_skill_registry(skill_registry)

    _progress_store = SessionProgressStore()
    return MessageRouter(
        run_turn=run_gateway_turn,
        event_store=event_store,
        default_agent_id="main",
        approval_store_for=lambda _target_agent_id: ApprovalStore(
            ctx.approvals_path,
            event_store=event_store,
        ),
        reset_session=lambda target_agent_id, session_id: _reset_local_gateway_session(
            ctx,
            target_agent_id,
            session_id,
        ),
        record_exchange=lambda inbound, outbound, include_user: _record_gateway_exchange_for_context(
            ctx,
            inbound,
            outbound,
            include_user=include_user,
        ),
        command_registry=command_registry,
        command_callbacks=build_command_callbacks(
            ctx,
            command_registry=command_registry,
            model_summary=lambda _target_agent_id: _local_model_summary(ctx),
            extra={
                "compact_session": lambda target_agent_id, session_id: _compact_gateway_session_for_context(
                    ctx,
                    target_agent_id,
                    session_id,
                ),
            },
        ),
        rate_limit=_gateway_rate_limit_config(ctx),
        progress_store=_progress_store,
    )


def _gateway_router_for(
    workspace_root: Path,
    agent_id: str | None,
    *,
    active_project: Path | None = None,
):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    ctx = resolve_global_context(
        workspace,
        agent=agent,
        bundle=bundle,
        active_project=active_project,
    )
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )

    def run_gateway_turn(**kwargs):
        return _run_turn_via_global_server(
            ctx,
            message=kwargs["message"],
            session_id=kwargs["session_id"],
            agent_id=kwargs["agent_id"],
            source_channel=kwargs.get("source_channel"),
            source_peer_id=kwargs.get("source_peer_id"),
            source_metadata=kwargs.get("source_metadata"),
            limits=kwargs.get("limits"),
            message_metadata=kwargs.get("message_metadata"),
            cancel_event=kwargs.get("cancel_event"),
        )

    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    skill_registry = SkillLoader(
        ctx.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills or None,
        available_credentials=credentials.credentials.keys(),
        scope=ctx.scope,
        event_store=EventStore(event_stream),
        agent_id=agent.id,
        session_id="gateway",
    ).load()
    command_registry = CommandRegistry.from_skill_registry(skill_registry)
    loaded_skill_names = set(skill_registry.loaded())
    host_commands_enabled = "host" in loaded_skill_names

    def intercept_gateway_message(message, target_agent_id, session_id, correlation_id):
        if host_commands_enabled:
            return handle_agent_creation_wizard(
                workspace,
                agent_id=target_agent_id,
                session_id=session_id,
                text=message.text,
            )
        return None

    _progress_store = SessionProgressStore()
    return (
        MessageRouter(
            run_turn=run_gateway_turn,
            event_store=EventStore(event_stream),
            default_agent_id=agent.id,
            approval_store_for=lambda target_agent_id: ApprovalStore(
                workspace / "agents" / target_agent_id / "approvals.json",
                event_store=EventStore(event_stream),
            ),
            reset_session=lambda target_agent_id, session_id: _reset_gateway_session(
                workspace,
                target_agent_id,
                session_id,
            ),
            intercept_message=intercept_gateway_message,
            record_exchange=lambda inbound, outbound, include_user: _record_gateway_exchange(
                workspace,
                inbound,
                outbound,
                include_user=include_user,
            ),
            command_registry=command_registry,
            command_callbacks=build_command_callbacks(
                ctx,
                command_registry=command_registry,
                agent_workspace=Path(agent.workspace),
                agent_workspace_for=lambda target_agent_id: Path(
                    load_workspace_config(workspace).agents.agents[target_agent_id].workspace
                ),
                model_summary=lambda target_agent_id: _gateway_model_summary(
                    workspace,
                    target_agent_id,
                ),
                extra={
                    "compact_session": lambda target_agent_id, session_id: _compact_gateway_session(
                        workspace,
                        target_agent_id,
                        session_id,
                    ),
                    "clear_conversation_state": lambda target_agent_id, session_id: _clear_gateway_conversation_state(
                        workspace,
                        target_agent_id,
                        session_id,
                    ),
                },
            ),
            rate_limit=_gateway_rate_limit_config(ctx),
            progress_store=_progress_store,
            make_channel_progress_callback=_make_telegram_progress_factory(workspace, bundle, agent.id),
        ),
        agent,
        bundle,
    )



def _run_turn_via_global_server(
    ctx,
    *,
    message: str,
    session_id: str,
    agent_id: str,
    source_channel: str | None = None,
    source_peer_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    message_metadata: dict[str, Any] | None = None,
    cancel_event: Any | None = None,
    text_delta_callback: Any | None = None,
    **_extra: Any,
) -> TurnResult:
    source_metadata = _source_metadata_with_active_project(ctx, source_metadata)
    client = MauriceClient(ctx)
    if not client.is_running():
        runtime = _agent_runtime_registry().get_or_create(ctx.context_root, agent_id)
        return runtime.run_turn(
            message=message,
            session_id=session_id,
            agent_id=agent_id,
            source_channel=source_channel,
            source_peer_id=source_peer_id,
            source_metadata=source_metadata,
            limits=limits,
            message_metadata=message_metadata,
            cancel_event=cancel_event,
            text_delta_callback=text_delta_callback,
        )
    client.connect()
    assistant_text = ""
    status = "failed"
    status_code: str | None = None
    turn_correlation_id = ""
    input_tokens = 0
    output_tokens = 0
    error_message: str | None = None
    tool_results: list[ToolResult] = []
    try:
        for event in client.run_turn(
            message,
            session_id=session_id,
            agent_id=agent_id,
            source_channel=source_channel,
            source_peer_id=source_peer_id,
            source_metadata=source_metadata,
            limits=limits,
            message_metadata=message_metadata,
            approval_mode="store",
        ):
            if _cancel_requested(cancel_event):
                MauriceClient(ctx).cancel_turn(agent_id=agent_id, session_id=session_id)
            kind = event.get("type")
            if kind == "text_delta":
                delta = str(event.get("delta") or "")
                assistant_text += delta
                if text_delta_callback is not None:
                    try:
                        text_delta_callback(delta)
                    except Exception:
                        pass
            elif kind == "tool_result":
                error_code = event.get("error")
                tool_results.append(
                    ToolResult(
                        ok=bool(event.get("ok")),
                        summary=str(event.get("summary") or ""),
                        trust="trusted",
                        artifacts=event.get("artifacts") if isinstance(event.get("artifacts"), list) else [],
                        error=(
                            ToolError(
                                code=str(error_code),
                                message=str(error_code),
                                retryable=False,
                            )
                            if error_code
                            else None
                        ),
                    )
                )
            elif kind == "error":
                error_message = str(event.get("message") or "")
            elif kind == "done":
                status = str(event.get("status") or status)
                status_code = event.get("status_code") or None
                turn_correlation_id = str(event.get("correlation_id") or turn_correlation_id)
                if not assistant_text and event.get("assistant_text"):
                    assistant_text = str(event.get("assistant_text") or "")
                input_tokens = int(event.get("input_tokens") or 0)
                output_tokens = int(event.get("output_tokens") or 0)
    finally:
        client.close()
    store = SessionStore(ctx.sessions_path)
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        session = store.create(agent_id, session_id=session_id)
    tool_activity = _session_tool_activity(session.messages).get(turn_correlation_id, [])
    return TurnResult(
        session=session,
        correlation_id=turn_correlation_id,
        assistant_text=assistant_text,
        tool_results=tool_results,
        status=status,
        status_code=status_code,
        error=error_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_activity=tool_activity,
    )


def _source_metadata_with_active_project(
    ctx: MauriceContext,
    source_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if ctx.active_project_root is None:
        return source_metadata
    metadata = dict(source_metadata or {})
    metadata.setdefault("active_project_root", str(ctx.active_project_root))
    return metadata


def _run_turn_via_local_server(
    ctx: MauriceContext,
    *,
    message: str,
    session_id: str,
    agent_id: str,
    source_channel: str | None = None,
    source_peer_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    message_metadata: dict[str, Any] | None = None,
    cancel_event: Any | None = None,
    text_delta_callback: Any | None = None,
    **_extra: Any,
) -> TurnResult:
    client = MauriceClient(ctx)
    client.ensure_running()
    client.connect()
    assistant_text = ""
    status = "failed"
    status_code: str | None = None
    turn_correlation_id = ""
    input_tokens = 0
    output_tokens = 0
    error_message: str | None = None
    tool_results: list[ToolResult] = []
    try:
        for event in client.run_turn(
            message,
            session_id=session_id,
            agent_id=agent_id,
            source_channel=source_channel,
            source_peer_id=source_peer_id,
            source_metadata=source_metadata,
            limits=limits,
            message_metadata=message_metadata,
            approval_mode="store",
        ):
            if _cancel_requested(cancel_event):
                MauriceClient(ctx).cancel_turn(agent_id=agent_id, session_id=session_id)
            kind = event.get("type")
            if kind == "text_delta":
                delta = str(event.get("delta") or "")
                assistant_text += delta
                if text_delta_callback is not None:
                    try:
                        text_delta_callback(delta)
                    except Exception:
                        pass
            elif kind == "tool_result":
                error_code = event.get("error")
                tool_results.append(
                    ToolResult(
                        ok=bool(event.get("ok")),
                        summary=str(event.get("summary") or ""),
                        trust="trusted",
                        artifacts=event.get("artifacts") if isinstance(event.get("artifacts"), list) else [],
                        error=(
                            ToolError(
                                code=str(error_code),
                                message=str(error_code),
                                retryable=False,
                            )
                            if error_code
                            else None
                        ),
                    )
                )
            elif kind == "error":
                error_message = str(event.get("message") or "")
            elif kind == "done":
                status = str(event.get("status") or status)
                status_code = event.get("status_code") or None
                turn_correlation_id = str(event.get("correlation_id") or turn_correlation_id)
                if not assistant_text and event.get("assistant_text"):
                    assistant_text = str(event.get("assistant_text") or "")
                input_tokens = int(event.get("input_tokens") or 0)
                output_tokens = int(event.get("output_tokens") or 0)
    finally:
        client.close()
    store = SessionStore(ctx.sessions_path)
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        session = store.create(agent_id, session_id=session_id)
    tool_activity = _session_tool_activity(session.messages).get(turn_correlation_id, [])
    return TurnResult(
        session=session,
        correlation_id=turn_correlation_id,
        assistant_text=assistant_text,
        tool_results=tool_results,
        status=status,
        status_code=status_code,
        error=error_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_activity=tool_activity,
    )


def _reset_local_gateway_session(ctx: MauriceContext, agent_id: str, session_id: str) -> None:
    try:
        SessionStore(ctx.sessions_path).reset(agent_id, session_id)
    except FileNotFoundError:
        SessionStore(ctx.sessions_path).create(agent_id, session_id=session_id)


def _reset_gateway_session(workspace: Path, agent_id: str, session_id: str) -> None:
    store = SessionStore(workspace / "sessions")
    try:
        store.reset(agent_id, session_id)
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)


def _clear_gateway_conversation_state(workspace: Path, agent_id: str, session_id: str) -> list[str]:
    cleared = []
    if clear_agent_creation_wizard(workspace, agent_id=agent_id, session_id=session_id):
        cleared.append("configuration d'agent")
    if clear_secret_capture(workspace, agent_id=agent_id, session_id=session_id):
        cleared.append("saisie de secret")
    return cleared



def _record_gateway_exchange(
    workspace: Path,
    inbound,
    outbound,
    *,
    include_user: bool,
) -> None:
    _record_gateway_exchange_in_store(
        SessionStore(workspace / "sessions"),
        inbound,
        outbound,
        include_user=include_user,
    )


def _record_gateway_exchange_for_context(
    ctx: MauriceContext,
    inbound,
    outbound,
    *,
    include_user: bool,
) -> None:
    _record_gateway_exchange_in_store(
        SessionStore(ctx.sessions_path),
        inbound,
        outbound,
        include_user=include_user,
    )


def _record_gateway_exchange_in_store(
    store: SessionStore,
    inbound,
    outbound,
    *,
    include_user: bool,
) -> None:
    try:
        store.load(outbound.agent_id, outbound.session_id)
    except FileNotFoundError:
        store.create(outbound.agent_id, session_id=outbound.session_id)
    if include_user:
        metadata = {"channel": inbound.channel, "peer_id": inbound.peer_id}
        attachments = inbound.metadata.get("attachments") if isinstance(inbound.metadata, dict) else None
        if attachments:
            metadata["attachments"] = attachments
        visible_user_message = inbound.metadata.get("visible_user_message") if isinstance(inbound.metadata, dict) else None
        if visible_user_message is not None:
            metadata["visible_user_message"] = visible_user_message
        store.append_message(
            outbound.agent_id,
            outbound.session_id,
            role="user",
            content=inbound.text,
            correlation_id=outbound.correlation_id,
            metadata=metadata,
        )
    if outbound.text:
        assistant_correlation_id = outbound.metadata.get("kernel_correlation_id") or outbound.correlation_id
        store.append_message(
            outbound.agent_id,
            outbound.session_id,
            role="assistant",
            content=outbound.text,
            correlation_id=assistant_correlation_id,
            metadata=outbound.metadata,
        )


def _compact_gateway_session_for_context(ctx: MauriceContext, agent_id: str, session_id: str) -> str:
    return _compact_gateway_session_in_store(SessionStore(ctx.sessions_path), agent_id, session_id)



def _compact_gateway_session(workspace: Path, agent_id: str, session_id: str) -> str:
    summary = _compact_gateway_session_in_store(SessionStore(workspace / "sessions"), agent_id, session_id)
    _clear_gateway_conversation_state(workspace, agent_id, session_id)
    return summary


def _compact_gateway_session_in_store(store: SessionStore, agent_id: str, session_id: str) -> str:
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)
        return "Session vide. Rien a compacter."
    if not session.messages:
        return "Session vide. Rien a compacter."

    message_count = len(session.messages)
    user_count = sum(1 for message in session.messages if message.role == "user")
    assistant_count = sum(1 for message in session.messages if message.role == "assistant")
    recent = [
        f"- {message.role}: {_compact_text(message.content, 220)}"
        for message in session.messages[-8:]
    ]
    summary = (
        "Session compactee localement.\n\n"
        f"Messages compactes : {message_count} "
        f"({user_count} utilisateur, {assistant_count} assistant).\n\n"
        "Derniers elements conserves :\n"
        + "\n".join(recent)
    )
    store.reset(agent_id, session_id)
    store.append_message(
        agent_id,
        session_id,
        role="system",
        content=summary,
        metadata={"compacted": True, "original_message_count": message_count},
    )
    return "Session compactee. J'ai garde un resume court dans le contexte."


def _local_model_summary(ctx: MauriceContext) -> str:
    provider = ctx.config.provider if isinstance(ctx.config, LocalConfig) else {}
    kind = provider.get("type", "mock")
    model = provider.get("model", "")
    return f"Modele courant : `{kind}:{model or 'mock'}`.\n\nChange-le depuis le bouton `Modele` du chat web."



def _gateway_model_summary(workspace: Path, agent_id: str) -> str:
    bundle = load_workspace_config(workspace)
    agent = _resolve_agent(bundle, agent_id)
    model = _effective_model_config(bundle, agent)
    provider = model.get("provider") or "inconnu"
    protocol = model.get("protocol") or "defaut"
    name = model.get("name") or "defaut"
    return (
        f"Modele de `{agent.id}` : `{provider}:{name}`.\n\n"
        f"Protocole : `{protocol}`.\n"
        "Change-le depuis le bouton `Modele` du chat web."
    )


def _gateway_model_status(workspace: Path, agent_id: str) -> dict[str, Any]:
    bundle = load_workspace_config(workspace)
    agent = _resolve_agent(bundle, agent_id)
    model = _effective_model_config(bundle, agent)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    choices = _model_choices(model, credentials)
    current = str(model.get("name") or "mock")
    return {
        "ok": True,
        "available": True,
        "agent_id": agent.id,
        "provider": model.get("provider") or "mock",
        "protocol": model.get("protocol"),
        "model": current,
        "choices": _choice_rows(choices, current),
    }


def _gateway_model_update(workspace: Path, agent_id: str, model_name: str) -> dict[str, Any]:
    bundle = load_workspace_config(workspace)
    agent = _resolve_agent(bundle, agent_id)
    model = _effective_model_config(bundle, agent)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    choices = _model_choices(model, credentials)
    valid_ids = {model_id for model_id, _label in choices}
    if valid_ids and model_name not in valid_ids:
        return {"ok": False, "error": "unknown_model", "model": model_name}

    updated_model = {**model, "name": model_name}
    if agent.default and not agent.model_chain:
        _write_gateway_kernel_model(workspace, updated_model)
    else:
        profile_id = _upsert_gateway_model_profile(workspace, updated_model)
        update_agent(workspace, agent_id=agent.id, model_chain=[profile_id])
    return _gateway_model_status(workspace, agent.id)


def _write_gateway_kernel_model(workspace: Path, model: dict[str, Any]) -> None:
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel = kernel_data.setdefault("kernel", {})
    _register_gateway_model_profile(kernel, model, make_default=True)
    write_yaml_file(kernel_path, kernel_data)


def _upsert_gateway_model_profile(workspace: Path, model: dict[str, Any]) -> str:
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel = kernel_data.setdefault("kernel", {})
    profile_id = _register_gateway_model_profile(kernel, model, make_default=False)
    write_yaml_file(kernel_path, kernel_data)
    return profile_id


def _register_gateway_model_profile(kernel: dict[str, Any], model: dict[str, Any], *, make_default: bool) -> str:
    payload = model_profile_payload(model)
    profile_id = model_profile_id(payload)
    models = kernel.setdefault("models", {})
    entries = models.setdefault("entries", {})
    entries[profile_id] = payload
    if make_default:
        models["default"] = profile_id
    return profile_id


def _gateway_agent_config_status(workspace: Path, agent_id: str) -> dict[str, Any]:
    bundle = load_workspace_config(workspace)
    agent = _resolve_agent(bundle, agent_id)
    available_skills = _available_skills(workspace)
    enabled_skills = _effective_agent_skills(bundle, agent, available_skills)
    model_status = _gateway_model_status(workspace, agent.id)
    model = _effective_model_config(bundle, agent)
    model_chain = _gateway_agent_model_chain(bundle, agent)
    provider_key = _provider_key_for_model(model)
    credential_name = str(model.get("credential") or "")
    credential_present = bool(
        credential_name and credential_name in load_workspace_credentials(workspace).credentials
    )
    web_search = _web_search_config_status(workspace, "web" in enabled_skills)
    telegram = _telegram_channel_for_agent(bundle, agent.id)
    telegram_payload: dict[str, Any] = {"enabled": False}
    if telegram is not None:
        channel_name, channel_config = telegram
        allowed_users = _int_list(channel_config.get("allowed_users"))
        allowed_chats = _int_list(channel_config.get("allowed_chats"))
        telegram_payload = {
            "enabled": channel_config.get("enabled", True) is not False,
            "channel": channel_name,
            "credential": channel_config.get("credential"),
            "allowed_users": allowed_users,
            "allowed_chats": allowed_chats,
            "default_session_id": canonical_session_id(agent.id),
            "status": channel_config.get("status"),
        }
    return {
        "ok": True,
        "available": True,
        "agent": {
            "id": agent.id,
            "permission_profile": agent.permission_profile,
            "skills": enabled_skills,
            "default": agent.default,
            "status": agent.status,
        },
        "permissions": [
            {"id": "safe", "label": "safe", "description": "lecture seule et tres prudent"},
            {"id": "limited", "label": "limited", "description": "lecture/ecriture dans son espace"},
            {"id": "power", "label": "power", "description": "acces etendu"},
        ],
        "skills": [
            {
                "id": name,
                "description": description,
                "enabled": name in enabled_skills,
            }
            for name, description in available_skills.items()
        ],
        "model": model_status,
        "model_chain": [
            {
                "id": item["id"],
                "provider": item["model"].get("provider") or "mock",
                "provider_key": _provider_key_for_model(item["model"]),
                "protocol": item["model"].get("protocol"),
                "model": item["model"].get("name") or "mock",
            }
            for item in model_chain
        ],
        "fallback": _gateway_fallback_status(model_chain),
        "worker": _gateway_worker_status(bundle, agent),
        "provider": {
            "current": provider_key,
            "base_url": model.get("base_url"),
            "credential": credential_name or None,
            "credential_present": credential_present,
            "choices": _provider_choices_for_web(workspace, model),
        },
        "web_search": web_search,
        "scheduler": _gateway_scheduler_status(bundle),
        "skill_setups": skill_setup_status(
            workspace,
            bundle.host.skill_roots,
            enabled_skills=enabled_skills,
        ),
        "telegram": telegram_payload,
    }


def _gateway_agent_model_chain(bundle: ConfigBundle, agent) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if agent.model_chain:
        for model_id in agent.model_chain:
            profile = bundle.kernel.models.entries.get(model_id)
            if profile is not None:
                result.append({"id": model_id, "model": profile.model_dump(mode="json")})
        if result:
            return result
    profile = bundle.kernel.models.entries.get(bundle.kernel.models.default)
    if profile is None:
        return []
    return [{"id": bundle.kernel.models.default, "model": profile.model_dump(mode="json")}]


def _gateway_fallback_status(model_chain: list[dict[str, Any]]) -> dict[str, Any]:
    if len(model_chain) < 2:
        return {"enabled": False}
    fallback = model_chain[1]["model"]
    return {
        "enabled": True,
        "profile_id": model_chain[1]["id"],
        "provider": _provider_key_for_model(fallback),
        "model": fallback.get("name") or "mock",
    }


def _gateway_worker_status(bundle: ConfigBundle, agent) -> dict[str, Any]:
    chain = _gateway_worker_model_chain(bundle, agent)
    if not chain:
        return {"enabled": False, "inherits_parent": True, "model_chain": []}
    first = chain[0]["model"]
    return {
        "enabled": bool(agent.worker_model_chain),
        "inherits_parent": not bool(agent.worker_model_chain),
        "profile_id": chain[0]["id"],
        "provider": _provider_key_for_model(first),
        "model": first.get("name") or "mock",
        "model_chain": [
            {
                "id": item["id"],
                "provider": item["model"].get("provider") or "mock",
                "provider_key": _provider_key_for_model(item["model"]),
                "protocol": item["model"].get("protocol"),
                "model": item["model"].get("name") or "mock",
            }
            for item in chain
        ],
    }


def _gateway_worker_model_chain(bundle: ConfigBundle, agent) -> list[dict[str, Any]]:
    if agent.worker_model_chain:
        result = []
        for model_id in agent.worker_model_chain:
            profile = bundle.kernel.models.entries.get(model_id)
            if profile is not None:
                result.append({"id": model_id, "model": profile.model_dump(mode="json")})
        if result:
            return result
    return _gateway_agent_model_chain(bundle, agent)


def _gateway_scheduler_status(bundle: ConfigBundle) -> dict[str, Any]:
    scheduler = bundle.kernel.scheduler
    return {
        "enabled": bool(scheduler.enabled),
        "dreaming_enabled": bool(scheduler.dreaming_enabled),
        "dreaming_time": scheduler.dreaming_time,
        "daily_enabled": bool(scheduler.daily_enabled),
        "daily_time": scheduler.daily_time,
        "sentinelle_enabled": bool(scheduler.sentinelle_enabled),
        "sentinelle_time": scheduler.sentinelle_time,
    }


def _gateway_agent_config_update(workspace: Path, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    bundle = load_workspace_config(workspace)
    agent = _resolve_agent(bundle, agent_id)
    available_skills = _available_skills(workspace)

    permission_profile = str(payload.get("permission_profile") or agent.permission_profile)
    if permission_profile not in {"safe", "limited", "power"}:
        return {"ok": False, "error": "invalid_permission"}

    raw_skills = payload.get("skills")
    if raw_skills is None:
        skills = _effective_agent_skills(bundle, agent, available_skills)
    elif isinstance(raw_skills, list):
        skills = [str(item) for item in raw_skills]
    else:
        return {"ok": False, "error": "invalid_skills"}
    unknown_skills = [name for name in skills if name not in available_skills]
    if unknown_skills:
        return {"ok": False, "error": "unknown_skills", "skills": unknown_skills}

    try:
        model, credential_to_allow = _web_model_from_agent_payload(
            workspace,
            _effective_model_config(bundle, agent),
            payload,
        )
        fallback_model, fallback_credential_to_allow = _web_fallback_model_from_agent_payload(
            workspace,
            model,
            payload,
        )
        worker_model, worker_credential_to_allow = _web_worker_model_from_agent_payload(
            workspace,
            model,
            payload,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    credentials = list(agent.credentials)
    if credential_to_allow and credential_to_allow not in credentials:
        credentials.append(credential_to_allow)
    if fallback_credential_to_allow and fallback_credential_to_allow not in credentials:
        credentials.append(fallback_credential_to_allow)
    if worker_credential_to_allow and worker_credential_to_allow not in credentials:
        credentials.append(worker_credential_to_allow)
    try:
        _apply_web_search_config(workspace, payload)
        _apply_gateway_scheduler_config(workspace, payload)
        _apply_gateway_skill_setup_config(workspace, bundle.host.skill_roots, skills, payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    profile_id = _upsert_gateway_model_profile(workspace, model)
    fallback_profile_id = _upsert_gateway_model_profile(workspace, fallback_model) if fallback_model else None
    worker_profile_id = _upsert_gateway_model_profile(workspace, worker_model) if worker_model else None
    worker_model_chain = (
        [worker_profile_id] if worker_profile_id else []
    ) if "worker_enabled" in payload else None
    if agent.default and fallback_profile_id is None:
        _write_gateway_kernel_model(workspace, model)
        model_chain: list[str] = []
    else:
        if agent.default:
            _write_gateway_kernel_model(workspace, model)
        model_chain = [profile_id]
        if fallback_profile_id and fallback_profile_id != profile_id:
            model_chain.append(fallback_profile_id)
    updated = update_agent(
        workspace,
        agent_id=agent.id,
        permission_profile=permission_profile,
        skills=skills,
        credentials=credentials,
        model_chain=model_chain,
        worker_model_chain=worker_model_chain,
        confirmed_permission_elevation=True,
    )
    if any(
        key in payload
        for key in {
            "scheduler_enabled",
            "dreaming_enabled",
            "dreaming_time",
            "daily_enabled",
            "daily_time",
            "sentinelle_enabled",
            "sentinelle_time",
            "skill_setup",
        }
    ):
        from maurice.host.commands.scheduler import _ensure_configured_scheduler_jobs

        _ensure_configured_scheduler_jobs(workspace, updated.id)
    _apply_web_telegram_config(workspace, updated, payload)
    return {
        **_gateway_agent_config_status(workspace, updated.id),
        "updated": True,
    }


def _gateway_agent_config_update_and_sync_telegram(
    workspace: Path,
    agent_id: str,
    payload: dict[str, Any],
    *,
    telegram_pollers: _WebTelegramPollers | None,
) -> dict[str, Any]:
    result = _gateway_agent_config_update(workspace, agent_id, payload)
    if result.get("ok") and telegram_pollers is not None and "telegram_enabled" in payload:
        telegram_pollers.sync()
        telegram = result.get("telegram")
        if isinstance(telegram, dict) and telegram.get("enabled"):
            telegram["runtime"] = "polling"
            telegram["status"] = "active"
    return result


def _effective_agent_skills(bundle: ConfigBundle, agent, available_skills: dict[str, str]) -> list[str]:
    configured = list(agent.skills or bundle.kernel.skills or [])
    if not configured:
        return list(available_skills)
    return [name for name in configured if name in available_skills]


def _provider_choices_for_web(workspace: Path, current_model: dict[str, Any]) -> list[dict[str, Any]]:
    current_provider = _provider_key_for_model(current_model)
    current_name = str(current_model.get("name") or "")

    def current_for(provider: str) -> str:
        return current_name if provider == current_provider else ""

    return [
        {
            "id": "chatgpt",
            "label": "ChatGPT",
            "default_model": "gpt-5",
            "models": _web_model_rows(
                chatgpt_model_choices(),
                current=current_for("chatgpt"),
                fallback="gpt-5",
            ),
            "needs_base_url": False,
            "needs_api_key": False,
        },
        {
            "id": "openai_api",
            "label": "OpenAI-compatible API",
            "default_model": "gpt-4o-mini",
            "default_base_url": "https://api.openai.com/v1",
            "models": _web_model_rows(
                _openai_compatible_model_choices(),
                current=current_for("openai_api"),
                fallback="gpt-4o-mini",
            ),
            "needs_base_url": True,
            "needs_api_key": True,
        },
        {
            "id": "ollama_local",
            "label": "Ollama local",
            "default_model": "llama3.1",
            "default_base_url": "http://localhost:11434",
            "models": _web_model_rows(
                ollama_model_choices("http://localhost:11434"),
                current=current_for("ollama_local"),
                fallback="llama3.1",
            ),
            "needs_base_url": True,
            "needs_api_key": False,
        },
        {
            "id": "ollama_api",
            "label": "Ollama API",
            "default_model": "gpt-oss:20b",
            "default_base_url": "https://ollama.com",
            "models": _web_model_rows(
                _ollama_api_model_choices(workspace, current_model),
                current=current_for("ollama_api"),
                fallback="gpt-oss:20b",
            ),
            "needs_base_url": True,
            "needs_api_key": True,
        },
    ]


def _web_model_rows(
    choices: list[tuple[str, str]],
    *,
    current: str,
    fallback: str,
) -> list[dict[str, str]]:
    rows = [{"id": model_id, "label": _web_model_label(model_id, label)} for model_id, label in choices]
    ids = {row["id"] for row in rows}
    if current and current not in ids:
        rows.insert(0, {"id": current, "label": current})
    if not rows and fallback:
        rows.append({"id": fallback, "label": fallback})
    return rows


def _web_model_label(model_id: str, label: str) -> str:
    if not label or label == model_id:
        return model_id
    return f"{model_id} - {label}"


def _openai_compatible_model_choices() -> list[tuple[str, str]]:
    return [
        ("gpt-4o-mini", "rapide et economique"),
        ("gpt-4o", "generaliste"),
    ]


def _ollama_api_model_choices(workspace: Path, current_model: dict[str, Any]) -> list[tuple[str, str]]:
    base_url = str(current_model.get("base_url") or "https://ollama.com")
    credential_name = str(current_model.get("credential") or "")
    credential = load_workspace_credentials(workspace).credentials.get(credential_name) if credential_name else None
    return ollama_model_choices(base_url, api_key=credential.value if credential is not None else "")


def _provider_key_for_model(model: dict[str, Any]) -> str:
    provider = str(model.get("provider") or "mock")
    protocol = str(model.get("protocol") or "")
    if provider == "auth" and protocol == "chatgpt_codex":
        return "chatgpt"
    if provider in {"api", "openai"} and protocol in {"openai_chat_completions", ""}:
        return "openai_api"
    if provider == "ollama":
        return "ollama_api" if model.get("credential") else "ollama_local"
    return "chatgpt"


def _web_model_from_agent_payload(
    workspace: Path,
    current_model: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    return _web_model_from_payload(
        workspace,
        current_model,
        payload,
        provider_key="provider",
        model_key="model",
        base_url_key="base_url",
        api_key_key="api_key",
    )


def _web_fallback_model_from_agent_payload(
    workspace: Path,
    current_model: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if payload.get("fallback_enabled") is not True:
        return None, None
    fallback_provider = str(payload.get("fallback_provider") or "").strip()
    fallback_model = str(payload.get("fallback_model") or "").strip()
    if not fallback_provider:
        raise ValueError("invalid_fallback_provider")
    model, credential = _web_model_from_payload(
        workspace,
        current_model,
        payload,
        provider_key="fallback_provider",
        model_key="fallback_model",
        base_url_key="fallback_base_url",
        api_key_key="fallback_api_key",
    )
    if fallback_model and str(model.get("name") or "") != fallback_model:
        raise ValueError("unknown_fallback_model")
    return model, credential


def _web_worker_model_from_agent_payload(
    workspace: Path,
    current_model: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if "worker_enabled" not in payload:
        return None, None
    if payload.get("worker_enabled") is not True:
        return None, None
    worker_provider = str(payload.get("worker_provider") or "").strip()
    worker_model = str(payload.get("worker_model") or "").strip()
    if not worker_provider:
        raise ValueError("invalid_worker_provider")
    model, credential = _web_model_from_payload(
        workspace,
        current_model,
        payload,
        provider_key="worker_provider",
        model_key="worker_model",
        base_url_key="worker_base_url",
        api_key_key="worker_api_key",
    )
    if worker_model and str(model.get("name") or "") != worker_model:
        raise ValueError("unknown_worker_model")
    return model, credential


def _web_model_from_payload(
    workspace: Path,
    current_model: dict[str, Any],
    payload: dict[str, Any],
    *,
    provider_key: str,
    model_key: str,
    base_url_key: str,
    api_key_key: str,
) -> tuple[dict[str, Any], str | None]:
    provider = str(payload.get(provider_key) or _provider_key_for_model(current_model))
    provider_choices = _provider_choices_for_web(workspace, current_model)
    valid_providers = {choice["id"] for choice in provider_choices}
    if provider not in valid_providers:
        raise ValueError("invalid_provider")
    provider_choice = next(choice for choice in provider_choices if choice["id"] == provider)
    model_name = str(payload.get(model_key) or "").strip()
    if not model_name and provider_key == "provider":
        model_name = str(current_model.get("name") or "").strip()
    valid_models = {
        str(model.get("id"))
        for model in provider_choice.get("models", [])
        if isinstance(model, dict) and model.get("id")
    }
    if model_name and valid_models and model_name not in valid_models:
        raise ValueError("unknown_model")
    if not model_name:
        model_name = str(provider_choice.get("default_model") or "")
    base_url = str(
        payload.get(base_url_key)
        or (current_model.get("base_url") if provider == _provider_key_for_model(current_model) else "")
        or provider_choice.get("default_base_url")
        or ""
    ).strip()
    api_key = str(payload.get(api_key_key) or "").strip()

    if provider == "chatgpt":
        return (
            {
                "provider": "auth",
                "protocol": "chatgpt_codex",
                "name": model_name or "gpt-5",
                "base_url": None,
                "credential": CHATGPT_CREDENTIAL_NAME,
            },
            CHATGPT_CREDENTIAL_NAME,
        )
    if provider == "openai_api":
        credential = "openai"
        base_url = base_url or "https://api.openai.com/v1"
        if api_key:
            _save_web_api_credential(workspace, credential, api_key, base_url)
        return (
            {
                "provider": "api",
                "protocol": "openai_chat_completions",
                "name": model_name or "gpt-4o-mini",
                "base_url": base_url,
                "credential": credential,
            },
            credential,
        )
    if provider == "ollama_local":
        return (
            {
                "provider": "ollama",
                "protocol": "ollama_chat",
                "name": model_name or "llama3.1",
                "base_url": base_url or "http://localhost:11434",
                "credential": None,
            },
            None,
        )
    if provider == "ollama_api":
        credential = "ollama"
        base_url = base_url or "https://ollama.com"
        if api_key:
            _save_web_api_credential(workspace, credential, api_key, base_url)
        return (
            {
                "provider": "ollama",
                "protocol": "ollama_chat",
                "name": model_name or "gpt-oss:20b",
                "base_url": base_url,
                "credential": credential,
            },
            credential,
        )
    raise ValueError("invalid_provider")


def _save_web_api_credential(workspace: Path, name: str, api_key: str, base_url: str) -> None:
    store = load_workspace_credentials(workspace)
    store.credentials[name] = CredentialRecord(type="api_key", value=api_key, base_url=base_url)
    write_workspace_credentials(workspace, store)


def _web_search_config_status(workspace: Path, web_enabled: bool) -> dict[str, Any]:
    skills_data = read_yaml_file(workspace_skills_config_path(workspace))
    web_config = skills_data.get("skills", {}).get("web", {})
    if not isinstance(web_config, dict):
        web_config = {}
    base_url = str(web_config.get("base_url") or "").strip()
    return {
        "enabled": web_enabled,
        "configured": bool(base_url),
        "base_url": base_url,
        "search_provider": str(web_config.get("search_provider") or "searxng"),
    }


def _apply_web_search_config(workspace: Path, payload: dict[str, Any]) -> None:
    if "web_search_base_url" not in payload:
        return
    base_url = str(payload.get("web_search_base_url") or "").strip()
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid_web_search_url")
    path = workspace_skills_config_path(workspace)
    skills_data = read_yaml_file(path)
    web_config = skills_data.setdefault("skills", {}).setdefault("web", {})
    if not isinstance(web_config, dict):
        web_config = {}
        skills_data["skills"]["web"] = web_config
    if base_url:
        web_config["search_provider"] = "searxng"
        web_config["base_url"] = base_url
    else:
        web_config.pop("search_provider", None)
        web_config.pop("base_url", None)
    write_yaml_file(path, skills_data)


def _apply_gateway_scheduler_config(workspace: Path, payload: dict[str, Any]) -> None:
    scheduler_keys = {
        "scheduler_enabled",
        "dreaming_enabled",
        "dreaming_time",
        "daily_enabled",
        "daily_time",
        "sentinelle_enabled",
        "sentinelle_time",
    }
    if not any(key in payload for key in scheduler_keys):
        return
    path = kernel_config_path(workspace)
    data = read_yaml_file(path)
    kernel = data.setdefault("kernel", {})
    scheduler = kernel.setdefault("scheduler", {})
    if "scheduler_enabled" in payload:
        scheduler["enabled"] = bool(payload.get("scheduler_enabled"))
    if "dreaming_enabled" in payload:
        scheduler["dreaming_enabled"] = bool(payload.get("dreaming_enabled"))
    if "daily_enabled" in payload:
        scheduler["daily_enabled"] = bool(payload.get("daily_enabled"))
    if "sentinelle_enabled" in payload:
        scheduler["sentinelle_enabled"] = bool(payload.get("sentinelle_enabled"))
    if "dreaming_time" in payload:
        scheduler["dreaming_time"] = _web_local_time(payload.get("dreaming_time"), "dreaming_time")
    if "daily_time" in payload:
        scheduler["daily_time"] = _web_local_time(payload.get("daily_time"), "daily_time")
    if "sentinelle_time" in payload:
        scheduler["sentinelle_time"] = _web_local_time(payload.get("sentinelle_time"), "sentinelle_time")
    write_yaml_file(path, data)


def _apply_gateway_skill_setup_config(
    workspace: Path,
    skill_roots: list[Any],
    enabled_skills: list[str],
    payload: dict[str, Any],
) -> None:
    raw = payload.get("skill_setup")
    updates: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("invalid_skill_setup")
    sentinelle_updates: dict[str, Any] = {}
    if "sentinelle_enabled" in payload:
        sentinelle_updates["daily_audit_enabled"] = bool(payload.get("sentinelle_enabled"))
    if "sentinelle_time" in payload:
        sentinelle_updates["daily_audit_time"] = _web_local_time(payload.get("sentinelle_time"), "sentinelle_time")
    if sentinelle_updates:
        existing = updates.get("sentinelle")
        if not isinstance(existing, dict):
            existing = {}
        existing.update(sentinelle_updates)
        updates["sentinelle"] = existing
    if not updates:
        return
    apply_skill_setup_updates(
        workspace,
        skill_roots,
        updates,
        enabled_skills=enabled_skills,
    )


def _web_local_time(value: Any, name: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raise ValueError(f"invalid_{name}")
    raw = raw.replace("h", ":")
    if ":" not in raw:
        raw = f"{raw}:00"
    hour_raw, minute_raw = raw.split(":", 1)
    try:
        hour = int(hour_raw)
        minute = int(minute_raw)
    except ValueError as exc:
        raise ValueError(f"invalid_{name}") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"invalid_{name}")
    return f"{hour:02d}:{minute:02d}"


def _apply_web_telegram_config(workspace: Path, agent, payload: dict[str, Any]) -> None:
    if "telegram_enabled" not in payload:
        return
    enabled = payload.get("telegram_enabled") is True
    host_path = host_config_path(workspace)
    data = read_yaml_file(host_path)
    host = data.setdefault("host", {})
    channels = host.setdefault("channels", {})
    channel_key = "telegram" if agent.id == "main" else f"telegram_{agent.id}"
    credential = "telegram_bot" if agent.id == "main" else f"telegram_bot_{agent.id}"
    token = str(payload.get("telegram_token") or "").strip()
    if token:
        store = load_workspace_credentials(workspace)
        store.credentials[credential] = CredentialRecord(
            type="token",
            value=token,
            provider="telegram_bot",
        )
        write_workspace_credentials(workspace, store)
    allowed_users = _int_values_from_web(payload.get("telegram_allowed_users"))
    previous = channels.get(channel_key) if isinstance(channels.get(channel_key), dict) else {}
    allowed_chats = previous.get("allowed_chats") if isinstance(previous, dict) else []
    allowed_chats = _telegram_allowed_chats_with_private_users(
        allowed_users,
        _int_list(allowed_chats),
    )
    if enabled:
        channels[channel_key] = {
            "adapter": "telegram",
            "enabled": True,
            "agent": agent.id,
            "credential": credential,
            "allowed_users": allowed_users,
            "allowed_chats": allowed_chats,
            "status": "configured_pending_restart" if token or credential in load_workspace_credentials(workspace).credentials else "missing_credential",
        }
        if "telegram" not in agent.channels:
            update_agent(workspace, agent_id=agent.id, channels=[*agent.channels, "telegram"])
    else:
        if isinstance(previous, dict):
            previous["enabled"] = False
            channels[channel_key] = previous
        if "telegram" in agent.channels:
            update_agent(
                workspace,
                agent_id=agent.id,
                channels=[channel for channel in agent.channels if channel != "telegram"],
            )
    write_yaml_file(host_path, data)


def _int_values_from_web(value: Any) -> list[int]:
    if isinstance(value, str):
        raw_items = re.split(r"[,;\s]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    result: list[int] = []
    for item in raw_items:
        try:
            text = str(item).strip()
            if text:
                result.append(int(text))
        except (TypeError, ValueError):
            continue
    return result


def _local_model_status(ctx: MauriceContext, agent_id: str) -> dict[str, Any]:
    provider = ctx.config.provider if isinstance(ctx.config, LocalConfig) else {}
    if not isinstance(provider, dict):
        provider = {}
    current = str(provider.get("model") or "mock")
    model = {
        "provider": provider.get("type") or "mock",
        "protocol": provider.get("protocol"),
        "name": current,
        "base_url": provider.get("base_url"),
        "credential": provider.get("credential"),
    }
    choices = _model_choices(model, load_workspace_credentials(ctx.context_root))
    return {
        "ok": True,
        "available": True,
        "agent_id": agent_id or "main",
        "provider": provider.get("type") or "mock",
        "protocol": provider.get("protocol"),
        "model": current,
        "choices": _choice_rows(choices, current),
    }


def _local_model_update(ctx: MauriceContext, agent_id: str, model_name: str) -> dict[str, Any]:
    cfg_path = config_path(ctx.context_root)
    data = read_yaml_file(cfg_path)
    provider = data.setdefault("provider", {})
    if not isinstance(provider, dict):
        provider = {}
        data["provider"] = provider
    provider["model"] = model_name
    write_yaml_file(cfg_path, data)
    if isinstance(ctx.config, LocalConfig):
        ctx.config.setdefault("provider", {})["model"] = model_name
    return _local_model_status(ctx, agent_id)


def _local_agent_config_status(ctx: MauriceContext, agent_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "error": "agent_config_global_only",
    }


def _local_agent_config_update(ctx: MauriceContext, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "error": "agent_config_global_only",
    }


def _model_choices(model: dict[str, Any], credentials: CredentialsStore) -> list[tuple[str, str]]:
    provider = str(model.get("provider") or "mock")
    protocol = str(model.get("protocol") or "")
    if provider == "auth" and protocol == "chatgpt_codex":
        return chatgpt_model_choices()
    if provider == "ollama":
        credential = _model_credential(model, credentials)
        return ollama_model_choices(
            str(model.get("base_url") or (credential.base_url if credential is not None else "") or "http://localhost:11434"),
            api_key=credential.value if credential is not None else "",
        )
    return []


def _choice_rows(choices: list[tuple[str, str]], current: str) -> list[dict[str, Any]]:
    rows = [
        {
            "id": model_id,
            "label": _web_model_label(model_id, label),
            "current": model_id == current,
        }
        for model_id, label in choices
    ]
    if current and current not in {row["id"] for row in rows}:
        rows.insert(0, {"id": current, "label": current, "current": True})
    return rows


def _gateway_git_root(workspace: Path, agent_id: str) -> Path:
    try:
        bundle = load_workspace_config(workspace)
        agent = _resolve_agent(bundle, agent_id)
        active = _active_dev_project_path(agent)
    except Exception:
        active = None
    return Path(active) if active is not None else workspace
