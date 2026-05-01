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
from maurice.system_skills.reminders.tools import fire_reminder

def _migration_inspect(jarvis_root: Path, *, as_json: bool) -> None:
    report = inspect_jarvis_workspace(jarvis_root)
    _print_migration_report(report, as_json=as_json)



def _migration_run(
    jarvis_root: Path,
    workspace_root: Path,
    *,
    dry_run: bool,
    include_artifacts: bool,
    as_json: bool,
) -> None:
    report = migrate_jarvis_workspace(
        jarvis_root,
        workspace_root,
        dry_run=dry_run,
        include_artifacts=include_artifacts,
    )
    _print_migration_report(report, as_json=as_json)



def _print_migration_report(report, *, as_json: bool) -> None:
    if as_json:
        print(report.model_dump_json(indent=2))
        return
    print(f"Jarvis migration report: {report.jarvis_root}")
    if report.workspace_root:
        print(f"Workspace: {report.workspace_root}")
    print(f"Mode: {'dry-run' if report.dry_run else 'apply'}")
    print(f"Items: {len(report.items)} migrated={report.migrated_count}")
    for item in report.items:
        destination = f" -> {item.destination}" if item.destination else ""
        reason = f" ({item.reason})" if item.reason else ""
        print(f"- {item.kind}: {item.status} {item.source}{destination}{reason}")
    for warning in report.warnings:
        print(f"warning: {warning}")



def _self_update_list(workspace_root: Path) -> None:
    proposals = list_runtime_proposals(workspace_root)
    if not proposals:
        print("No runtime proposals.")
        return
    for proposal in proposals:
        print(f"{proposal.id} {proposal.status} risk={proposal.risk} {proposal.summary}")



def _self_update_validate(workspace_root: Path, proposal_id: str) -> None:
    report = validate_runtime_proposal(workspace_root, proposal_id)
    print(f"Proposal validation: {'ok' if report.ok else 'failed'}")
    for check in report.checks:
        print(f"- {check.name}: {'ok' if check.ok else 'failed'} - {check.summary}")
    if not report.ok:
        raise SystemExit("Proposal validation failed")



def _self_update_test(workspace_root: Path, proposal_id: str) -> None:
    report = run_proposal_tests(workspace_root, proposal_id)
    print(f"Proposal tests: {'ok' if report.ok else 'failed'}")
    if not report.commands:
        print("No test commands.")
    for command in report.commands:
        print(f"- {command['returncode']} {command['command']}")
    if not report.ok:
        raise SystemExit("Proposal tests failed")



def _self_update_apply(workspace_root: Path, proposal_id: str, *, confirmed: bool, run_tests: bool) -> None:
    try:
        report = apply_runtime_proposal(
            workspace_root,
            proposal_id,
            confirmed=confirmed,
            run_tests=run_tests,
        )
    except PermissionError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Proposal apply: {report.status}")
    if report.report_path:
        print(f"Report: {report.report_path}")
    if report.rollback_path:
        print(f"Rollback: {report.rollback_path}")
    for error in report.errors:
        print(f"error: {error}")
    if not report.applied:
        raise SystemExit("Proposal apply failed")



def _monitor_snapshot(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    as_json: bool,
) -> None:
    try:
        snapshot = build_monitoring_snapshot(
            workspace_root,
            agent_id=agent_id,
            event_limit=event_limit,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if as_json:
        print(snapshot.model_dump_json(indent=2))
        return
    print(f"Runtime: {snapshot.runtime.workspace_root}")
    print(f"Agents: {len(snapshot.agents)}")
    print(f"Skills: {len(snapshot.skills)}")
    print(f"Approvals: {snapshot.approvals.total} {snapshot.approvals.by_status}")
    print(f"Jobs: {snapshot.jobs.total} {snapshot.jobs.by_status}")
    print(f"Runs: {snapshot.runs.total} {snapshot.runs.by_status}")
    print(f"Events: {len(snapshot.events)}")



def _monitor_events(workspace_root: Path, *, agent_id: str | None, limit: int) -> None:
    try:
        events = read_event_tail(workspace_root, agent_id=agent_id, limit=limit)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not events:
        print("No events.")
        return
    for event in events:
        print(f"{event.time.isoformat()} {event.agent_id} {event.session_id} {event.name} {event.id}")

