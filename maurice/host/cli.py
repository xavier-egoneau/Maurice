"""Command line entrypoint for Maurice."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agents import (
    archive_agent,
    create_agent,
    delete_agent,
    disable_agent,
    list_agents,
    update_agent,
)
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME,
    ChatGPTAuthFlow,
    clear_chatgpt_auth,
    get_valid_chatgpt_access_token,
    load_chatgpt_auth,
    save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.credentials import (
    CredentialRecord,
    CredentialsStore,
    credentials_path,
    ensure_workspace_credentials_migrated,
    load_workspace_credentials,
    write_workspace_credentials,
)
from maurice.host.dashboard import build_dashboard_snapshot
from maurice.host.gateway import GatewayHttpServer, MessageRouter
from maurice.host.migration import inspect_jarvis_workspace, migrate_jarvis_workspace
from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.paths import (
    agents_config_path,
    ensure_workspace_config_migrated,
    host_config_path,
    kernel_config_path,
    maurice_home,
    workspace_skills_config_path,
)
from maurice.host.secret_capture import capture_pending_secret
from maurice.host.self_update import (
    apply_runtime_proposal,
    list_runtime_proposals,
    run_proposal_tests,
    validate_runtime_proposal,
)
from maurice.host.service import check_install, inspect_service_status, read_service_logs
from maurice.host.workspace import ensure_workspace_content_migrated, initialize_workspace
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.config import ConfigBundle, load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider,
    ChatGPTCodexProvider,
    MockProvider,
    OllamaCompatibleProvider,
    OpenAICompatibleProvider,
    UnsupportedProvider,
)
from maurice.kernel.runs import RunApprovalStore, RunCoordinationStore, RunExecutor, RunStore
from maurice.kernel.scheduler import JobRunner, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillLoader
from maurice.system_skills.dreaming.tools import dreaming_tool_executors
from maurice.system_skills.filesystem.tools import filesystem_tool_executors
from maurice.system_skills.host.tools import host_tool_executors
from maurice.system_skills.memory.tools import build_dream_input, memory_tool_executors
from maurice.system_skills.reminders.tools import fire_reminder, reminders_tool_executors
from maurice.system_skills.self_update.tools import self_update_tool_executors
from maurice.system_skills.skills.tools import skill_authoring_tool_executors
from maurice.system_skills.vision.tools import vision_tool_executors
from maurice.system_skills.web.tools import web_tool_executors


PROVIDER_HELP = {
    "chatgpt": ("ChatGPT", "connexion via ton abonnement ChatGPT, sans cle API OpenAI"),
    "openai_api": ("API compatible OpenAI", "URL + cle API, pour OpenAI ou un provider compatible"),
    "ollama": ("Ollama", "modele local ou serveur Ollama"),
}
DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_WORKSPACE = Path.home() / "Documents" / "workspace_maurice"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maurice",
        description="Maurice agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")
    doctor_parser = subparsers.add_parser("doctor", help="Check the local Maurice installation.")
    doctor_parser.add_argument(
        "--workspace",
        help="Workspace root to inspect. If omitted, only package import is checked.",
    )
    install_parser = subparsers.add_parser("install", help="Check local host prerequisites.")
    install_parser.add_argument(
        "--workspace",
        help="Optional workspace root to include in install checks.",
    )
    onboard_parser = subparsers.add_parser("onboard", help="Initialize a Maurice workspace.")
    onboard_parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help=f"Workspace root to create or update. Defaults to {DEFAULT_WORKSPACE}.",
    )
    onboard_parser.add_argument(
        "--agent",
        nargs="?",
        const="",
        help="Onboard a new durable agent. If omitted after --agent, the wizard asks for the id.",
    )
    onboard_parser.add_argument(
        "--model",
        action="store_true",
        help="With --agent, update only this agent's model configuration.",
    )
    onboard_parser.add_argument(
        "--permission-profile",
        choices=["safe", "limited", "power"],
        default="safe",
        help="Default permission profile for the main agent.",
    )
    onboard_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask for model, gateway, and provider basics after workspace creation.",
    )
    start_parser = subparsers.add_parser("start", help="Start Maurice background services.")
    start_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    start_parser.add_argument("--agent", help="Agent id to use. Defaults to configured agents.")
    start_parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval.")
    start_parser.add_argument("--no-telegram", action="store_true", help="Do not start Telegram polling.")
    start_parser.add_argument("--no-scheduler", action="store_true", help="Do not start the scheduler.")
    start_parser.add_argument("--foreground", action="store_true", help="Keep services attached to this terminal.")
    stop_parser = subparsers.add_parser("stop", help="Stop a running Maurice instance.")
    stop_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    logs_parser = subparsers.add_parser("logs", help="Show recent Maurice event logs.")
    logs_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to inspect.")
    logs_parser.add_argument("--agent", help="Agent id to inspect. Defaults to the configured default agent.")
    logs_parser.add_argument("--limit", type=int, default=20, help="Maximum events to show.")
    dashboard_parser = subparsers.add_parser("dashboard", help="Open the local dashboard.")
    dashboard_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to inspect.")
    dashboard_parser.add_argument("--agent", help="Agent id to inspect. Defaults to the configured default agent.")
    dashboard_parser.add_argument("--event-limit", type=int, default=8, help="Recent events to show.")
    dashboard_parser.add_argument("--watch", action="store_true", help="Refresh the dashboard until interrupted.")
    dashboard_parser.add_argument("--plain", action="store_true", help="Use the non-interactive text dashboard.")
    dashboard_parser.add_argument("--refresh-seconds", type=float, default=1.0, help="Dashboard refresh interval.")

    run_parser = subparsers.add_parser("run", help="Run one Maurice turn.")
    run_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    run_parser.add_argument("--session", default="default", help="Session id to use.")
    run_parser.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    run_parser.add_argument("--message", required=True, help="Message to send to the agent.")

    agents_parser = subparsers.add_parser("agents", help="Manage permanent agents.")
    agents_subparsers = agents_parser.add_subparsers(dest="agents_command")
    agents_list = agents_subparsers.add_parser("list", help="List permanent agents.")
    agents_list.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_create = agents_subparsers.add_parser("create", help="Create a permanent agent.")
    agents_create.add_argument("agent_id", help="Agent id to create.")
    agents_create.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_create.add_argument(
        "--permission-profile",
        choices=["safe", "limited", "power"],
        help="Permission profile for the agent. Defaults to the global profile.",
    )
    agents_create.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="Enabled skill for the agent. May be provided more than once.",
    )
    agents_create.add_argument(
        "--credential",
        action="append",
        dest="credentials",
        help="Credential name this agent may use. May be provided more than once.",
    )
    agents_create.add_argument(
        "--channel",
        action="append",
        dest="channels",
        help="Channel binding for the agent. May be provided more than once.",
    )
    agents_create.add_argument("--default", action="store_true", help="Make this the default agent.")
    agents_create.add_argument(
        "--confirm-permission-elevation",
        action="store_true",
        help="Confirm an agent profile more permissive than the global profile.",
    )
    agents_update = agents_subparsers.add_parser("update", help="Update a permanent agent.")
    agents_update.add_argument("agent_id", help="Agent id to update.")
    agents_update.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_update.add_argument(
        "--permission-profile",
        choices=["safe", "limited", "power"],
        help="New permission profile for the agent.",
    )
    agents_update.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="Replace enabled skills. May be provided more than once.",
    )
    agents_update.add_argument(
        "--credential",
        action="append",
        dest="credentials",
        help="Replace allowed credentials. May be provided more than once.",
    )
    agents_update.add_argument(
        "--channel",
        action="append",
        dest="channels",
        help="Replace channel bindings. May be provided more than once.",
    )
    agents_update.add_argument("--default", action="store_true", help="Make this the default agent.")
    agents_update.add_argument(
        "--confirm-permission-elevation",
        action="store_true",
        help="Confirm an agent profile more permissive than the global profile.",
    )
    agents_disable = agents_subparsers.add_parser("disable", help="Disable a permanent agent.")
    agents_disable.add_argument("agent_id", help="Agent id to disable.")
    agents_disable.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_archive = agents_subparsers.add_parser("archive", help="Archive a permanent agent.")
    agents_archive.add_argument("agent_id", help="Agent id to archive.")
    agents_archive.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_delete = agents_subparsers.add_parser("delete", help="Delete a permanent agent.")
    agents_delete.add_argument("agent_id", help="Agent id to delete.")
    agents_delete.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    agents_delete.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm destructive deletion of the agent config and workspace.",
    )

    runs_parser = subparsers.add_parser("runs", help="Manage disposable subagent runs.")
    runs_subparsers = runs_parser.add_subparsers(dest="runs_command")
    runs_list = runs_subparsers.add_parser("list", help="List subagent runs.")
    runs_list.add_argument("--workspace", required=True, help="Workspace root to use.")
    runs_list.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_list.add_argument("--state", choices=["created", "running", "checkpointing", "paused", "completed", "failed", "cancelled"])
    runs_create = runs_subparsers.add_parser("create", help="Create a subagent run.")
    runs_create.add_argument("--workspace", required=True, help="Workspace root to use.")
    runs_create.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_create.add_argument("--task", required=True, help="Task assigned to the run.")
    runs_create.add_argument("--base-agent", help="Base permanent agent/profile for the run.")
    runs_create.add_argument("--inline-profile", help="Inline JSON profile for this temporary run.")
    runs_create.add_argument("--context-summary", default="", help="Short standalone context summary.")
    runs_create.add_argument("--relevant-file", action="append", dest="relevant_files", help="Relevant file path. May repeat.")
    runs_create.add_argument("--constraint", action="append", dest="constraints", help="Run constraint. May repeat.")
    runs_create.add_argument("--plan-step", action="append", dest="plan_steps", help="Planned step. May repeat.")
    runs_create.add_argument("--requires-self-check", action="store_true", help="Require self-check evidence in the final output.")
    runs_create.add_argument("--can-request-install", action="store_true", help="Allow the run to request dependency installation through parent approval.")
    runs_create.add_argument("--package-manager", action="append", dest="package_managers", help="Allowed package manager for install requests. May repeat.")
    runs_create.add_argument("--autonomy-mode", default="continue_until_blocked", help="Run autonomy mode.")
    runs_create.add_argument("--max-steps", type=int, default=20, help="Maximum autonomous steps before stopping.")
    runs_create.add_argument("--checkpoint-every-steps", type=int, default=5, help="Checkpoint interval for autonomous runs.")
    runs_create.add_argument("--stop-condition", action="append", dest="stop_conditions", help="Autonomous stop condition. May repeat.")
    runs_create.add_argument(
        "--context-inheritance",
        choices=["none", "current_task", "linked_session"],
        default="current_task",
    )
    runs_create.add_argument("--write-path", action="append", dest="write_paths", help="Allowed write path. May repeat.")
    runs_create.add_argument("--permission-class", action="append", dest="permission_classes", help="Allowed permission class. May repeat.")
    runs_start = runs_subparsers.add_parser("start", help="Mark a run as running.")
    runs_start.add_argument("run_id")
    runs_start.add_argument("--workspace", required=True)
    runs_start.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_resume = runs_subparsers.add_parser("resume", help="Resume a paused or cancelled run if safe.")
    runs_resume.add_argument("run_id")
    runs_resume.add_argument("--workspace", required=True)
    runs_resume.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_execute = runs_subparsers.add_parser("execute", help="Execute a run until blocked by its autonomy policy.")
    runs_execute.add_argument("run_id")
    runs_execute.add_argument("--workspace", required=True)
    runs_execute.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_execute.add_argument("--prepare-only", action="store_true", help="Only prepare the run session and checkpoint.")
    runs_checkpoint = runs_subparsers.add_parser("checkpoint", help="Pause a run with a checkpoint.")
    runs_checkpoint.add_argument("run_id")
    runs_checkpoint.add_argument("--workspace", required=True)
    runs_checkpoint.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_checkpoint.add_argument("--summary", required=True)
    runs_checkpoint.add_argument("--unsafe-to-resume", action="store_true")
    runs_complete = runs_subparsers.add_parser("complete", help="Complete a run.")
    runs_complete.add_argument("run_id")
    runs_complete.add_argument("--workspace", required=True)
    runs_complete.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_complete.add_argument("--summary", required=True)
    runs_complete.add_argument("--changed-file", action="append", dest="changed_files", help="Changed file path. May repeat.")
    runs_complete.add_argument("--risk", action="append", dest="risks", help="Known residual risk. May repeat.")
    runs_complete.add_argument("--verification-command", action="append", dest="verification_commands", help="Verification command. May repeat.")
    runs_complete.add_argument("--verification-status", action="append", dest="verification_statuses", help="Verification status matching --verification-command. May repeat.")
    runs_complete.add_argument("--verification-summary", action="append", dest="verification_summaries", help="Verification output summary. May repeat.")
    runs_review = runs_subparsers.add_parser("review", help="Record parent review for a completed run.")
    runs_review.add_argument("run_id")
    runs_review.add_argument("--workspace", required=True)
    runs_review.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_review.add_argument("--status", choices=["accepted", "needs_changes"], required=True)
    runs_review.add_argument("--summary", required=True)
    runs_review.add_argument("--followup", action="append", dest="followups")
    runs_fail = runs_subparsers.add_parser("fail", help="Fail a run.")
    runs_fail.add_argument("run_id")
    runs_fail.add_argument("--workspace", required=True)
    runs_fail.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_fail.add_argument("--summary", required=True)
    runs_fail.add_argument("--error", action="append", dest="errors", required=True)
    runs_cancel = runs_subparsers.add_parser("cancel", help="Cancel a run with a checkpoint.")
    runs_cancel.add_argument("run_id")
    runs_cancel.add_argument("--workspace", required=True)
    runs_cancel.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_cancel.add_argument("--summary", default="Run cancellation requested.")
    runs_cancel.add_argument("--unsafe-to-resume", action="store_true")
    runs_coordinate = runs_subparsers.add_parser("coordinate", help="Request parent-owned coordination between runs.")
    runs_coordinate.add_argument("run_id", help="Source run id.")
    runs_coordinate.add_argument("--workspace", required=True)
    runs_coordinate.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_coordinate.add_argument("--affects", action="append", dest="affected_run_ids", required=True, help="Affected run id. May repeat.")
    runs_coordinate.add_argument("--impact", required=True, help="Impact summary.")
    runs_coordinate.add_argument("--requested-action", required=True, help="Action requested from the parent.")
    runs_coordination_list = runs_subparsers.add_parser("coordination-list", help="List run coordination events.")
    runs_coordination_list.add_argument("--workspace", required=True)
    runs_coordination_list.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_coordination_list.add_argument("--status", choices=["pending", "acknowledged", "resolved"])
    runs_coordination_ack = runs_subparsers.add_parser("coordination-ack", help="Acknowledge a coordination event.")
    runs_coordination_ack.add_argument("coordination_id")
    runs_coordination_ack.add_argument("--workspace", required=True)
    runs_coordination_ack.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_coordination_resolve = runs_subparsers.add_parser("coordination-resolve", help="Resolve a coordination event.")
    runs_coordination_resolve.add_argument("coordination_id")
    runs_coordination_resolve.add_argument("--workspace", required=True)
    runs_coordination_resolve.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_request_approval = runs_subparsers.add_parser("request-approval", help="Request parent approval for a blocked run.")
    runs_request_approval.add_argument("run_id")
    runs_request_approval.add_argument("--workspace", required=True)
    runs_request_approval.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_request_approval.add_argument("--type", choices=["permission", "dependency"], required=True)
    runs_request_approval.add_argument("--reason", required=True)
    runs_request_approval.add_argument("--scope", action="append", dest="scope_items", help="Requested scope key=value. May repeat.")
    runs_request_approval.add_argument("--unsafe-to-resume", action="store_true")
    runs_approvals_list = runs_subparsers.add_parser("approvals-list", help="List run approval requests.")
    runs_approvals_list.add_argument("--workspace", required=True)
    runs_approvals_list.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_approvals_list.add_argument("--status", choices=["pending", "approved", "denied"])
    runs_approvals_approve = runs_subparsers.add_parser("approvals-approve", help="Approve a run approval request.")
    runs_approvals_approve.add_argument("approval_id")
    runs_approvals_approve.add_argument("--workspace", required=True)
    runs_approvals_approve.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")
    runs_approvals_deny = runs_subparsers.add_parser("approvals-deny", help="Deny a run approval request.")
    runs_approvals_deny.add_argument("approval_id")
    runs_approvals_deny.add_argument("--workspace", required=True)
    runs_approvals_deny.add_argument("--agent", help="Parent agent id. Defaults to the configured default agent.")

    auth_parser = subparsers.add_parser("auth", help="Manage login/session provider credentials.")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command")
    auth_login = auth_subparsers.add_parser("login", help="Login to an auth provider.")
    auth_login.add_argument("provider", choices=["chatgpt"], help="Auth provider to connect.")
    auth_login.add_argument("--workspace", required=True, help="Workspace root to use.")
    auth_status = auth_subparsers.add_parser("status", help="Show auth provider status.")
    auth_status.add_argument("provider", choices=["chatgpt"], help="Auth provider to inspect.")
    auth_status.add_argument("--workspace", required=True, help="Workspace root to use.")
    auth_logout = auth_subparsers.add_parser("logout", help="Remove auth provider credentials.")
    auth_logout.add_argument("provider", choices=["chatgpt"], help="Auth provider to clear.")
    auth_logout.add_argument("--workspace", required=True, help="Workspace root to use.")

    approvals_parser = subparsers.add_parser("approvals", help="Inspect and resolve pending approvals.")
    approvals_subparsers = approvals_parser.add_subparsers(dest="approvals_command")
    approvals_list = approvals_subparsers.add_parser("list", help="List approvals.")
    approvals_list.add_argument("--workspace", required=True, help="Workspace root to use.")
    approvals_list.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    approvals_list.add_argument(
        "--status",
        choices=["pending", "approved", "denied", "expired"],
        help="Only show approvals with this status.",
    )
    approvals_approve = approvals_subparsers.add_parser("approve", help="Approve an approval.")
    approvals_approve.add_argument("approval_id", help="Approval id to approve.")
    approvals_approve.add_argument("--workspace", required=True, help="Workspace root to use.")
    approvals_approve.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    approvals_deny = approvals_subparsers.add_parser("deny", help="Deny an approval.")
    approvals_deny.add_argument("approval_id", help="Approval id to deny.")
    approvals_deny.add_argument("--workspace", required=True, help="Workspace root to use.")
    approvals_deny.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")

    scheduler_parser = subparsers.add_parser("scheduler", help="Schedule and run background jobs.")
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command")
    scheduler_dream = scheduler_subparsers.add_parser("schedule-dream", help="Schedule a dreaming.run job.")
    scheduler_dream.add_argument("--workspace", required=True, help="Workspace root to use.")
    scheduler_dream.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    scheduler_dream.add_argument("--delay-seconds", type=int, default=0, help="Delay before first run.")
    scheduler_dream.add_argument("--interval-seconds", type=int, help="Reschedule interval for recurring dreams.")
    scheduler_dream.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="Skill to include. May be provided more than once.",
    )
    scheduler_run_once = scheduler_subparsers.add_parser("run-once", help="Run due scheduler jobs once.")
    scheduler_run_once.add_argument("--workspace", required=True, help="Workspace root to use.")
    scheduler_run_once.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    scheduler_run_once.add_argument("--limit", type=int, help="Maximum jobs to run.")
    scheduler_serve = scheduler_subparsers.add_parser("serve", help="Run the scheduler service.")
    scheduler_serve.add_argument("--workspace", required=True, help="Workspace root to use.")
    scheduler_serve.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    scheduler_serve.add_argument("--poll-seconds", type=float, default=5.0, help="Polling interval.")

    gateway_parser = subparsers.add_parser("gateway", help="Route channel-neutral messages.")
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command")
    gateway_local = gateway_subparsers.add_parser("local-message", help="Route one local gateway message.")
    gateway_local.add_argument("--workspace", required=True, help="Workspace root to use.")
    gateway_local.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    gateway_local.add_argument("--session", help="Session id to use.")
    gateway_local.add_argument("--peer", default="local", help="Peer id to route from.")
    gateway_local.add_argument("--message", required=True, help="Message text to route.")
    gateway_serve = gateway_subparsers.add_parser("serve", help="Run the local HTTP gateway.")
    gateway_serve.add_argument("--workspace", required=True, help="Workspace root to use.")
    gateway_serve.add_argument("--agent", help="Agent id to use. Defaults to the configured default agent.")
    gateway_serve.add_argument("--host", help="Host to bind. Defaults to host config.")
    gateway_serve.add_argument("--port", type=int, help="Port to bind. Defaults to host config.")
    gateway_telegram = gateway_subparsers.add_parser("telegram-poll", help="Poll Telegram and route messages.")
    gateway_telegram.add_argument("--workspace", required=True, help="Workspace root to use.")
    gateway_telegram.add_argument("--agent", help="Agent id to use. Defaults to the configured Telegram agent.")
    gateway_telegram.add_argument("--once", action="store_true", help="Poll once and exit.")
    gateway_telegram.add_argument("--poll-seconds", type=float, default=2.0, help="Delay between polls.")

    service_parser = subparsers.add_parser("service", help="Inspect local Maurice service hooks.")
    service_subparsers = service_parser.add_subparsers(dest="service_command")
    service_status = service_subparsers.add_parser("status", help="Show local service status.")
    service_status.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    service_logs = service_subparsers.add_parser("logs", help="Show recent agent event logs.")
    service_logs.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    service_logs.add_argument("--agent", help="Agent id to inspect. Defaults to the configured default agent.")
    service_logs.add_argument("--limit", type=int, default=20, help="Maximum events to show.")

    monitor_parser = subparsers.add_parser("monitor", help="Read generic monitoring snapshots.")
    monitor_subparsers = monitor_parser.add_subparsers(dest="monitor_command")
    monitor_snapshot = monitor_subparsers.add_parser("snapshot", help="Show runtime monitoring snapshot.")
    monitor_snapshot.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    monitor_snapshot.add_argument("--agent", help="Agent id to inspect. Defaults to the configured default agent.")
    monitor_snapshot.add_argument("--event-limit", type=int, default=20, help="Recent events to include.")
    monitor_snapshot.add_argument("--json", action="store_true", help="Print the full snapshot as JSON.")
    monitor_events = monitor_subparsers.add_parser("events", help="Show recent runtime events.")
    monitor_events.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    monitor_events.add_argument("--agent", help="Agent id to inspect. Defaults to the configured default agent.")
    monitor_events.add_argument("--limit", type=int, default=20, help="Maximum events to show.")

    migration_parser = subparsers.add_parser("migration", help="Inspect and migrate Jarvis-owned data.")
    migration_subparsers = migration_parser.add_subparsers(dest="migration_command")
    migration_inspect = migration_subparsers.add_parser("inspect", help="Inspect a Jarvis workspace.")
    migration_inspect.add_argument("--jarvis", required=True, help="Jarvis workspace root to inspect.")
    migration_inspect.add_argument("--json", action="store_true", help="Print full report as JSON.")
    migration_run = migration_subparsers.add_parser("run", help="Migrate compatible Jarvis data.")
    migration_run.add_argument("--jarvis", required=True, help="Jarvis workspace root to migrate from.")
    migration_run.add_argument("--workspace", required=True, help="Maurice workspace root to migrate into.")
    migration_run.add_argument("--dry-run", action="store_true", help="Report planned migration without writing.")
    migration_run.add_argument(
        "--include-content",
        "--include-artifacts",
        action="store_true",
        dest="include_artifacts",
        help="Copy selected Jarvis content.",
    )
    migration_run.add_argument("--json", action="store_true", help="Print full report as JSON.")

    self_update_parser = subparsers.add_parser("self-update", help="Host-owned runtime proposal apply flow.")
    self_update_subparsers = self_update_parser.add_subparsers(dest="self_update_command")
    self_update_list = self_update_subparsers.add_parser("list", help="List runtime proposals.")
    self_update_list.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    self_update_validate = self_update_subparsers.add_parser("validate", help="Validate a runtime proposal.")
    self_update_validate.add_argument("proposal_id")
    self_update_validate.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    self_update_test = self_update_subparsers.add_parser("test", help="Run proposal test plan commands.")
    self_update_test.add_argument("proposal_id")
    self_update_test.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    self_update_apply = self_update_subparsers.add_parser("apply", help="Apply an approved runtime proposal.")
    self_update_apply.add_argument("proposal_id")
    self_update_apply.add_argument("--workspace", required=True, help="Workspace root to inspect.")
    self_update_apply.add_argument("--confirm-approval", action="store_true", help="Confirm host approval to apply.")
    self_update_apply.add_argument("--skip-tests", action="store_true", help="Skip proposal test plan commands.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        if args.workspace:
            _doctor_workspace(Path(args.workspace))
        else:
            print("Maurice doctor: basic package import OK")
        return 0

    if args.command == "install":
        _install(workspace_root=Path(args.workspace) if args.workspace else None)
        return 0

    if args.command == "onboard":
        runtime_root = Path(__file__).resolve().parents[2]
        workspace_arg = Path(args.workspace)
        if args.agent is not None and not host_config_path(workspace_arg).exists():
            initialize_workspace(
                workspace_arg,
                runtime_root,
                permission_profile=args.permission_profile,
            )
        if args.model and args.agent is None:
            raise SystemExit("Use --model with --agent <id>.")
        if args.agent is not None:
            if args.model:
                _onboard_agent_model(workspace_arg, agent_id=args.agent)
            else:
                _onboard_agent(workspace_arg, agent_id=args.agent)
            return 0
        existing = _onboarding_existing_values(workspace_arg) if args.interactive else {}
        workspace = initialize_workspace(
            workspace_arg,
            runtime_root,
            permission_profile=args.permission_profile,
        )
        if args.interactive:
            _onboard_interactive(workspace, existing=existing)
        else:
            print(f"Maurice workspace initialized: {workspace}")
        return 0

    if args.command == "run":
        result = run_one_turn(
            workspace_root=Path(args.workspace),
            message=args.message,
            session_id=args.session,
            agent_id=args.agent,
        )
        if result.assistant_text:
            print(result.assistant_text)
        for tool_result in result.tool_results:
            print(tool_result.summary)
        if result.status != "completed":
            detail = f": {result.error}" if result.error else ""
            raise SystemExit(f"Turn failed{detail}")
        return 0

    if args.command == "start":
        if args.foreground:
            _start_services(
                Path(args.workspace),
                agent_id=args.agent,
                poll_seconds=args.poll_seconds,
                telegram=not args.no_telegram,
                scheduler=not args.no_scheduler,
            )
        else:
            _start_services_daemon(
                Path(args.workspace),
                agent_id=args.agent,
                poll_seconds=args.poll_seconds,
                telegram=not args.no_telegram,
                scheduler=not args.no_scheduler,
            )
        return 0

    if args.command == "stop":
        _stop_services(Path(args.workspace))
        return 0

    if args.command == "logs":
        _service_logs(Path(args.workspace), agent_id=args.agent, limit=args.limit)
        return 0

    if args.command == "dashboard":
        _dashboard(
            Path(args.workspace),
            agent_id=args.agent,
            event_limit=args.event_limit,
            watch=args.watch,
            refresh_seconds=args.refresh_seconds,
            plain=args.plain,
        )
        return 0

    if args.command == "agents":
        if args.agents_command == "list":
            _agents_list(Path(args.workspace))
            return 0
        if args.agents_command == "create":
            _agents_create(
                Path(args.workspace),
                agent_id=args.agent_id,
                permission_profile=args.permission_profile,
                skills=args.skills,
                credentials=args.credentials,
                channels=args.channels,
                make_default=args.default,
                confirmed_permission_elevation=args.confirm_permission_elevation,
            )
            return 0
        if args.agents_command == "update":
            _agents_update(
                Path(args.workspace),
                agent_id=args.agent_id,
                permission_profile=args.permission_profile,
                skills=args.skills,
                credentials=args.credentials,
                channels=args.channels,
                make_default=args.default,
                confirmed_permission_elevation=args.confirm_permission_elevation,
            )
            return 0
        if args.agents_command == "disable":
            _agents_disable(Path(args.workspace), agent_id=args.agent_id)
            return 0
        if args.agents_command == "archive":
            _agents_archive(Path(args.workspace), agent_id=args.agent_id)
            return 0
        if args.agents_command == "delete":
            _agents_delete(
                Path(args.workspace),
                agent_id=args.agent_id,
                confirmed=args.confirm,
            )
            return 0

    if args.command == "runs":
        if args.runs_command == "list":
            _runs_list(Path(args.workspace), agent_id=args.agent, state=args.state)
            return 0
        if args.runs_command == "create":
            _runs_create(
                Path(args.workspace),
                agent_id=args.agent,
                task=args.task,
                base_agent=args.base_agent,
                inline_profile=args.inline_profile,
                context_summary=args.context_summary,
                context_inheritance=args.context_inheritance,
                relevant_files=args.relevant_files,
                constraints=args.constraints,
                plan_steps=args.plan_steps,
                write_paths=args.write_paths,
                permission_classes=args.permission_classes,
                requires_self_check=args.requires_self_check,
                can_request_install=args.can_request_install,
                package_managers=args.package_managers,
                autonomy_mode=args.autonomy_mode,
                max_steps=args.max_steps,
                checkpoint_every_steps=args.checkpoint_every_steps,
                stop_conditions=args.stop_conditions,
            )
            return 0
        if args.runs_command == "start":
            _runs_start(Path(args.workspace), args.run_id, agent_id=args.agent)
            return 0
        if args.runs_command == "resume":
            _runs_resume(Path(args.workspace), args.run_id, agent_id=args.agent)
            return 0
        if args.runs_command == "execute":
            _runs_execute(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                prepare_only=args.prepare_only,
            )
            return 0
        if args.runs_command == "checkpoint":
            _runs_checkpoint(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                summary=args.summary,
                safe_to_resume=not args.unsafe_to_resume,
            )
            return 0
        if args.runs_command == "complete":
            _runs_complete(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                summary=args.summary,
                changed_files=args.changed_files,
                risks=args.risks,
                verification_commands=args.verification_commands,
                verification_statuses=args.verification_statuses,
                verification_summaries=args.verification_summaries,
            )
            return 0
        if args.runs_command == "review":
            _runs_review(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                status=args.status,
                summary=args.summary,
                followups=args.followups,
            )
            return 0
        if args.runs_command == "fail":
            _runs_fail(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                summary=args.summary,
                errors=args.errors,
            )
            return 0
        if args.runs_command == "cancel":
            _runs_cancel(
                Path(args.workspace),
                args.run_id,
                agent_id=args.agent,
                summary=args.summary,
                safe_to_resume=not args.unsafe_to_resume,
            )
            return 0
        if args.runs_command == "coordinate":
            _runs_coordinate(
                Path(args.workspace),
                source_run_id=args.run_id,
                agent_id=args.agent,
                affected_run_ids=args.affected_run_ids,
                impact=args.impact,
                requested_action=args.requested_action,
            )
            return 0
        if args.runs_command == "coordination-list":
            _runs_coordination_list(Path(args.workspace), agent_id=args.agent, status=args.status)
            return 0
        if args.runs_command == "coordination-ack":
            _runs_coordination_ack(Path(args.workspace), args.coordination_id, agent_id=args.agent)
            return 0
        if args.runs_command == "coordination-resolve":
            _runs_coordination_resolve(Path(args.workspace), args.coordination_id, agent_id=args.agent)
            return 0
        if args.runs_command == "request-approval":
            _runs_request_approval(
                Path(args.workspace),
                run_id=args.run_id,
                agent_id=args.agent,
                type=args.type,
                reason=args.reason,
                scope_items=args.scope_items,
                safe_to_resume=not args.unsafe_to_resume,
            )
            return 0
        if args.runs_command == "approvals-list":
            _runs_approvals_list(Path(args.workspace), agent_id=args.agent, status=args.status)
            return 0
        if args.runs_command == "approvals-approve":
            _runs_approvals_approve(Path(args.workspace), args.approval_id, agent_id=args.agent)
            return 0
        if args.runs_command == "approvals-deny":
            _runs_approvals_deny(Path(args.workspace), args.approval_id, agent_id=args.agent)
            return 0

    if args.command == "auth":
        if args.auth_command == "login":
            _auth_login(args.provider, Path(args.workspace))
            return 0
        if args.auth_command == "status":
            _auth_status(args.provider, Path(args.workspace))
            return 0
        if args.auth_command == "logout":
            _auth_logout(args.provider, Path(args.workspace))
            return 0

    if args.command == "approvals":
        if args.approvals_command == "list":
            _approvals_list(Path(args.workspace), agent_id=args.agent, status=args.status)
            return 0
        if args.approvals_command == "approve":
            _approvals_resolve(
                Path(args.workspace),
                args.approval_id,
                agent_id=args.agent,
                action="approve",
            )
            return 0
        if args.approvals_command == "deny":
            _approvals_resolve(
                Path(args.workspace),
                args.approval_id,
                agent_id=args.agent,
                action="deny",
            )
            return 0

    if args.command == "scheduler":
        if args.scheduler_command == "schedule-dream":
            _scheduler_schedule_dream(
                Path(args.workspace),
                agent_id=args.agent,
                delay_seconds=args.delay_seconds,
                interval_seconds=args.interval_seconds,
                skills=args.skills,
            )
            return 0
        if args.scheduler_command == "run-once":
            _scheduler_run_once(Path(args.workspace), agent_id=args.agent, limit=args.limit)
            return 0
        if args.scheduler_command == "serve":
            _scheduler_serve(
                Path(args.workspace),
                agent_id=args.agent,
                poll_seconds=args.poll_seconds,
            )
            return 0

    if args.command == "gateway":
        if args.gateway_command == "local-message":
            _gateway_local_message(
                Path(args.workspace),
                message=args.message,
                peer_id=args.peer,
                agent_id=args.agent,
                session_id=args.session,
            )
            return 0
        if args.gateway_command == "serve":
            _gateway_serve(
                Path(args.workspace),
                agent_id=args.agent,
                host=args.host,
                port=args.port,
            )
            return 0
        if args.gateway_command == "telegram-poll":
            _gateway_telegram_poll(
                Path(args.workspace),
                agent_id=args.agent,
                once=args.once,
                poll_seconds=args.poll_seconds,
            )
            return 0

    if args.command == "service":
        if args.service_command == "status":
            _service_status(Path(args.workspace))
            return 0
        if args.service_command == "logs":
            _service_logs(Path(args.workspace), agent_id=args.agent, limit=args.limit)
            return 0

    if args.command == "monitor":
        if args.monitor_command == "snapshot":
            _monitor_snapshot(
                Path(args.workspace),
                agent_id=args.agent,
                event_limit=args.event_limit,
                as_json=args.json,
            )
            return 0
        if args.monitor_command == "events":
            _monitor_events(Path(args.workspace), agent_id=args.agent, limit=args.limit)
            return 0

    if args.command == "migration":
        if args.migration_command == "inspect":
            _migration_inspect(Path(args.jarvis), as_json=args.json)
            return 0
        if args.migration_command == "run":
            _migration_run(
                Path(args.jarvis),
                Path(args.workspace),
                dry_run=args.dry_run,
                include_artifacts=args.include_artifacts,
                as_json=args.json,
            )
            return 0

    if args.command == "self-update":
        if args.self_update_command == "list":
            _self_update_list(Path(args.workspace))
            return 0
        if args.self_update_command == "validate":
            _self_update_validate(Path(args.workspace), args.proposal_id)
            return 0
        if args.self_update_command == "test":
            _self_update_test(Path(args.workspace), args.proposal_id)
            return 0
        if args.self_update_command == "apply":
            _self_update_apply(
                Path(args.workspace),
                args.proposal_id,
                confirmed=args.confirm_approval,
                run_tests=not args.skip_tests,
            )
            return 0

    parser.print_help()
    return 0


def _install(*, workspace_root: Path | None) -> None:
    runtime_root = Path(__file__).resolve().parents[2]
    report = check_install(runtime_root=runtime_root, workspace_root=workspace_root)
    _print_host_checks("Maurice install", report.ok, report.checks)
    if not report.ok:
        raise SystemExit("Maurice install: failed")


def _service_status(workspace_root: Path) -> None:
    report = inspect_service_status(workspace_root)
    _print_host_checks("Maurice service status", report.ok, report.checks)
    if not report.ok:
        raise SystemExit("Maurice service status: failed")


def _service_logs(workspace_root: Path, *, agent_id: str | None, limit: int) -> None:
    try:
        events = read_service_logs(workspace_root, agent_id=agent_id, limit=limit)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not events:
        print("No service logs.")
        return
    for event in events:
        print(f"{event.time.isoformat()} {event.agent_id} {event.session_id} {event.name} {event.id}")


def _dashboard(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    watch: bool = False,
    refresh_seconds: float = 1.0,
    plain: bool = False,
) -> None:
    if not plain and not watch and sys.stdout.isatty():
        try:
            _dashboard_textual(
                workspace_root,
                agent_id=agent_id,
                event_limit=event_limit,
                refresh_seconds=refresh_seconds,
            )
            return
        except ImportError:
            pass
    if not plain and _dashboard_rich(
        workspace_root,
        agent_id=agent_id,
        event_limit=event_limit,
        watch=watch,
        refresh_seconds=refresh_seconds,
    ):
        return
    frame = 0
    try:
        while True:
            if watch:
                print("\033[2J\033[H", end="")
            _render_dashboard(workspace_root, agent_id=agent_id, event_limit=event_limit, frame=frame)
            if not watch:
                return
            frame += 1
            time.sleep(max(refresh_seconds, 0.2))
    except KeyboardInterrupt:
        print("")


def _dashboard_textual(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    refresh_seconds: float,
) -> None:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import DataTable, Static, TabbedContent, TabPane
    from rich.text import Text

    class MauriceHeader(Static):
        def render(self) -> str:
            return _ASCII_MAURICE

    class MauriceStatus(Static):
        def update_snapshot(self, bundle: ConfigBundle, snapshot) -> None:
            gateway = f"{snapshot.runtime.gateway.get('host')}:{snapshot.runtime.gateway.get('port')}"
            telegram = "ok" if _telegram_channel_configured(bundle) else "-"
            provider = _effective_model_label(bundle, _default_agent(bundle))
            scheduler = "ok" if snapshot.runtime.scheduler_enabled else "-"
            self.update(
                f"Service [#E8761A]{gateway}[/]   Automatismes [#E8761A]{scheduler}[/]   "
                f"Telegram [#E8761A]{telegram}[/]   Modele [#E8761A]{provider}[/]"
            )

    class HelpBar(Static):
        def update_for_tab(self, tab_id: str) -> None:
            commands = {
                "tab-cluster": "q Quitter   r Rafraichir",
                "tab-jobs": "q Quitter   r Rafraichir   d Desactiver l'automatisme selectionne",
                "tab-runs": "q Quitter   r Rafraichir",
                "tab-models": "q Quitter   r Rafraichir   m Changer le modele",
                "tab-security": "q Quitter   r Rafraichir   p Changer la permission selectionnee",
                "tab-skills": "q Quitter   r Rafraichir   t Activer/desactiver la capacite user selectionnee",
                "tab-logs": "q Quitter   r Rafraichir",
            }
            self.update(commands.get(tab_id, "q Quitter   r Rafraichir"))

    class MauriceDashboard(App):
        TITLE = "Maurice"
        ENABLE_COMMAND_PALETTE = False
        CSS = """
        Screen { background: #120A04; color: #C8A882; }
        MauriceHeader { height: 8; padding: 0 2; background: #120A04; }
        MauriceStatus { height: 1; padding: 0 2; background: #1E0E06; color: #7A5C3A; }
        TabbedContent { height: 1fr; }
        TabPane { padding: 0; overflow-y: hidden; overflow-x: hidden; }
        Tabs { background: #1A0D07; }
        Tab { color: #7A5C3A; background: #1A0D07; }
        Tab:hover { color: #C8803A; background: #1A0D07; }
        Tab.-active { color: #E8761A; background: #1A0D07; }
        Underline > .underline--bar { color: #E8761A; }
        DataTable { height: 1fr; background: #120A04; color: #C8A882; }
        DataTable > .datatable--header { color: #DA7756; text-style: bold; }
        HelpBar { height: 1; padding: 0 1; background: #24303A; color: #F3D9AE; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quitter"),
            Binding("r", "refresh_data", "Rafraichir"),
            Binding("p", "change_permission", "Permission"),
            Binding("d", "disable_automation", "Auto off"),
            Binding("t", "toggle_user_skill", "Capacite"),
            Binding("m", "change_model", "Modele"),
        ]

        def compose(self) -> ComposeResult:
            self._frame = 0
            self._active_tab = "tab-cluster"
            self._automation_rows = []
            self._model_rows = []
            self._permission_rows = []
            self._skill_rows = []
            yield MauriceHeader()
            yield MauriceStatus(id="status-bar")
            with TabbedContent():
                with TabPane("Cluster", id="tab-cluster"):
                    yield DataTable(id="cluster-table")
                with TabPane("Automatismes", id="tab-jobs"):
                    yield DataTable(id="jobs-table")
                with TabPane("Sessions", id="tab-runs"):
                    yield DataTable(id="runs-table")
                with TabPane("Modeles", id="tab-models"):
                    yield DataTable(id="models-table")
                with TabPane("Permissions", id="tab-security"):
                    yield DataTable(id="security-table")
                with TabPane("Capacites", id="tab-skills"):
                    yield DataTable(id="skills-table")
                with TabPane("Journal", id="tab-logs"):
                    yield DataTable(id="logs-table")
            yield HelpBar(id="help-bar")

        def on_mount(self) -> None:
            self._init_tables()
            self.action_refresh_data()
            self.query_one("#help-bar", HelpBar).update_for_tab("tab-cluster")
            self.set_interval(max(refresh_seconds, 0.2), self.action_refresh_data)

        def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
            self._active_tab = event.pane.id or "tab-cluster"
            self.query_one("#help-bar", HelpBar).update_for_tab(self._active_tab)

        def _init_tables(self) -> None:
            self.query_one("#cluster-table", DataTable).add_columns("Agent", "Modele", "Statut", "Permissions", "Acces")
            self.query_one("#jobs-table", DataTable).add_columns("Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme")
            self.query_one("#runs-table", DataTable).add_columns("Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour")
            self.query_one("#models-table", DataTable).add_columns("Agent", "Type", "Modele", "Connexion")
            self.query_one("#security-table", DataTable).add_columns("Agent", "Mode", "Maximum global", "Changement")
            self.query_one("#skills-table", DataTable).add_columns("Agent", "Capacite", "Source", "Active", "Etat", "Problemes")
            self.query_one("#logs-table", DataTable).add_columns("Heure", "Niveau", "Source", "Agent", "Session", "Message")

        def action_refresh_data(self) -> None:
            bundle = load_workspace_config(workspace_root)
            selected_agent = _resolve_agent(bundle, agent_id)
            monitor = build_monitoring_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
            dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
            self.query_one("#status-bar", MauriceStatus).update_snapshot(bundle, monitor)
            self._replace_rows("#cluster-table", [[row.label, row.model, _status_marker(row.status, self._frame), row.permission, row.access] for row in dashboard.agents])
            self._replace_rows("#jobs-table", [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations])
            self._replace_rows("#runs-table", [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions])
            self._replace_rows("#models-table", [[row.agent_id, row.provider, row.model, row.auth_state] for row in dashboard.models])
            self._replace_rows("#security-table", [[row.agent_id, row.current_profile, row.global_maximum, row.escalation] for row in dashboard.permissions])
            self._replace_rows("#skills-table", [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills])
            self._replace_rows("#logs-table", [[row.time, _log_level_text(Text, row.level), row.source, row.agent_id, row.session_id, row.message] for row in dashboard.logs])
            self._automation_rows = dashboard.automations
            self._model_rows = dashboard.models
            self._permission_rows = dashboard.permissions
            self._skill_rows = dashboard.skills
            self._frame += 1

        def _replace_rows(self, selector: str, rows: list[list[str]]) -> None:
            table = self.query_one(selector, DataTable)
            table.clear(columns=False)
            for row in rows or [["-" for _ in table.columns]]:
                table.add_row(*row)

        def action_change_permission(self) -> None:
            if self._active_tab != "tab-security":
                self.notify("Va dans l'onglet Permissions pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#security-table", self._permission_rows)
            if row is None:
                self.notify("Ouvre l'onglet Permissions et selectionne un agent.", severity="warning")
                return
            profiles = ["safe", "limited", "power"]
            next_profile = profiles[(profiles.index(row.current_profile) + 1) % len(profiles)]
            try:
                update_agent(
                    workspace_root,
                    agent_id=row.agent_id,
                    permission_profile=next_profile,
                    confirmed_permission_elevation=False,
                )
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
            self.notify(f"Permission de {row.agent_id}: {next_profile}")
            self.action_refresh_data()

        def action_disable_automation(self) -> None:
            if self._active_tab != "tab-jobs":
                self.notify("Va dans l'onglet Automatismes pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#jobs-table", self._automation_rows)
            if row is None:
                self.notify("Ouvre l'onglet Automatismes et selectionne une ligne.", severity="warning")
                return
            if not row.enabled:
                self.notify("Cet automatisme est deja inactif.", severity="warning")
                return
            bundle = load_workspace_config(workspace_root)
            workspace = Path(bundle.host.workspace_root)
            try:
                JobStore(workspace / "agents" / row.owner_agent / "jobs.json").cancel(row.job_id)
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
            self.notify(f"Automatisme desactive: {row.name}")
            self.action_refresh_data()

        def action_toggle_user_skill(self) -> None:
            if self._active_tab != "tab-skills":
                self.notify("Va dans l'onglet Capacites pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#skills-table", self._skill_rows)
            if row is None:
                self.notify("Ouvre l'onglet Capacites et selectionne une ligne.", severity="warning")
                return
            if row.source != "user":
                self.notify("Les capacites systeme sont protegees ici. Seules les capacites user sont modifiables.", severity="warning", timeout=6)
                return
            bundle = load_workspace_config(workspace_root)
            agent = bundle.agents.agents[row.agent_id]
            skills = list(agent.skills or bundle.kernel.skills)
            if row.enabled and row.name in skills:
                skills.remove(row.name)
                action = "desactivee"
            elif not row.enabled and row.name not in skills:
                skills.append(row.name)
                action = "activee"
            else:
                self.notify("Aucun changement necessaire.", severity="warning")
                return
            update_agent(workspace_root, agent_id=row.agent_id, skills=skills)
            self.notify(f"Capacite {row.name} {action} pour {row.agent_id}")
            self.action_refresh_data()

        def action_change_model(self) -> None:
            if self._active_tab != "tab-models":
                self.notify("Va dans l'onglet Modeles pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#models-table", self._model_rows)
            agent = row.agent_id if row is not None else "main"
            try:
                with self.suspend():
                    _onboard_agent_model(workspace_root, agent_id=agent)
            except SystemExit as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify(f"Modele mis a jour pour {agent}")
            self.action_refresh_data()

        def _selected_model_row(self, selector: str, rows):
            table = self.query_one(selector, DataTable)
            if not rows:
                return None
            index = getattr(table, "cursor_row", 0) or 0
            if index < 0 or index >= len(rows):
                return None
            return rows[index]

    MauriceDashboard().run()


def _dashboard_rich(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    watch: bool,
    refresh_seconds: float,
) -> bool:
    try:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        return False

    console = Console()

    def build(frame: int):
        bundle = load_workspace_config(workspace_root)
        selected_agent = _resolve_agent(bundle, agent_id)
        snapshot = build_monitoring_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
        dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
        spinner = "|/-\\"[frame % 4]
        provider = _effective_model_label(bundle, selected_agent)
        gateway = f"{snapshot.runtime.gateway.get('host')}:{snapshot.runtime.gateway.get('port')}"
        telegram = "ok" if _telegram_channel_configured(bundle) else "-"
        title = Text("MAURICE", style="bold #E8761A")
        header = Text.from_markup(_ASCII_MAURICE)
        status = Text.from_markup(
            f"Service [bold #E8761A]{gateway}[/]   Automatismes [bold #E8761A]{'ok' if snapshot.runtime.scheduler_enabled else '-'}[/]   "
            f"Telegram [bold #E8761A]{telegram}[/]   Modele [bold #E8761A]{provider}[/]   {spinner}"
        )
        tabs = Text("Cluster   Automatismes   Sessions   Modeles   Permissions   Capacites   Journal", style="#E8761A")
        return Group(
            title,
            header,
            status,
            tabs,
            _rich_table(Table, "Cluster", ["Agent", "Modele", "Statut", "Permissions", "Acces"], [[row.label, row.model, row.status, row.permission, row.access] for row in dashboard.agents]),
            _rich_table(Table, "Automatismes", ["Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme"], [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations]),
            _rich_table(Table, "Sessions", ["Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour"], [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions]),
            _rich_table(Table, "Capacites", ["Agent", "Capacite", "Source", "Active", "Etat", "Problemes"], [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills]),
            _rich_table(Table, "Journal", ["Heure", "Niveau", "Source", "Agent", "Session", "Message"], [[row.time, row.level, row.source, row.agent_id, row.session_id, row.message] for row in dashboard.logs]),
        )

    try:
        if watch:
            frame = 0
            with Live(build(frame), console=console, refresh_per_second=4, screen=False) as live:
                while True:
                    time.sleep(max(refresh_seconds, 0.2))
                    frame += 1
                    live.update(build(frame))
        else:
            console.print(build(0))
    except KeyboardInterrupt:
        console.print("")
    return True


def _rich_table(table_cls, title: str, headers: list[str], rows: list[list[str]]):
    table = table_cls(
        title=title,
        border_style="#5C3317",
        header_style="bold #DA7756",
        style="#C8A882",
        title_style="bold #E8761A",
        expand=False,
    )
    for header in headers:
        table.add_column(header)
    for row in rows or [["-" for _ in headers]]:
        table.add_row(*[str(value) for value in row])
    return table


_ASCII_MAURICE = """\
[#E8761A]███╗   ███╗ █████╗ ██╗   ██╗██████╗ ██╗ ██████╗███████╗[/]
[#E8761A]████╗ ████║██╔══██╗██║   ██║██╔══██╗██║██╔════╝██╔════╝[/]
[#DA6010]██╔████╔██║███████║██║   ██║██████╔╝██║██║     █████╗  [/]
[#C04E08]██║╚██╔╝██║██╔══██║██║   ██║██╔══██╗██║██║     ██╔══╝  [/]
[#A03A00]██║ ╚═╝ ██║██║  ██║╚██████╔╝██║  ██║██║╚██████╗███████╗[/]
[#7A2A00]╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝╚══════╝[/]"""


def _cluster_rows(bundle: ConfigBundle, snapshot) -> list[list[str]]:
    active_agents = _active_agents(snapshot.events)
    rows = []
    for agent in snapshot.agents:
        marker = "*" if agent.default else " "
        status = "active" if agent.id in active_agents else agent.status
        rows.append(
            [
                f"{marker} {agent.id}",
                _agent_model_name(bundle, agent.id),
                status,
                agent.permission_profile,
                ",".join(agent.channels) or "-",
            ]
        )
    return rows


def _yes_no(value: bool) -> str:
    return "oui" if value else "non"


def _status_marker(status: str, frame: int = 0) -> str:
    if status == "occupe":
        spinner = "|/-\\"
        return f"{spinner[frame % 4]} actif"
    if status == "actif":
        return ". pret"
    if status in {"desactive", "archive"}:
        return f"- {status}"
    return f". {status}"


def _log_level_text(text_cls, level: str):
    styles = {
        "info": "#7A9A7A",
        "warning": "yellow",
        "error": "bold red",
    }
    return text_cls(level, style=styles.get(level, "white"))


def _model_rows(bundle: ConfigBundle, snapshot) -> list[list[str]]:
    return [
        [
            agent.id,
            str(_effective_model_config(bundle, bundle.agents.agents.get(agent.id)).get("provider") or "mock"),
            _agent_model_name(bundle, agent.id),
        ]
        for agent in snapshot.agents
    ]


def _security_rows(snapshot) -> list[list[str]]:
    return [
        [
            agent.id,
            agent.permission_profile,
            ",".join(agent.credentials) or "-",
            ",".join(agent.channels) or "-",
        ]
        for agent in snapshot.agents
    ]


def _event_rows(events) -> list[list[str]]:
    return [
        [
            event.time.strftime("%H:%M:%S"),
            event.agent_id,
            event.session_id,
            event.name,
        ]
        for event in events
    ]


def _render_dashboard(workspace_root: Path, *, agent_id: str | None, event_limit: int, frame: int) -> None:
    bundle = load_workspace_config(workspace_root)
    selected_agent = _resolve_agent(bundle, agent_id)
    dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
    spinner = "|/-\\"[frame % 4]

    print(_color(f"MAURICE {spinner}", "1;38;5;208"))
    print(_color("local-first agent runtime", "38;5;130"))
    print(_dashboard_status_line([
        ("Service", dashboard.status.service),
        ("Automatismes", dashboard.status.automatismes),
        ("Telegram", dashboard.status.telegram),
        ("Modele", dashboard.status.modele),
    ]))
    print(_color("Cluster  Automatismes  Sessions  Modeles  Permissions  Capacites  Journal", "38;5;130"))
    print(_color("=" * 88, "38;5;52"))
    print("")

    print(_dashboard_table(["Agent", "Modele", "Statut", "Permissions", "Acces"], [[row.label, row.model, _status_marker(row.status), row.permission, row.access] for row in dashboard.agents]))
    print("")
    print(_dashboard_status_line([
        ("Agents", str(len(dashboard.agents))),
        ("Automatismes", str(len(dashboard.automations))),
        ("Sessions", str(len(dashboard.sessions))),
        ("Capacites", str(len(dashboard.skills))),
    ]))
    print("")
    print(_color("Automatismes", "1;38;5;208"))
    print(_dashboard_table(["Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme"], [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations]))
    print("")
    print(_color("Sessions", "1;38;5;208"))
    print(_dashboard_table(["Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour"], [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions]))
    print("")
    print(_color("Capacites", "1;38;5;208"))
    print(_dashboard_table(["Agent", "Capacite", "Source", "Active", "Etat", "Problemes"], [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills]))
    print("")
    print(_color("Journal", "1;38;5;208"))
    if not dashboard.logs:
        print("- none")
        return
    for row in dashboard.logs:
        print(f"  {row.time}  {row.level:<7} {row.agent_id:<10} {row.session_id:<22} {row.message}")


def _start_services(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
    telegram: bool,
    scheduler: bool,
) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    pid_path = _pid_path(workspace)
    existing_pid = _read_pid(pid_path)
    if existing_pid and _pid_is_running(existing_pid):
        raise SystemExit(f"Maurice already appears to be running with pid {existing_pid}.")
    _write_pid(pid_path, os.getpid())
    stop_event = threading.Event()
    workers: list[threading.Thread] = []
    if scheduler:
        workers.append(
            threading.Thread(
                target=_scheduler_serve_until_stopped,
                args=(Path(bundle.host.workspace_root), agent_id, max(poll_seconds, 0.1), stop_event),
                daemon=True,
            )
        )
    if telegram and _telegram_channel_configured(bundle):
        workers.append(
            threading.Thread(
                target=_telegram_poll_until_stopped,
                args=(Path(bundle.host.workspace_root), agent_id, max(poll_seconds, 0.1), stop_event),
                daemon=True,
            )
        )
    if not workers:
        _remove_pid(pid_path)
        print("No Maurice services to start.")
        return
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda _signum, _frame: stop_event.set())
    print("Maurice started. Press Ctrl+C to stop.")
    for worker in workers:
        worker.start()
    try:
        while any(worker.is_alive() for worker in workers):
            time.sleep(0.2)
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=5)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        _remove_pid(pid_path)
        print("Maurice stopped")


def _start_services_daemon(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
    telegram: bool,
    scheduler: bool,
) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    pid_path = _pid_path(workspace)
    existing_pid = _read_pid(pid_path)
    if existing_pid and _pid_is_running(existing_pid):
        raise SystemExit(f"Maurice already appears to be running with pid {existing_pid}.")
    if not scheduler and not (telegram and _telegram_channel_configured(bundle)):
        print("No Maurice services to start.")
        return
    log_path = workspace / "maurice-service.log"
    command = [
        sys.executable,
        "-m",
        "maurice.host.cli",
        "start",
        "--workspace",
        str(workspace),
        "--poll-seconds",
        str(poll_seconds),
        "--foreground",
    ]
    if agent_id:
        command.extend(["--agent", agent_id])
    if not telegram:
        command.append("--no-telegram")
    if not scheduler:
        command.append("--no-scheduler")
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.time() + 3
    while time.time() < deadline:
        pid = _read_pid(pid_path)
        if pid and _pid_is_running(pid):
            print(f"Maurice started in background with pid {pid}.")
            print(f"Logs: {log_path}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        print(f"Maurice start requested in background with pid {process.pid}.")
        print(f"Logs: {log_path}")
        return
    raise SystemExit(f"Maurice failed to start. See logs: {log_path}")


def _stop_services(workspace_root: Path) -> None:
    bundle = load_workspace_config(workspace_root)
    pid_path = _pid_path(Path(bundle.host.workspace_root))
    pid = _read_pid(pid_path)
    if not pid:
        print("Maurice is not running.")
        return
    if not _pid_is_running(pid):
        _remove_pid(pid_path)
        print("Maurice is not running. Removed stale pid file.")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Stop requested for Maurice pid {pid}.")


def _pid_path(workspace: Path) -> Path:
    return workspace / "maurice.pid"


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(f"{pid}\n", encoding="utf-8")


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _remove_pid(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dashboard_status_line(items: list[tuple[str, str]]) -> str:
    parts = []
    for label, value in items:
        parts.append(f"{_color(label, '38;5;130')} {_color(value, '1;38;5;208')}")
    return "   ".join(parts)


def _dashboard_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["-" for _ in headers]]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [border]
    lines.append(
        "|"
        + "|".join(f" {_color(headers[index], '1;38;5;208'):<{widths[index] + _ansi_padding(headers[index])}} " for index in range(len(headers)))
        + "|"
    )
    lines.append(border)
    for row in rows:
        lines.append("|" + "|".join(f" {row[index]:<{widths[index]}} " for index in range(len(row))) + "|")
    lines.append(border)
    return "\n".join(lines)


def _job_rows(jobs) -> list[list[str]]:
    rows = []
    for job in jobs[:8]:
        rows.append(
            [
                job.name,
                job.owner,
                str(job.status),
                job.run_at.strftime("%H:%M:%S"),
                str(job.interval_seconds or "-"),
                _short(job.last_error or "-", 28),
            ]
        )
    return rows


def _run_rows(runs) -> list[list[str]]:
    rows = []
    for run in runs[:8]:
        rows.append(
            [
                run.id[-8:],
                str(run.state),
                _short(run.task, 40),
                run.updated_at.strftime("%H:%M:%S"),
            ]
        )
    return rows


def _skill_rows(skills) -> list[list[str]]:
    rows = []
    for skill in skills[:12]:
        issue = "; ".join(skill.errors or skill.suggested_fixes) or "-"
        rows.append([skill.name, skill.state, _short(issue, 40)])
    return rows


def _active_agents(events) -> set[str]:
    active: set[str] = set()
    for event in events[-20:]:
        if event.name in {"turn.started", "tool.started", "gateway.message.received", "job.started"}:
            active.add(event.agent_id)
        if event.name in {"turn.completed", "turn.failed", "tool.completed", "tool.failed", "job.completed", "job.failed"}:
            active.discard(event.agent_id)
    return active


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(limit - 1, 0)] + "…"


def _ansi_padding(text: str) -> int:
    return len(_color(text, "1;38;5;208")) - len(text) if _supports_color() else 0


def _compact_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "{}"
    return "{" + ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) + "}"


def _agent_model_name(bundle: ConfigBundle, agent_id: str) -> str:
    agent = bundle.agents.agents.get(agent_id)
    model = _effective_model_config(bundle, agent)
    provider = model.get("provider") or "mock"
    name = model.get("name") or provider
    return f"{provider}/{name}"


def _effective_model_label(bundle: ConfigBundle, agent=None) -> str:
    model = _effective_model_config(bundle, agent)
    return f"{model.get('provider') or 'mock'}:{model.get('protocol') or model.get('name') or 'mock'}"


def _default_agent(bundle: ConfigBundle):
    return next((agent for agent in bundle.agents.agents.values() if agent.default), None)


def _telegram_channel_configured(bundle: ConfigBundle) -> bool:
    telegram = bundle.host.channels.get("telegram")
    return isinstance(telegram, dict) and telegram.get("enabled", True) is not False


def _print_host_checks(title: str, ok: bool, checks) -> None:
    status = "ok" if ok else "failed"
    print(f"{title}: {status}")
    for check in checks:
        print(f"- {check.name}: {check.state} - {check.summary}")


def _onboarding_existing_values(workspace_root: Path) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    ensure_workspace_config_migrated(workspace)
    host_data = read_yaml_file(host_config_path(workspace))
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    agents_data = read_yaml_file(agents_config_path(workspace))
    skills_data = read_yaml_file(workspace_skills_config_path(workspace))

    kernel = kernel_data.get("kernel") if isinstance(kernel_data.get("kernel"), dict) else {}
    host = host_data.get("host") if isinstance(host_data.get("host"), dict) else {}
    gateway = host.get("gateway") if isinstance(host.get("gateway"), dict) else {}
    permissions = kernel.get("permissions") if isinstance(kernel.get("permissions"), dict) else {}
    model = kernel.get("model") if isinstance(kernel.get("model"), dict) else {}
    skills = skills_data.get("skills") if isinstance(skills_data.get("skills"), dict) else {}
    web_skill = skills.get("web") if isinstance(skills.get("web"), dict) else {}
    channels = host.get("channels") if isinstance(host.get("channels"), dict) else {}
    telegram = channels.get("telegram") if isinstance(channels.get("telegram"), dict) else {}
    provider = _onboarding_provider_choice(model)

    existing: dict[str, Any] = {
        "host_data": host_data,
        "kernel_data": kernel_data,
        "agents_data": agents_data,
        "skills_data": skills_data,
        "provider": provider,
    }
    if permissions.get("profile"):
        existing["profile"] = permissions["profile"]
    if gateway.get("port"):
        existing["gateway_port"] = gateway["port"]
    if web_skill.get("base_url"):
        existing["searxng_url"] = web_skill["base_url"]
    if model.get("name"):
        existing["model"] = model["name"]
    if model.get("base_url"):
        existing["base_url"] = model["base_url"]
    if model.get("credential"):
        existing["credential"] = model["credential"]
    if telegram:
        existing["telegram_config"] = telegram
        existing["telegram_credential"] = telegram.get("credential") or "telegram_bot"
        existing["telegram_agent"] = telegram.get("agent") or "main"
        existing["telegram_allowed_users"] = telegram.get("allowed_users") or []
        existing["telegram_allowed_chats"] = telegram.get("allowed_chats") or []
    return existing


def _onboarding_provider_choice(model: dict[str, Any]) -> str:
    provider = model.get("provider")
    protocol = model.get("protocol")
    if provider == "auth" and protocol == "chatgpt_codex":
        return "chatgpt"
    if provider == "api" and protocol == "openai_chat_completions":
        return "openai_api"
    if provider == "ollama" or (provider == "api" and protocol == "ollama_chat"):
        return "ollama"
    return "mock"


def _model_existing_values(model: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {"provider": _onboarding_provider_choice(model)}
    if model.get("name"):
        existing["model"] = model["name"]
    if model.get("base_url"):
        existing["base_url"] = model["base_url"]
    if model.get("credential"):
        existing["credential"] = model["credential"]
    return existing


def _ask_model_config(
    workspace: Path,
    *,
    existing: dict[str, object],
    provider: str | None = None,
) -> tuple[dict[str, object], str | None, str]:
    provider = provider or _ask_provider_choice(default=_real_provider_default(existing.get("provider")))
    credential_to_allow: str | None = None
    kernel_model: dict[str, object] = {
        "provider": "mock",
        "protocol": None,
        "name": "mock",
        "base_url": None,
        "credential": None,
    }
    if provider == "chatgpt":
        model = _ask_model_from_choices(
            "Modele ChatGPT",
            _chatgpt_model_choices(),
            default=str(existing.get("model") or "gpt-5"),
        )
        kernel_model = {
            "provider": "auth",
            "protocol": "chatgpt_codex",
            "name": model,
            "base_url": None,
            "credential": CHATGPT_CREDENTIAL_NAME,
        }
        credential_to_allow = CHATGPT_CREDENTIAL_NAME
    elif provider == "openai_api":
        credential_name = _ask("Credential name", default=str(existing.get("credential") or "openai"))
        model = _ask("Model name", default=str(existing.get("model") or "gpt-4o-mini"))
        base_url = _ask("API base URL", default=str(existing.get("base_url") or "https://api.openai.com/v1"))
        key_hint = "configured; press Enter to keep" if _credential_exists(workspace, credential_name) else "leave empty to configure later"
        api_key = _ask_api_key(f"API key ({key_hint})")
        kernel_model = {
            "provider": "api",
            "protocol": "openai_chat_completions",
            "name": model,
            "base_url": base_url,
            "credential": credential_name,
        }
        credential_to_allow = credential_name
        if api_key:
            _save_api_credential(workspace, credential_name, api_key, base_url)
    elif provider == "ollama":
        deployment = _ask_ollama_deployment(existing)
        credential_name = None
        api_key = ""
        if deployment == "cloud":
            base_url = _ask("Ollama Cloud URL", default=str(existing.get("base_url") or "https://ollama.com"))
            credential_name = _ask("Ollama credential name", default=str(existing.get("credential") or "ollama"))
            key_hint = "configured; press Enter to keep" if _credential_exists(workspace, credential_name) else "required for Ollama Cloud; leave empty to configure later"
            api_key = _ask_api_key(f"Ollama API key ({key_hint})")
            if api_key:
                _save_api_credential(workspace, credential_name, api_key, base_url)
            else:
                api_key = _credential_value(workspace, credential_name)
        else:
            base_url = _ask("Ollama URL auto-heberge", default=str(existing.get("base_url") or "http://localhost:11434"))
        model = _ask_model_from_choices(
            "Modele Ollama",
            _ollama_model_choices(base_url, api_key=api_key),
            default=str(existing.get("model") or "llama3.1"),
        )
        kernel_model = {
            "provider": "ollama",
            "protocol": "ollama_chat",
            "name": model,
            "base_url": base_url,
            "credential": credential_name,
        }
        credential_to_allow = credential_name
    return kernel_model, credential_to_allow, provider


def _onboard_agent(workspace_root: Path, *, agent_id: str) -> None:
    workspace = workspace_root.expanduser().resolve()
    bundle = load_workspace_config(workspace)
    print(f"Maurice agent onboarding: {workspace}")
    if not agent_id:
        agent_id = _ask("Agent id", default="")
    if not agent_id:
        raise SystemExit("Agent id is required.")
    profile = _ask_choice(
        "Permission profile",
        ["limited", "safe", "power"],
        default=bundle.kernel.permissions.profile,
    )
    skills = _ask_csv_strings(
        "Skills enabled",
        default=",".join(bundle.kernel.skills),
    )
    credentials = _ask_csv_strings(
        "Credentials allowed (comma-separated, empty = none)",
        default="",
    )
    channels = _ask_csv_strings(
        "Channels bound (comma-separated, empty = none)",
        default="",
    )
    make_default = _ask_yes_no("Make this the default agent", default=False)
    try:
        agent = create_agent(
            workspace,
            agent_id=agent_id,
            permission_profile=profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=make_default,
        )
    except PermissionError as exc:
        if not _ask_yes_no(f"{exc} Confirm permission elevation", default=False):
            raise SystemExit(str(exc)) from exc
        agent = create_agent(
            workspace,
            agent_id=agent_id,
            permission_profile=profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=make_default,
            confirmed_permission_elevation=True,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent onboarded: {agent.id} ({agent.permission_profile})")


def _onboard_agent_model(workspace_root: Path, *, agent_id: str) -> None:
    workspace = workspace_root.expanduser().resolve()
    bundle = load_workspace_config(workspace)
    if not agent_id:
        agent_id = _ask("Agent id", default="")
    if not agent_id:
        raise SystemExit("Agent id is required.")
    if agent_id not in bundle.agents.agents:
        raise SystemExit(f"Unknown agent: {agent_id}")
    agent = bundle.agents.agents[agent_id]
    existing = _model_existing_values(agent.model or bundle.kernel.model.model_dump(mode="json"))
    print(f"Maurice model onboarding: {agent_id}")
    print("")
    _print_dim("Appuie sur Entree pour conserver la valeur proposee.")
    kernel_model, credential_to_allow, provider = _ask_model_config(workspace, existing=existing)
    credentials = list(agent.credentials or [])
    if credential_to_allow and credential_to_allow not in credentials:
        credentials.append(credential_to_allow)
    if agent.default:
        _write_kernel_model(workspace, kernel_model)
        update_agent(workspace, agent_id=agent_id, clear_model=True, credentials=credentials)
    else:
        update_agent(workspace, agent_id=agent_id, model=kernel_model, credentials=credentials)
    if provider == "chatgpt" and _ask_yes_no("Connect ChatGPT now?", default=False):
        _auth_login("chatgpt", workspace)
    print("")
    print(f"Agent model updated: {agent_id}")
    print(f"Next: maurice run --agent {agent_id} --message \"salut Maurice\"")


def _write_kernel_model(workspace: Path, kernel_model: dict[str, object]) -> None:
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel_data.setdefault("kernel", {})["model"] = kernel_model
    write_yaml_file(kernel_path, kernel_data)


def _onboard_interactive(workspace_root: Path, *, existing: dict[str, object] | None = None) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    existing = existing or {}
    print(f"Maurice workspace initialized: {workspace}")
    print("")
    _print_title("Maurice - Onboarding")
    _print_dim("Appuie sur Entree pour conserver la valeur proposee.")

    profile = _ask_choice(
        "Permission profile",
        ["limited", "safe", "power"],
        default=str(existing.get("profile") or bundle.kernel.permissions.profile),
    )
    provider = _ask_provider_choice(default=_real_provider_default(existing.get("provider")))
    gateway_port = _ask_int("Gateway port", default=int(existing.get("gateway_port") or bundle.host.gateway.port))
    search_config = _existing_search_config(existing)
    telegram_config = _ask_telegram_config(workspace, existing=existing)

    kernel_model, credential_to_allow, provider = _ask_model_config(
        workspace,
        existing=existing,
        provider=provider,
    )

    _write_onboarding_config(
        workspace,
        profile=profile,
        gateway_port=gateway_port,
        kernel_model=kernel_model,
        credential_to_allow=credential_to_allow,
        search_config=search_config,
        telegram_config=telegram_config,
        existing=existing,
    )

    if provider == "chatgpt" and _ask_yes_no("Connect ChatGPT now?", default=False):
        _auth_login("chatgpt", workspace)

    print("")
    print("Maurice onboarding complete.")
    print(f"Workspace: {workspace}")
    provider_label = PROVIDER_HELP[provider][0]
    print(f"Provider: {provider_label} ({kernel_model['provider']} {kernel_model.get('protocol') or ''})".strip())
    print(f"Gateway: http://127.0.0.1:{gateway_port}")
    print("")
    print("Next:")
    print(f"  maurice doctor --workspace {workspace}")
    print(f"  maurice run --workspace {workspace} --message \"salut Maurice\"")


def _write_onboarding_config(
    workspace: Path,
    *,
    profile: str,
    gateway_port: int,
    kernel_model: dict[str, object],
    credential_to_allow: str | None,
    search_config: dict[str, object] | None,
    telegram_config: dict[str, object] | None,
    existing: dict[str, object] | None = None,
) -> None:
    existing = existing or {}
    ensure_workspace_config_migrated(workspace)
    fresh_host_data = read_yaml_file(host_config_path(workspace))
    host_data = _existing_config(existing, "host_data") or fresh_host_data
    fresh_host = fresh_host_data.get("host") if isinstance(fresh_host_data.get("host"), dict) else {}
    host = host_data.setdefault("host", {})
    for key in ("runtime_root", "workspace_root", "skill_roots"):
        if key in fresh_host:
            host[key] = fresh_host[key]
    host_data.setdefault("host", {}).setdefault("gateway", {})["port"] = gateway_port
    channels = host_data.setdefault("host", {}).setdefault("channels", {})
    if telegram_config:
        channels["telegram"] = telegram_config
    else:
        channels.pop("telegram", None)
    write_yaml_file(host_config_path(workspace), host_data)

    kernel_data = _existing_config(existing, "kernel_data") or read_yaml_file(kernel_config_path(workspace))
    kernel_data.setdefault("kernel", {})["model"] = kernel_model
    kernel_data["kernel"].setdefault("permissions", {})["profile"] = profile
    write_yaml_file(kernel_config_path(workspace), kernel_data)

    fresh_agents_data = read_yaml_file(agents_config_path(workspace))
    agents_data = _existing_config(existing, "agents_data") or fresh_agents_data
    main_agent = agents_data.setdefault("agents", {}).setdefault("main", {})
    fresh_main = (fresh_agents_data.get("agents") or {}).get("main") if isinstance(fresh_agents_data.get("agents"), dict) else {}
    if isinstance(fresh_main, dict):
        for key in ("workspace", "event_stream"):
            if key in fresh_main:
                main_agent[key] = fresh_main[key]
    main_agent["permission_profile"] = profile
    if credential_to_allow:
        credentials = list(main_agent.get("credentials") or [])
        if credential_to_allow not in credentials:
            credentials.append(credential_to_allow)
        main_agent["credentials"] = credentials
    channels = list(main_agent.get("channels") or [])
    if telegram_config and "telegram" not in channels:
        channels.append("telegram")
    if not telegram_config:
        channels = [channel for channel in channels if channel != "telegram"]
    main_agent["channels"] = channels
    write_yaml_file(agents_config_path(workspace), agents_data)

    skills_data = _existing_config(existing, "skills_data") or read_yaml_file(workspace_skills_config_path(workspace))
    web_config = skills_data.setdefault("skills", {}).setdefault("web", {})
    if search_config:
        web_config["search_provider"] = search_config["provider"]
        web_config["base_url"] = search_config["base_url"]
    else:
        web_config.pop("search_provider", None)
        web_config.pop("base_url", None)
    write_yaml_file(workspace_skills_config_path(workspace), skills_data)


def _existing_config(existing: dict[str, object], key: str) -> dict[str, Any]:
    value = existing.get(key)
    return value if isinstance(value, dict) else {}


def _existing_search_config(existing: dict[str, object]) -> dict[str, object] | None:
    searxng_url = existing.get("searxng_url")
    if isinstance(searxng_url, str) and searxng_url:
        return {"provider": "searxng", "base_url": searxng_url}
    return {"provider": "searxng", "base_url": DEFAULT_SEARXNG_URL}


def _ask_telegram_config(workspace: Path, *, existing: dict[str, object]) -> dict[str, object] | None:
    current = existing.get("telegram_config")
    default_enabled = isinstance(current, dict)
    print("")
    print(_color("Bot Telegram", "1"))
    print("Maurice peut pre-configurer ton bot Telegram maintenant.")
    print("Dans Telegram, parle a @BotFather, cree un bot avec /newbot, puis colle ici le token qu'il te donne.")
    print("Pour trouver ton id Telegram, tu peux envoyer /start a @userinfobot ou @RawDataBot.")
    print("Tu peux mettre plusieurs ids autorises, separes par des virgules.")
    _print_dim("Pour les groupes, on securise d'abord par l'id de l'utilisateur qui parle au bot.")
    if not _ask_yes_no("Configurer un bot Telegram", default=default_enabled):
        return None

    credential_name = str(existing.get("telegram_credential") or "telegram_bot")
    token_configured = _credential_exists(workspace, credential_name)
    token_hint = "deja configure, Entree pour garder" if token_configured else "coller le token BotFather, ou Entree pour le configurer plus tard"
    token = _ask_secret(f"Token du bot Telegram ({token_hint})", default="")
    if token:
        _save_token_credential(
            workspace,
            credential_name,
            token,
            provider="telegram_bot",
        )

    allowed_users = _ask_csv_ints(
        "Id(s) Telegram autorises (plusieurs ids separes par virgule)",
        default=_csv_default(existing.get("telegram_allowed_users")),
    )
    effective_token = token or _credential_value(workspace, credential_name)
    if effective_token and allowed_users and _ask_yes_no("Envoyer un premier message au bot pour valider la config maintenant", default=True):
        _validate_telegram_first_message(effective_token, allowed_users)
    return {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": credential_name,
        "allowed_users": allowed_users,
        "allowed_chats": [],
        "status": "configured_pending_adapter",
    }


def _csv_default(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return ",".join(str(item) for item in value)


def _ask_csv_ints(prompt: str, *, default: str) -> list[int]:
    while True:
        value = _ask(prompt, default=default)
        if not value.strip():
            return []
        try:
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        except ValueError:
            print("Entre des nombres separes par des virgules.")


def _ask_csv_strings(prompt: str, *, default: str) -> list[str]:
    value = _ask(prompt, default=default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _credential_value(workspace: Path, name: str) -> str:
    record = load_workspace_credentials(workspace).credentials.get(name)
    return record.value if record is not None else ""


def _validate_telegram_first_message(token: str, allowed_users: list[int]) -> None:
    print("")
    print("Validation Telegram")
    print("1. Ouvre ton bot dans Telegram.")
    print("2. Envoie-lui un message, par exemple: salut Maurice")
    print("3. Reviens ici et appuie sur Entree.")
    input("Entree quand le message est envoye: ")
    try:
        updates = _telegram_get_updates(token)
    except (OSError, ValueError) as exc:
        print(f"Validation Telegram impossible pour le moment: {exc}")
        return
    seen_ids = _telegram_sender_ids(updates)
    matching = sorted(set(seen_ids).intersection(allowed_users))
    if matching:
        print(f"Validation Telegram OK: message recu depuis id {matching[0]}.")
        return
    if seen_ids:
        print(f"Message recu, mais depuis un id non autorise: {', '.join(str(item) for item in sorted(set(seen_ids)))}")
        print("Ajoute cet id dans la liste autorisee puis relance l'onboarding.")
        return
    print("Aucun message recent trouve. Tu pourras relancer l'onboarding apres avoir envoye un message au bot.")


def _telegram_get_updates(
    token: str,
    *,
    offset: int | None = None,
    timeout_seconds: int = 0,
) -> list[dict[str, Any]]:
    query = []
    if offset is not None:
        query.append(f"offset={offset}")
    if timeout_seconds:
        query.append(f"timeout={timeout_seconds}")
    suffix = f"?{'&'.join(query)}" if query else ""
    url = f"https://api.telegram.org/bot{token}/getUpdates{suffix}"
    try:
        with urlrequest.urlopen(url, timeout=max(timeout_seconds + 5, 10)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise ValueError(detail) from exc
    if not payload.get("ok"):
        raise ValueError(payload.get("description") or "Telegram getUpdates failed.")
    result = payload.get("result") or []
    return result if isinstance(result, list) else []


def _telegram_bot_username(token: str) -> str:
    try:
        payload = _telegram_api_json(token, "getMe", {})
    except (OSError, ValueError):
        return ""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = result.get("username")
    return username if isinstance(username, str) else ""


def _telegram_send_message(token: str, chat_id: int, text: str) -> None:
    _telegram_api_json(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text or "(empty response)",
        },
    )


def _telegram_send_chat_action(token: str, chat_id: int, action: str = "typing") -> None:
    _telegram_api_json(
        token,
        "sendChatAction",
        {
            "chat_id": chat_id,
            "action": action,
        },
    )


def _telegram_api_json(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise ValueError(detail) from exc
    if not result.get("ok"):
        raise ValueError(result.get("description") or f"Telegram {method} failed.")
    return result


def _telegram_update_to_inbound(
    update: dict[str, Any],
    *,
    agent_id: str,
    allowed_users: list[int],
    allowed_chats: list[int],
):
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return None
    if allowed_users and user_id not in allowed_users:
        return None
    if allowed_chats and chat_id not in allowed_chats:
        return None
    return {
        "channel": "telegram",
        "peer_id": str(user_id),
        "text": text,
        "agent_id": agent_id,
        "metadata": {
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message.get("message_id"),
        },
    }


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int)]


def _read_int_file(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_int_file(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def _redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def _telegram_sender_ids(updates: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for update in updates:
        message = update.get("message") or update.get("edited_message") or update.get("channel_post") or {}
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if isinstance(sender_id, int):
            ids.append(sender_id)
    return ids


def _save_api_credential(workspace: Path, name: str, api_key: str, base_url: str) -> None:
    store = load_workspace_credentials(workspace)
    store.credentials[name] = CredentialRecord(type="api_key", value=api_key, base_url=base_url)
    write_workspace_credentials(workspace, store)


def _save_token_credential(workspace: Path, name: str, token: str, *, provider: str) -> None:
    store = load_workspace_credentials(workspace)
    store.credentials[name] = CredentialRecord(type="token", value=token, provider=provider)
    write_workspace_credentials(workspace, store)


def _credential_exists(workspace: Path, name: str) -> bool:
    return name in load_workspace_credentials(workspace).credentials


def _credential_value(workspace: Path, name: str) -> str:
    credential = load_workspace_credentials(workspace).credentials.get(name)
    return credential.value if credential is not None else ""


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _color(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _print_title(text: str) -> None:
    line = f" {text} "
    print(_color(line, "1;34"))


def _print_dim(text: str) -> None:
    print(_color(text, "2"))


def _ask(prompt: str, *, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_secret(prompt: str, *, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    if sys.stdin.isatty():
        value = getpass.getpass(f"{prompt}{suffix}: ").strip()
    else:
        value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_api_key(prompt: str) -> str:
    _print_dim("La cle ne s'affiche pas pendant la saisie. Dans un terminal Linux, colle avec Ctrl+Shift+V ou clic droit > Coller.")
    while True:
        value = _ask_secret(prompt, default="").strip()
        if value == "^":
            print("Collage non pris en compte. Essaie Ctrl+Shift+V ou clic droit > Coller.")
            continue
        return value


def _ask_int(prompt: str, *, default: int) -> int:
    while True:
        value = _ask(prompt, default=str(default))
        try:
            return int(value)
        except ValueError:
            print("Please enter a number.")


def _ask_choice(prompt: str, choices: list[str], *, default: str) -> str:
    choice_text = "/".join(choices)
    while True:
        value = _ask(f"{prompt} ({choice_text})", default=default)
        if value in choices:
            return value
        print(f"Choose one of: {choice_text}")


def _ask_provider_choice(*, default: str) -> str:
    default = _real_provider_default(default)
    choices = list(PROVIDER_HELP)
    print("")
    print(_color("Provider LLM", "1"))
    for index, choice in enumerate(choices, start=1):
        label, description = PROVIDER_HELP[choice]
        marker = _color(choice, "36")
        print(f"  {index}. {marker} - {label}: {description}")
    print("")
    while True:
        value = _ask(f"Choix du provider (1-{len(choices)} ou identifiant)", default=default)
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        aliases = {
            "openai": "openai_api",
            "api": "openai_api",
        }
        value = aliases.get(value, value)
        if value in choices:
            return value
        print(f"Choisis un provider valide: {', '.join(choices)}")


def _ask_ollama_deployment(existing: dict[str, object]) -> str:
    base_url = str(existing.get("base_url") or "")
    credential = str(existing.get("credential") or "")
    default = "cloud" if credential or (base_url and not _is_local_ollama_url(base_url)) else "auto_heberge"
    print("")
    print(_color("Hebergement Ollama", "1"))
    print(f"  1. {_color('auto_heberge', '36')} - Ollama sur ta machine, ton reseau, ou ton serveur")
    print(f"  2. {_color('cloud', '36')} - Ollama Cloud ou endpoint distant avec cle API")
    print("")
    return _ask_choice("Mode Ollama", ["auto_heberge", "cloud"], default=default)


def _ask_model_from_choices(prompt: str, choices: list[tuple[str, str]], *, default: str) -> str:
    choices_by_id = {model_id: label for model_id, label in choices}
    if choices:
        print("")
        print(_color(prompt, "1"))
        for index, (model_id, label) in enumerate(choices, start=1):
            marker = _color(model_id, "36")
            suffix = f" - {label}" if label and label != model_id else ""
            print(f"  {index}. {marker}{suffix}")
        print("")
        while True:
            value = _ask(f"Choix du modele (1-{len(choices)} ou identifiant)", default=default)
            if value.isdigit():
                index = int(value)
                if 1 <= index <= len(choices):
                    return choices[index - 1][0]
                print(f"Choisis un numero entre 1 et {len(choices)}, ou entre un identifiant manuel.")
                continue
            if value in choices_by_id:
                return value
            return value
    return _ask(prompt, default=default)


def _chatgpt_model_choices() -> list[tuple[str, str]]:
    cache_path = Path.home() / ".codex" / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    rows: list[tuple[int, str, str]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = str(model.get("slug") or "").strip()
        if not slug:
            continue
        visibility = str(model.get("visibility") or "list")
        if visibility not in {"list", "default", ""}:
            continue
        priority = model.get("priority")
        if not isinstance(priority, int):
            priority = 999
        display_name = str(model.get("display_name") or slug).strip()
        description = str(model.get("description") or "").strip()
        label = display_name if not description else f"{display_name}: {description}"
        rows.append((priority, slug, label))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [(slug, label) for _, slug, label in rows]


def _ollama_model_choices(base_url: str, *, api_key: str = "") -> list[tuple[str, str]]:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        req = urlrequest.Request(url, headers=headers)
        with urlrequest.urlopen(req, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    choices: list[tuple[str, str]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "").strip()
        if not name:
            continue
        details = model.get("details") if isinstance(model.get("details"), dict) else {}
        family = str(details.get("family") or "").strip()
        size = model.get("size")
        label_parts = [part for part in [family, _format_bytes(size)] if part]
        choices.append((name, ", ".join(label_parts) or name))
    return sorted(choices, key=lambda item: item[0])


def _is_local_ollama_url(base_url: str) -> bool:
    value = base_url.lower().strip().rstrip("/")
    return (
        value.startswith("http://localhost")
        or value.startswith("http://127.0.0.1")
        or value.startswith("http://0.0.0.0")
    )


def _format_bytes(value: object) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"


def _real_provider_default(provider: object) -> str:
    value = str(provider or "").strip()
    if value in PROVIDER_HELP:
        return value
    return "chatgpt"


def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "o", "oui"}:
            return True
        if value in {"n", "no", "non"}:
            return False
        print("Please answer yes or no.")


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


def _auth_login(provider: str, workspace_root: Path) -> None:
    if provider != "chatgpt":
        raise SystemExit(f"Unsupported auth provider: {provider}")

    def on_url(url: str) -> None:
        print("Open this URL to authenticate ChatGPT:")
        print(url)
        import webbrowser

        webbrowser.open(url)

    token_data = ChatGPTAuthFlow().run(on_url=on_url)
    save_chatgpt_auth(workspace_root, token_data)
    print("ChatGPT auth: connected")


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


def _runs_list(workspace_root: Path, *, agent_id: str | None, state: str | None) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    runs = store.list(state=state)
    if not runs:
        print("No runs.")
        return
    for run in runs:
        print(f"{run.id} {run.state} parent={run.parent_agent_id} safe_to_resume={run.safe_to_resume} {run.task}")


def _runs_create(
    workspace_root: Path,
    *,
    agent_id: str | None,
    task: str,
    base_agent: str | None,
    inline_profile: str | None,
    context_summary: str,
    context_inheritance: str,
    relevant_files: list[str] | None,
    constraints: list[str] | None,
    plan_steps: list[str] | None,
    write_paths: list[str] | None,
    permission_classes: list[str] | None,
    requires_self_check: bool,
    can_request_install: bool,
    package_managers: list[str] | None,
    autonomy_mode: str,
    max_steps: int,
    checkpoint_every_steps: int,
    stop_conditions: list[str] | None,
) -> None:
    store, agent = _run_store_for(workspace_root, agent_id)
    base_agent_id, base_agent_profile = _resolve_run_profile(
        workspace_root,
        parent_agent_id=agent.id,
        base_agent_id=base_agent,
        inline_profile=inline_profile,
    )
    run = store.create(
        parent_agent_id=agent.id,
        task=task,
        base_agent=base_agent_id,
        base_agent_profile=base_agent_profile,
        context_summary=context_summary,
        context_inheritance=context_inheritance,
        relevant_files=[{"path": path, "reason": ""} for path in relevant_files or []],
        constraints=constraints,
        plan=plan_steps,
        write_scope={"paths": write_paths or []},
        permission_scope={"classes": permission_classes or []},
        dependency_policy={
            "can_request_install": can_request_install,
            "allowed_package_managers": package_managers or [],
            "requires_parent_approval": True,
        },
        output_contract={"requires_self_check": requires_self_check},
        execution_policy={
            "mode": autonomy_mode,
            "max_steps": max_steps,
            "checkpoint_every_steps": checkpoint_every_steps,
            "stop_conditions": stop_conditions
            or [
                "needs_user_decision",
                "permission_denied",
                "approval_required",
                "tests_failing_after_retry",
                "plan_complete",
                "execution_engine_missing",
            ],
        },
    )
    print(f"Run created: {run.id}")


def _base_agent_profile(workspace_root: Path, base_agent_id: str) -> tuple[str, dict[str, object]]:
    bundle = load_workspace_config(workspace_root)
    try:
        agent = bundle.agents.agents[base_agent_id]
    except KeyError as exc:
        raise SystemExit(f"Unknown base agent: {base_agent_id}") from exc
    if agent.status != "active":
        raise SystemExit(f"Base agent is not active: {base_agent_id} ({agent.status})")
    return (
        agent.id,
        {
            "id": agent.id,
            "skills": list(agent.skills),
            "credentials": list(agent.credentials),
            "permission_profile": agent.permission_profile,
            "channels": list(agent.channels),
            "model": agent.model,
        },
    )


def _resolve_run_profile(
    workspace_root: Path,
    *,
    parent_agent_id: str,
    base_agent_id: str | None,
    inline_profile: str | None,
) -> tuple[str, dict[str, object]]:
    if inline_profile and base_agent_id:
        raise SystemExit("--inline-profile cannot be combined with --base-agent")
    if inline_profile:
        try:
            profile = json.loads(inline_profile)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid inline profile JSON: {exc}") from exc
        if not isinstance(profile, dict):
            raise SystemExit("Inline profile must be a JSON object.")
        profile.setdefault("id", "inline")
        profile.setdefault("skills", [])
        profile.setdefault("credentials", [])
        profile.setdefault("permission_profile", "safe")
        profile.setdefault("channels", [])
        profile.setdefault("model", None)
        profile["inline"] = True
        return str(profile["id"]), profile
    return _base_agent_profile(workspace_root, base_agent_id or parent_agent_id)


def _runs_start(workspace_root: Path, run_id: str, *, agent_id: str | None) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    run = store.mark_running(run_id)
    print(f"Run started: {run.id}")


def _runs_resume(workspace_root: Path, run_id: str, *, agent_id: str | None) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    try:
        run = store.resume(run_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Run resumed: {run.id}")


def _runs_execute(workspace_root: Path, run_id: str, *, agent_id: str | None, prepare_only: bool) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    executor = RunExecutor(store)
    if prepare_only:
        run, _envelope = executor.prepare(run_id)
        print(f"Run prepared: {run.id}")
        return
    run, report = executor.execute_autonomous(run_id)
    print(f"Run autonomy stopped: {run.id} reason={report.stop_reason} status={run.state}")


def _runs_checkpoint(
    workspace_root: Path,
    run_id: str,
    *,
    agent_id: str | None,
    summary: str,
    safe_to_resume: bool,
) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    run, _envelope = store.checkpoint(
        run_id,
        summary=summary,
        safe_to_resume=safe_to_resume,
    )
    print(f"Run checkpointed: {run.id}")


def _runs_complete(
    workspace_root: Path,
    run_id: str,
    *,
    agent_id: str | None,
    summary: str,
    changed_files: list[str] | None,
    risks: list[str] | None,
    verification_commands: list[str] | None,
    verification_statuses: list[str] | None,
    verification_summaries: list[str] | None,
) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    try:
        run, _envelope = store.complete(
            run_id,
            summary=summary,
            changed_files=changed_files,
            risks=risks,
            verification=_verification_records(
                verification_commands,
                verification_statuses,
                verification_summaries,
            ),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Run completed: {run.id}")


def _verification_records(
    commands: list[str] | None,
    statuses: list[str] | None,
    summaries: list[str] | None,
) -> list[dict[str, str]]:
    if not commands:
        return []
    statuses = statuses or []
    summaries = summaries or []
    records = []
    for index, command in enumerate(commands):
        records.append(
            {
                "command": command,
                "status": statuses[index] if index < len(statuses) else "unknown",
                "output_summary": summaries[index] if index < len(summaries) else "",
            }
        )
    return records


def _runs_review(
    workspace_root: Path,
    run_id: str,
    *,
    agent_id: str | None,
    status: str,
    summary: str,
    followups: list[str] | None,
) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    try:
        run, _review = store.review(
            run_id,
            status=status,
            summary=summary,
            requested_followups=followups,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Run reviewed: {run.id} {status}")


def _runs_fail(
    workspace_root: Path,
    run_id: str,
    *,
    agent_id: str | None,
    summary: str,
    errors: list[str],
) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    run, _envelope = store.fail(run_id, summary=summary, errors=errors)
    print(f"Run failed: {run.id}")


def _runs_cancel(
    workspace_root: Path,
    run_id: str,
    *,
    agent_id: str | None,
    summary: str,
    safe_to_resume: bool,
) -> None:
    store, _agent = _run_store_for(workspace_root, agent_id)
    run, _envelope = store.cancel(run_id, summary=summary, safe_to_resume=safe_to_resume)
    print(f"Run cancelled: {run.id}")


def _runs_coordinate(
    workspace_root: Path,
    *,
    source_run_id: str,
    agent_id: str | None,
    affected_run_ids: list[str],
    impact: str,
    requested_action: str,
) -> None:
    store, agent = _coordination_store_for(workspace_root, agent_id)
    event = store.request(
        parent_agent_id=agent.id,
        source_run_id=source_run_id,
        affected_run_ids=affected_run_ids,
        impact=impact,
        requested_action=requested_action,
    )
    print(f"Coordination requested: {event.id}")


def _runs_coordination_list(
    workspace_root: Path,
    *,
    agent_id: str | None,
    status: str | None,
) -> None:
    store, _agent = _coordination_store_for(workspace_root, agent_id)
    events = store.list(status=status)
    if not events:
        print("No coordination events.")
        return
    for event in events:
        affected = ",".join(event.affected_run_ids)
        print(f"{event.id} {event.status} from={event.source_run_id} affects={affected} {event.impact}")


def _runs_coordination_ack(workspace_root: Path, coordination_id: str, *, agent_id: str | None) -> None:
    store, _agent = _coordination_store_for(workspace_root, agent_id)
    event = store.acknowledge(coordination_id)
    print(f"Coordination acknowledged: {event.id}")


def _runs_coordination_resolve(workspace_root: Path, coordination_id: str, *, agent_id: str | None) -> None:
    store, _agent = _coordination_store_for(workspace_root, agent_id)
    event = store.resolve(coordination_id)
    print(f"Coordination resolved: {event.id}")


def _runs_request_approval(
    workspace_root: Path,
    *,
    run_id: str,
    agent_id: str | None,
    type: str,
    reason: str,
    scope_items: list[str] | None,
    safe_to_resume: bool,
) -> None:
    run_store, agent = _run_store_for(workspace_root, agent_id)
    approval_store, _agent = _run_approval_store_for(workspace_root, agent.id)
    requested_scope = _scope_items(scope_items)
    try:
        run_store.validate_approval_request(
            run_id,
            type=type,
            requested_scope=requested_scope,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    run_store.checkpoint(
        run_id,
        summary=f"Blocked on {type} approval: {reason}",
        requested_followups=["Parent approval required before continuing."],
        safe_to_resume=safe_to_resume,
    )
    approval = approval_store.request(
        parent_agent_id=agent.id,
        run_id=run_id,
        type=type,
        reason=reason,
        requested_scope=requested_scope,
    )
    print(f"Run approval requested: {approval.id}")


def _runs_approvals_list(workspace_root: Path, *, agent_id: str | None, status: str | None) -> None:
    store, _agent = _run_approval_store_for(workspace_root, agent_id)
    approvals = store.list(status=status)
    if not approvals:
        print("No run approvals.")
        return
    for approval in approvals:
        print(f"{approval.id} {approval.status} run={approval.run_id} type={approval.type} {approval.reason}")


def _runs_approvals_approve(workspace_root: Path, approval_id: str, *, agent_id: str | None) -> None:
    store, _agent = _run_approval_store_for(workspace_root, agent_id)
    approval = store.approve(approval_id)
    print(f"Run approval approved: {approval.id}")


def _runs_approvals_deny(workspace_root: Path, approval_id: str, *, agent_id: str | None) -> None:
    store, _agent = _run_approval_store_for(workspace_root, agent_id)
    approval = store.deny(approval_id)
    print(f"Run approval denied: {approval.id}")


def _scope_items(items: list[str] | None) -> dict[str, str]:
    scope = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Invalid scope item, expected key=value: {item}")
        key, value = item.split("=", 1)
        scope[key] = value
    return scope


def _run_store_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    return (
        RunStore(
            workspace / "agents" / agent.id / "runs.json",
            workspace_root=workspace,
            event_store=EventStore(event_stream),
        ),
        agent,
    )


def _coordination_store_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    return (
        RunCoordinationStore(
            workspace / "agents" / agent.id / "run_coordination.json",
            event_store=EventStore(event_stream),
        ),
        agent,
    )


def _run_approval_store_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    return (
        RunApprovalStore(
            workspace / "agents" / agent.id / "run_approvals.json",
            event_store=EventStore(event_stream),
        ),
        agent,
    )


def _auth_status(provider: str, workspace_root: Path) -> None:
    if provider != "chatgpt":
        raise SystemExit(f"Unsupported auth provider: {provider}")
    record = load_chatgpt_auth(workspace_root)
    if record is None:
        print("ChatGPT auth: not connected")
        return
    expires = float(getattr(record, "expires", 0) or 0)
    remaining = int(max(0, expires - time.time()) // 60)
    if remaining:
        print(f"ChatGPT auth: connected, token expires in {remaining} min")
    else:
        print("ChatGPT auth: token expired or expiry unknown")


def _auth_logout(provider: str, workspace_root: Path) -> None:
    if provider != "chatgpt":
        raise SystemExit(f"Unsupported auth provider: {provider}")
    if clear_chatgpt_auth(workspace_root):
        print("ChatGPT auth: credential removed")
    else:
        print("ChatGPT auth: no credential found")


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
    agent = _resolve_agent(bundle, agent_id)
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


def _scheduler_schedule_dream(
    workspace_root: Path,
    *,
    agent_id: str | None,
    delay_seconds: int,
    interval_seconds: int | None,
    skills: list[str] | None,
) -> None:
    store, agent = _job_store_for(workspace_root, agent_id)
    arguments: dict[str, object] = {}
    if skills:
        arguments["skills"] = skills
    job = store.schedule(
        name="dreaming.run",
        owner="skill:dreaming",
        run_at=utc_now() + timedelta(seconds=delay_seconds),
        interval_seconds=interval_seconds,
        payload={
            "agent_id": agent.id,
            "session_id": "dreaming",
            "arguments": arguments,
        },
    )
    print(f"Scheduled {job.name}: {job.id}")


def _scheduler_run_once(
    workspace_root: Path,
    *,
    agent_id: str | None,
    limit: int | None,
) -> None:
    store, agent = _job_store_for(workspace_root, agent_id)
    handlers = _scheduler_handlers(workspace_root, agent.id)
    results = JobRunner(store, handlers).run_due(limit=limit)
    if not results:
        print("No due jobs.")
        return
    for job in results:
        print(f"{job.id} {job.status} {job.name}")


def _scheduler_serve(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
) -> None:
    store, agent = _job_store_for(workspace_root, agent_id)
    handlers = _scheduler_handlers(workspace_root, agent.id)
    service = SchedulerService(
        JobRunner(store, handlers),
        poll_interval_seconds=poll_seconds,
    )
    print(f"Maurice scheduler running for agent {agent.id}")
    try:
        service.run_forever()
    except KeyboardInterrupt:
        service.stop()
        print("Maurice scheduler stopped")


def _scheduler_serve_until_stopped(
    workspace_root: Path,
    agent_id: str | None,
    poll_seconds: float,
    stop_event: threading.Event,
) -> None:
    store, agent = _job_store_for(workspace_root, agent_id)
    handlers = _scheduler_handlers(workspace_root, agent.id)
    service = SchedulerService(
        JobRunner(store, handlers),
        poll_interval_seconds=poll_seconds,
        sleep=lambda seconds: stop_event.wait(seconds),
    )
    print(f"Scheduler started for agent {agent.id}")
    while not stop_event.is_set():
        service.run_forever(max_iterations=1)
        stop_event.wait(max(poll_seconds, 0.1))
    print("Scheduler stopped")


def _job_store_for(workspace_root: Path, agent_id: str | None):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    return (
        JobStore(
            workspace / "agents" / agent.id / "jobs.json",
            event_store=EventStore(event_stream),
        ),
        agent,
    )


def _scheduler_handlers(workspace_root: Path, agent_id: str):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    event_store = EventStore(event_stream)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    context = PermissionContext(
        workspace_root=bundle.host.workspace_root,
        runtime_root=bundle.host.runtime_root,
        maurice_home_root=str(maurice_home()),
    )
    registry = SkillLoader(
        bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        available_credentials=credentials.credentials.keys(),
        event_store=event_store,
        agent_id=agent.id,
        session_id="scheduler",
    ).load()
    dreaming = dreaming_tool_executors(
        context,
        registry,
        event_store=event_store,
        dream_input_builders={"memory": lambda: build_dream_input(context)},
    )
    def run_dream(job):
        arguments = job.payload.get("arguments", {})
        result = dreaming["dreaming.run"](arguments if isinstance(arguments, dict) else {})
        if not result.ok:
            raise RuntimeError(result.summary)
        return result

    def run_reminder(job):
        arguments = job.payload.get("arguments", {})
        result = fire_reminder(arguments if isinstance(arguments, dict) else {}, context, event_store=event_store)
        if not result.ok:
            raise RuntimeError(result.summary)
        return result

    return {"dreaming.run": run_dream, "reminders.fire": run_reminder}


def _schedule_reminder_callback(workspace: Path, agent_id: str):
    def schedule(payload: dict[str, object]) -> str:
        store = JobStore(workspace / "agents" / agent_id / "jobs.json")
        job = store.schedule(
            name="reminders.fire",
            owner="skill:reminders",
            run_at=payload["run_at"],
            payload={
                "agent_id": agent_id,
                "session_id": "reminders",
                "arguments": {
                    "reminder_id": payload["reminder_id"],
                },
            },
        )
        return job.id

    return schedule


def _cancel_job_callback(workspace: Path, agent_id: str):
    def cancel(job_id: str) -> None:
        try:
            JobStore(workspace / "agents" / agent_id / "jobs.json").cancel(job_id)
        except KeyError:
            return

    return cancel


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
    router, _agent, bundle = _gateway_router_for(workspace_root, agent_id)
    channels = ChannelAdapterRegistry.from_config(
        bundle.host.channels,
        default_agent_id=_agent.id,
    )
    event_stream = (
        Path(_agent.event_stream)
        if _agent.event_stream
        else Path(bundle.host.workspace_root) / "agents" / _agent.id / "events.jsonl"
    )
    server = GatewayHttpServer(
        host=host or bundle.host.gateway.host,
        port=port or bundle.host.gateway.port,
        router=router,
        channels=channels,
        event_store=EventStore(event_stream),
    )
    bound_host, bound_port = server.address
    print(f"Maurice gateway listening on http://{bound_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Maurice gateway stopped")
    finally:
        server.shutdown()


def _gateway_telegram_poll(
    workspace_root: Path,
    *,
    agent_id: str | None,
    once: bool,
    poll_seconds: float,
) -> None:
    bundle = load_workspace_config(workspace_root)
    telegram = bundle.host.channels.get("telegram")
    if not isinstance(telegram, dict) or not telegram.get("enabled", True):
        raise SystemExit("Telegram channel is not configured. Run onboarding first.")
    credential_name = str(telegram.get("credential") or "telegram_bot")
    workspace = Path(bundle.host.workspace_root)
    token = _credential_value(workspace, credential_name)
    if not token:
        raise SystemExit("Telegram bot token is missing. Run onboarding first.")
    target_agent = agent_id or str(telegram.get("agent") or "main")
    router, agent, bundle = _gateway_router_for(workspace, target_agent)
    event_store = EventStore(Path(agent.event_stream))
    offset_path = Path(bundle.host.workspace_root) / "agents" / agent.id / "telegram.offset"
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


def _telegram_poll_until_stopped(
    workspace_root: Path,
    agent_id: str | None,
    poll_seconds: float,
    stop_event: threading.Event,
) -> None:
    bundle = load_workspace_config(workspace_root)
    telegram = bundle.host.channels.get("telegram")
    if not isinstance(telegram, dict) or not telegram.get("enabled", True):
        print("Telegram channel is not configured.")
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
    offset_path = Path(bundle.host.workspace_root) / "agents" / agent.id / "telegram.offset"
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


def _telegram_start_chat_action(token: str, chat_id: int):
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.is_set():
            try:
                _telegram_send_chat_action(token, chat_id, "typing")
            except Exception:
                return
            stop_event.wait(4)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=0.2)

    return stop


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
        )

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


def _doctor_workspace(workspace_root: Path) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    ensure_workspace_credentials_migrated(workspace)
    ensure_workspace_config_migrated(workspace)
    ensure_workspace_content_migrated(workspace)
    required_dirs = ["agents", "skills", "sessions", "content"]
    missing = [name for name in required_dirs if not (workspace / name).is_dir()]
    if missing:
        raise SystemExit(f"Maurice doctor: missing workspace dirs: {', '.join(missing)}")
    if not workspace_skills_config_path(workspace).is_file():
        raise SystemExit("Maurice doctor: missing workspace skills.yaml")
    print(f"Maurice doctor: workspace OK ({workspace})")
    print(f"Maurice credentials: {credentials_path()}")
    default_agent = _default_agent(bundle)
    print(f"Maurice model: {_effective_model_label(bundle, default_agent)}")
    if default_agent is not None and default_agent.model:
        print(
            f"Maurice model note: {default_agent.id} overrides kernel.model; "
            "kernel.model is only the fallback default."
        )


def run_one_turn(
    *,
    workspace_root: Path,
    message: str,
    session_id: str,
    agent_id: str | None = None,
) -> TurnResult:
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream)
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    permission_context = PermissionContext(
        workspace_root=bundle.host.workspace_root,
        runtime_root=bundle.host.runtime_root,
        maurice_home_root=str(maurice_home()),
    )
    event_store = EventStore(event_stream)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    registry = SkillLoader(
        bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        available_credentials=credentials.credentials.keys(),
        event_store=event_store,
        agent_id=agent.id,
        session_id=session_id,
    ).load()
    model_config = _effective_model_config(bundle, agent)
    provider = _provider_for_config(bundle, message, credentials, agent=agent)
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(workspace / "sessions"),
        event_store=event_store,
        permission_context=permission_context,
        permission_profile=agent.permission_profile,
        tool_executors={
            **filesystem_tool_executors(permission_context),
            **memory_tool_executors(permission_context),
            **dreaming_tool_executors(
                permission_context,
                registry,
                event_store=event_store,
                dream_input_builders={
                    "memory": lambda: build_dream_input(permission_context)
                },
            ),
            **skill_authoring_tool_executors(
                permission_context,
                bundle.host.skill_roots,
                enabled_skills=agent.skills or bundle.kernel.skills,
            ),
            **self_update_tool_executors(permission_context),
            **web_tool_executors(
                permission_context,
                bundle.skills.skills.get("web", {}),
            ),
            **host_tool_executors(
                permission_context,
                agent_id=agent.id,
                session_id=session_id,
            ),
            **reminders_tool_executors(
                permission_context,
                event_store=event_store,
                schedule_reminder=_schedule_reminder_callback(workspace, agent.id),
                cancel_job=_cancel_job_callback(workspace, agent.id),
            ),
            **vision_tool_executors(
                permission_context,
                config=bundle.skills.skills.get("vision", {}),
            ),
        },
        approval_store=ApprovalStore(
            workspace / "agents" / agent.id / "approvals.json",
            event_store=event_store,
        ),
        model=str(model_config.get("name") or bundle.kernel.model.name),
        system_prompt=_agent_system_prompt(workspace),
    )
    return loop.run_turn(
        agent_id=agent.id,
        session_id=session_id,
        message=message,
    )


def _resolve_agent(bundle: ConfigBundle, agent_id: str | None):
    if agent_id:
        try:
            agent = bundle.agents.agents[agent_id]
        except KeyError as exc:
            raise SystemExit(f"Unknown agent: {agent_id}") from exc
        if agent.status != "active":
            raise SystemExit(f"Agent is not active: {agent_id} ({agent.status})")
        return agent
    for agent in bundle.agents.agents.values():
        if agent.default and agent.status == "active":
            return agent
    try:
        agent = bundle.agents.agents["main"]
    except KeyError as exc:
        raise SystemExit("No default agent configured") from exc
    if agent.status != "active":
        raise SystemExit("No active default agent configured")
    return agent


def _agent_system_prompt(workspace: Path) -> str:
    content = workspace / "content"
    return (
        "Maurice kernel turn loop.\n"
        f"Workspace root: {workspace}\n"
        f"User content root: {content}\n"
        "Use the workspace `content/` directory as the default place for user-facing files, folders, exports, drafts, reports, and other produced content. "
        "When the user names a relative folder or file without another base path, resolve it under `content/`. "
        "For example, if the user says `ouvre le dossier toto`, inspect `$workspace/content/toto`. "
        "Do not put secrets or host configuration in `content/`."
    )


def _provider_for_config(
    bundle: ConfigBundle,
    message: str,
    credentials: CredentialsStore | None = None,
    *,
    agent=None,
):
    model = _effective_model_config(bundle, agent)
    provider_name = model["provider"]
    if provider_name == "mock":
        return MockProvider(
            [
                {"type": "text_delta", "delta": f"Mock response: {message}"},
                {"type": "status", "status": "completed"},
            ]
        )
    if provider_name == "api":
        protocol = model.get("protocol")
        if not protocol:
            return UnsupportedProvider(
                code="missing_protocol",
                message="API provider requires kernel.model.protocol.",
            )
        credential = _model_credential(model, credentials)
        return ApiProvider(
            protocol=protocol,
            api_key=credential.value if credential is not None else None,
            base_url=(
                model.get("base_url")
                or (credential.base_url if credential is not None else None)
            ),
        )
    if provider_name == "auth":
        protocol = model.get("protocol") or "unknown"
        if protocol == "chatgpt_codex":
            credential_name = model.get("credential") or CHATGPT_CREDENTIAL_NAME
            if agent is not None and "*" not in agent.credentials and credential_name not in agent.credentials:
                return UnsupportedProvider(
                    code="credential_not_allowed",
                    message=f"Agent is not allowed to use credential: {credential_name}",
                )
            token = get_valid_chatgpt_access_token(
                bundle.host.workspace_root,
                credential_name=credential_name,
            )
            if not token:
                return UnsupportedProvider(
                    code="auth_missing",
                    message="ChatGPT auth requires a stored credential.",
                )
            credential = _model_credential(model, credentials)
            return ChatGPTCodexProvider(
                token=token,
                base_url=(
                    model.get("base_url")
                    or (credential.base_url if credential is not None else None)
                    or "https://chatgpt.com/backend-api/codex"
                ),
            )
        return UnsupportedProvider(
            code="auth_provider_not_implemented",
            message=f"Auth provider protocol is not implemented yet: {protocol}",
        )
    if provider_name == "openai":
        credential = (
            credentials.credentials.get(str(model.get("credential") or "openai"))
            if credentials is not None
            else None
        )
        return OpenAICompatibleProvider(
            api_key=credential.value if credential is not None else None,
            base_url=(
                model.get("base_url")
                or (credential.base_url if credential is not None else None)
                or "https://api.openai.com/v1"
            ),
        )
    if provider_name == "ollama":
        credential = (
            credentials.credentials.get(str(model.get("credential")))
            if credentials is not None and model.get("credential")
            else None
        )
        return OllamaCompatibleProvider(
            base_url=str(model.get("base_url") or "http://localhost:11434"),
            api_key=credential.value if credential is not None else "",
        )
    raise SystemExit(f"Unsupported provider: {provider_name}")


def _effective_model_config(bundle: ConfigBundle, agent=None) -> dict[str, Any]:
    agent_model = getattr(agent, "model", None)
    if agent_model:
        return dict(agent_model)
    return bundle.kernel.model.model_dump(mode="json")


def _model_credential(model: dict[str, Any], credentials: CredentialsStore | None):
    if credentials is None:
        return None
    name = model.get("credential")
    if not name and model.get("protocol") == "openai_chat_completions":
        name = "openai"
    if not name:
        return None
    return credentials.credentials.get(name)


if __name__ == "__main__":
    raise SystemExit(main())
