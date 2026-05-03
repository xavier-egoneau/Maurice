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
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.system_skills.reminders.tools import fire_reminder

def _agents_list(workspace_root: Path) -> None:
    agents = list_agents(workspace_root)
    if not agents:
        print("No agents.")
        return
    for agent in agents:
        marker = "default" if agent.default else "-"
        print(
            f"{agent.id} {marker} status={agent.status} profile={agent.permission_profile} "
            f"skills={','.join(agent.skills)} credentials={','.join(agent.credentials)} "
            f"channels={','.join(agent.channels)}"
        )



def _agents_create(
    workspace_root: Path,
    *,
    agent_id: str,
    permission_profile: str | None,
    skills: list[str] | None,
    credentials: list[str] | None,
    channels: list[str] | None,
    make_default: bool,
    confirmed_permission_elevation: bool,
) -> None:
    try:
        agent = create_agent(
            workspace_root,
            agent_id=agent_id,
            permission_profile=permission_profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=make_default,
            confirmed_permission_elevation=confirmed_permission_elevation,
        )
    except (PermissionError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent created: {agent.id} ({agent.permission_profile})")



def _agents_update(
    workspace_root: Path,
    *,
    agent_id: str,
    permission_profile: str | None,
    skills: list[str] | None,
    credentials: list[str] | None,
    channels: list[str] | None,
    make_default: bool,
    confirmed_permission_elevation: bool,
) -> None:
    try:
        agent = update_agent(
            workspace_root,
            agent_id=agent_id,
            permission_profile=permission_profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=True if make_default else None,
            confirmed_permission_elevation=confirmed_permission_elevation,
        )
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent updated: {agent.id} ({agent.permission_profile})")



def _agents_disable(workspace_root: Path, *, agent_id: str) -> None:
    try:
        agent = disable_agent(workspace_root, agent_id=agent_id)
    except (KeyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent disabled: {agent.id}")



def _agents_archive(workspace_root: Path, *, agent_id: str) -> None:
    try:
        agent = archive_agent(workspace_root, agent_id=agent_id)
    except (KeyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent archived: {agent.id}")



def _agents_delete(workspace_root: Path, *, agent_id: str, confirmed: bool) -> None:
    try:
        agent = delete_agent(workspace_root, agent_id=agent_id, confirmed=confirmed)
    except (KeyError, PermissionError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent deleted: {agent.id}")

