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
from maurice.host.errors import AgentError
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

def _approvals_list(
    workspace_root: Path,
    *,
    agent_id: str | None = None,
    status: str | None = None,
) -> None:
    store, _agent = _approval_store_for(workspace_root, agent_id)
    approvals = store.list(status=status)
    if not approvals:
        print("No approvals.")
        return
    for approval in approvals:
        print(
            f"{approval.id} {approval.status} {approval.tool_name} "
            f"{approval.permission_class} {approval.summary}"
        )



def _approvals_resolve(
    workspace_root: Path,
    approval_id: str,
    *,
    agent_id: str | None = None,
    action: str,
) -> None:
    store, _agent = _approval_store_for(workspace_root, agent_id)
    try:
        if action == "approve":
            approval = store.approve(approval_id)
        elif action == "deny":
            approval = store.deny(approval_id)
        else:
            raise ValueError(action)
    except KeyError as exc:
        raise SystemExit(f"Unknown approval: {approval_id}") from exc
    print(f"{approval.id} {approval.status} {approval.tool_name}")



def _approval_store_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    try:
        agent = _resolve_agent(bundle, agent_id)
    except AgentError as exc:
        raise SystemExit(str(exc)) from exc
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    event_store = EventStore(event_stream)
    return (
        ApprovalStore(
            workspace / "agents" / agent.id / "approvals.json",
            event_store=event_store,
        ),
        agent,
    )

