"""Auto-split from cli.py."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agent_wizard import clear_agent_creation_wizard, handle_agent_creation_wizard
from maurice.host.agents import archive_agent, create_agent, delete_agent, disable_agent, list_agents, update_agent
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME, ChatGPTAuthFlow, clear_chatgpt_auth,
    get_valid_chatgpt_access_token, load_chatgpt_auth, save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.command_registry import CommandRegistry, default_command_registry
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
from maurice.host.gateway import GatewayHttpServer, MessageRouter
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
from maurice.host.runtime import (
    run_one_turn, _resolve_agent, _agent_system_prompt, _active_dev_project_path,
    _provider_for_config, _effective_model_config, _model_credential,
    _effective_model_label, _default_agent,
)
from maurice.host.secret_capture import capture_pending_secret
from maurice.host.self_update import (
    apply_runtime_proposal, list_runtime_proposals, run_proposal_tests, validate_runtime_proposal,
)
from maurice.host.service import check_install, inspect_service_status, read_service_logs
from maurice.host.telegram import (
    _credential_value, _telegram_channel_configured, _telegram_channel_configs,
    _telegram_channel_for_agent, _telegram_offset_path, _validate_telegram_first_message,
    _telegram_get_updates, _telegram_bot_username, _telegram_send_message,
    _telegram_send_chat_action, _telegram_api_json, _telegram_update_to_inbound,
    _int_list, _read_int_file, _write_int_file, _redact_secret,
    _telegram_sender_ids, _telegram_start_chat_action,
)
from maurice.host.workspace import ensure_workspace_content_migrated, initialize_workspace
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import ConfigBundle, load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider, ChatGPTCodexProvider, MockProvider,
    OllamaCompatibleProvider, OpenAICompatibleProvider, UnsupportedProvider,
)
from maurice.kernel.runs import RunApprovalStore, RunCoordinationStore, RunExecutor, RunStore
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.system_skills.dev.planner import PLAN_WIZARD_FILE, clear_plan_wizard, handle_plan_wizard
from maurice.system_skills.reminders.tools import fire_reminder

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
) -> None:
    server = _build_gateway_http_server(workspace_root, agent_id=agent_id, host=None, port=None)
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
) -> GatewayHttpServer:
    router, agent, bundle = _gateway_router_for(workspace_root, agent_id)
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
    return GatewayHttpServer(
        host=host or bundle.host.gateway.host,
        port=port or bundle.host.gateway.port,
        router=router,
        channels=channels,
        event_store=EventStore(event_stream),
        web_agents=lambda: _gateway_web_agents(workspace),
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



def _gateway_web_session_history(workspace: Path, agent_id: str, session_id: str) -> list[dict[str, Any]]:
    store = SessionStore(workspace / "sessions")
    try:
        session = store.load(agent_id, session_id)
    except FileNotFoundError:
        return []
    messages = []
    for message in session.messages:
        if (
            message.metadata.get("internal") is True
            or message.metadata.get("autonomy_internal") is True
            or _looks_like_internal_gateway_message(message.content)
        ):
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
    return messages



def _looks_like_internal_gateway_message(content: str) -> bool:
    normalized = str(content).lstrip()
    return normalized.startswith("Commande interne `/dev`") or normalized.startswith("Continue le mode dev")



def _gateway_web_session_reset(workspace: Path, agent_id: str, session_id: str) -> bool:
    store = SessionStore(workspace / "sessions")
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
        session_id = f"{inbound['channel']}:{inbound['peer_id']}"
        captured = capture_pending_secret(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
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
        _telegram_send_message(token, chat_id, result.outbound.text)
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



def _gateway_router_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )

    def run_gateway_turn(**kwargs):
        return run_one_turn(
            workspace_root=workspace,
            message=kwargs["message"],
            session_id=kwargs["session_id"],
            agent_id=kwargs["agent_id"],
            source_channel=kwargs.get("source_channel"),
            source_peer_id=kwargs.get("source_peer_id"),
            source_metadata=kwargs.get("source_metadata"),
            limits=kwargs.get("limits"),
            message_metadata=kwargs.get("message_metadata"),
        )

    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    skill_registry = SkillLoader(
        bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills or None,
        available_credentials=credentials.credentials.keys(),
        event_store=EventStore(event_stream),
        agent_id=agent.id,
        session_id="gateway",
    ).load()
    command_registry = CommandRegistry.from_skill_registry(skill_registry)
    host_commands_enabled = "host" in (agent.skills or bundle.kernel.skills)
    dev_commands_enabled = "dev" in (agent.skills or bundle.kernel.skills)

    def intercept_gateway_message(message, target_agent_id, session_id, correlation_id):
        if dev_commands_enabled:
            response = handle_plan_wizard(
                store_path=_dev_plan_wizard_store_path(workspace, target_agent_id),
                agent_id=target_agent_id,
                session_id=session_id,
                text=message.text,
            )
            if response is not None:
                return response
        if host_commands_enabled:
            return handle_agent_creation_wizard(
                workspace,
                agent_id=target_agent_id,
                session_id=session_id,
                text=message.text,
            )
        return None

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
            command_callbacks={
                "workspace": workspace,
                "agent_workspace": Path(agent.workspace),
                "agent_workspace_for": lambda target_agent_id: Path(
                    load_workspace_config(workspace).agents.agents[target_agent_id].workspace
                ),
                "compact_session": lambda target_agent_id, session_id: _compact_gateway_session(
                    workspace,
                    target_agent_id,
                    session_id,
                ),
                "model_summary": lambda target_agent_id: _gateway_model_summary(
                    workspace,
                    target_agent_id,
                ),
            },
        ),
        agent,
        bundle,
    )



def _reset_gateway_session(workspace: Path, agent_id: str, session_id: str) -> None:
    store = SessionStore(workspace / "sessions")
    try:
        store.reset(agent_id, session_id)
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)
    clear_agent_creation_wizard(workspace, agent_id=agent_id, session_id=session_id)
    clear_plan_wizard(
        store_path=_dev_plan_wizard_store_path(workspace, agent_id),
        agent_id=agent_id,
        session_id=session_id,
    )



def _record_gateway_exchange(
    workspace: Path,
    inbound,
    outbound,
    *,
    include_user: bool,
) -> None:
    store = SessionStore(workspace / "sessions")
    try:
        store.load(outbound.agent_id, outbound.session_id)
    except FileNotFoundError:
        store.create(outbound.agent_id, session_id=outbound.session_id)
    if include_user:
        store.append_message(
            outbound.agent_id,
            outbound.session_id,
            role="user",
            content=inbound.text,
            correlation_id=outbound.correlation_id,
            metadata={"channel": inbound.channel, "peer_id": inbound.peer_id},
        )
    if outbound.text:
        store.append_message(
            outbound.agent_id,
            outbound.session_id,
            role="assistant",
            content=outbound.text,
            correlation_id=outbound.correlation_id,
            metadata=outbound.metadata,
        )



def _compact_gateway_session(workspace: Path, agent_id: str, session_id: str) -> str:
    store = SessionStore(workspace / "sessions")
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
    clear_agent_creation_wizard(workspace, agent_id=agent_id, session_id=session_id)
    clear_plan_wizard(
        store_path=_dev_plan_wizard_store_path(workspace, agent_id),
        agent_id=agent_id,
        session_id=session_id,
    )
    return "Session compactee. J'ai garde un resume court dans le contexte."



def _dev_plan_wizard_store_path(workspace: Path, agent_id: str) -> Path:
    try:
        agent = load_workspace_config(workspace).agents.agents[agent_id]
        return Path(agent.workspace) / PLAN_WIZARD_FILE
    except KeyError:
        return workspace / "agents" / agent_id / PLAN_WIZARD_FILE



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
        f"Pour le changer : `maurice onboard --agent {agent.id} --model`."
    )

