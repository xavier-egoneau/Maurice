"""Command line entrypoint for Maurice."""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import signal
import secrets
import subprocess
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agent_wizard import (
    clear_agent_creation_wizard,
    handle_agent_creation_wizard,
)
from maurice.host.commands.agents import (
    _agents_list, _agents_create, _agents_update,
    _agents_disable, _agents_archive, _agents_delete,
)
from maurice.host.commands.models import (
    _models_add,
    _models_assign,
    _models_default,
    _models_list,
    _models_worker,
)
from maurice.host.commands.auth import _auth_login, _auth_status, _auth_logout
from maurice.host.commands.approvals import _approvals_list, _approvals_resolve, _approval_store_for
from maurice.host.commands.misc import (
    _migration_inspect, _migration_run, _print_migration_report,
    _self_update_list, _self_update_validate, _self_update_test, _self_update_apply,
    _monitor_snapshot, _monitor_events,
)
from maurice.host.commands.scheduler import (
    _scheduler_schedule_dream, _scheduler_configure, _scheduler_run_once,
    _scheduler_serve, _scheduler_serve_until_stopped, _job_store_for,
    _ensure_configured_scheduler_jobs, _ensure_recurring_job,
    _cancel_recurring_job_kind, _next_local_time, _normalize_local_time_or_exit,
    _scheduler_handlers,
)
from maurice.host.commands.gateway_server import (
    _gateway_local_message, _gateway_serve, _gateway_serve_until_stopped,
    _build_gateway_http_server, _build_gateway_http_server_for_context, _gateway_telegram_poll,
    _gateway_web_agents, _gateway_web_session_history,
    _looks_like_internal_gateway_message, _gateway_web_session_reset,
    _telegram_poll_until_stopped, _telegram_poll_once,
    _gateway_router_for, _reset_gateway_session, _record_gateway_exchange,
    _compact_gateway_session, _gateway_model_summary, _WebTelegramPollers,
)
from maurice.host.context import MauriceContext, resolve_global_context, resolve_local_context
from maurice.host.commands.dashboard import (
    _dashboard, _dashboard_textual, _dashboard_rich,
    _rich_table, _cluster_rows, _log_level_text,
    _model_rows, _security_rows, _event_rows, _render_dashboard,
    _dashboard_status_line, _dashboard_table, _job_rows,
    _skill_rows, _active_agents, _compact_counts, _agent_model_name,
)
from maurice.host.commands.service import (
    _install, _service_status, _service_logs,
    _start_services, _start_services_daemon,
    _stop_services, _restart_services_daemon,
    _wait_for_stop, _pid_path, _write_pid, _read_pid,
    _remove_pid, _pid_is_running, _doctor,
)
from maurice.host.commands.onboard import (
    _onboarding_existing_values, _onboarding_provider_choice,
    _model_existing_values, _ask_model_config, _onboard_agent, _onboard_agent_model,
    _write_kernel_model, _onboard_interactive, _write_onboarding_config,
    _existing_config, _existing_search_config, _ask_telegram_config,
    _csv_default, _ask_csv_ints, _ask_csv_strings,
    _save_api_credential, _save_token_credential, _credential_exists,
    _ask, _ask_secret, _ask_api_key, _ask_int, _ask_choice,
    _ask_provider_choice, _ask_ollama_deployment, _ask_model_from_choices,
    _chatgpt_model_choices, _ollama_model_choices, _is_local_ollama_url,
    _format_bytes, _real_provider_default, _ask_yes_no,
)
from maurice.host.setup import run_setup
from maurice.host.output import (
    _yes_no, _status_marker, _short, _ansi_padding, _compact_text,
    _supports_color, _color, _print_title, _print_dim, _print_host_checks,
)
from maurice.host.telegram import (
    _credential_value, _telegram_channel_configured, _telegram_channel_configs,
    _telegram_channel_for_agent, _telegram_offset_path,
    _validate_telegram_first_message, _telegram_get_updates,
    _telegram_bot_username, _telegram_send_message, _telegram_send_chat_action,
    _telegram_api_json, _telegram_update_to_inbound,
    _int_list, _read_int_file, _write_int_file, _redact_secret,
    _telegram_sender_ids, _telegram_start_chat_action,
)
from maurice.host.delivery import (
    _schedule_reminder_callback, _deliver_reminder_result,
    _build_daily_digest, _latest_dream_report, _human_datetime,
    _deliver_daily_digest, _emit_daily_event, _cancel_job_callback,
)
from maurice.host.errors import AgentError, ProviderError
from maurice.host.runtime import (
    run_one_turn, _resolve_agent, _agent_system_prompt,
    _active_dev_project_path, _provider_for_config,
    _effective_model_config, _model_credential,
    _effective_model_label, _default_agent,
)
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
from maurice.host.client import MauriceClient
from maurice.host.command_registry import CommandRegistry
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
from maurice.host.model_catalog import (
    chatgpt_model_choices,
    format_bytes,
    ollama_model_choices,
)
from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.paths import (
    agents_config_path,
    ensure_workspace_config_migrated,
    host_config_path,
    kernel_config_path,
    maurice_home,
    workspace_skills_config_path,
)
from maurice.host.project import global_config_path, resolve_project_root
from maurice.host.project_registry import record_seen_project
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
from maurice.kernel.compaction import CompactionConfig
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
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.system_skills.reminders.tools import fire_reminder


PROVIDER_HELP = {
    "chatgpt": ("ChatGPT", "connexion via ton abonnement ChatGPT, sans cle API OpenAI"),
    "openai_api": ("API compatible OpenAI", "URL + cle API, pour OpenAI ou un provider compatible"),
    "ollama": ("Ollama", "modele local ou serveur Ollama"),
}
DEFAULT_WORKSPACE = Path.home() / "Documents" / "workspace_maurice"


def _resolve_service_workspace(workspace_arg: str | None, *, command: str) -> Path:
    if workspace_arg:
        return Path(workspace_arg)
    config = read_yaml_file(global_config_path())
    usage = config.get("usage") if isinstance(config.get("usage"), dict) else {}
    mode = str(usage.get("mode") or "local")
    configured_workspace = usage.get("workspace")
    if mode == "global":
        return Path(str(configured_workspace)).expanduser() if configured_workspace else DEFAULT_WORKSPACE
    raise SystemExit(
        f"`maurice {command}` démarre l'assistant de bureau, mais Maurice est réglé pour démarrer dans un dossier.\n"
        "Utilise `maurice` ou `maurice chat` dans un dossier.\n"
        "Pour passer au contexte global, lance `maurice setup`, choisis `global`, "
        "puis relance `maurice start`.\n"
        "Tu peux aussi passer explicitement `--workspace /chemin/du/workspace-global` "
        "pour démarrer un daemon global ponctuel."
    )


def _resolve_web_context(*, dir_arg: str | None, workspace_arg: str | None) -> MauriceContext:
    if dir_arg and workspace_arg:
        raise SystemExit("Utilise soit `--dir` pour un contexte local, soit `--workspace` pour un contexte global.")
    if workspace_arg:
        return resolve_global_context(
            Path(workspace_arg),
            active_project=resolve_project_root(Path.cwd(), confirm=False),
        )
    if dir_arg:
        return resolve_local_context(Path(dir_arg))

    config = read_yaml_file(global_config_path())
    usage = config.get("usage") if isinstance(config.get("usage"), dict) else {}
    mode = str(usage.get("mode") or "local")
    if mode == "global":
        workspace = Path(str(usage.get("workspace") or DEFAULT_WORKSPACE)).expanduser()
        return resolve_global_context(
            workspace,
            active_project=resolve_project_root(Path.cwd(), confirm=False),
        )

    project_root = resolve_project_root(Path.cwd(), confirm=True)
    if project_root is None:
        raise SystemExit("Aucun dossier de travail choisi.")
    return resolve_local_context(project_root)


def _web_chat(
    *,
    dir_arg: str | None,
    workspace_arg: str | None,
    agent_id: str | None,
    host: str,
    port: int,
    open_browser: bool,
) -> None:
    ctx = _resolve_web_context(dir_arg=dir_arg, workspace_arg=workspace_arg)
    if ctx.scope == "local" and agent_id not in {None, "main"}:
        raise SystemExit("Le contexte local expose seulement l'agent `main`.")
    if ctx.active_project_root is not None:
        record_seen_project(ctx.agent_workspace_root, ctx.active_project_root)
    token = secrets.token_urlsafe(24)
    telegram_pollers = _web_telegram_pollers_for_context(ctx, agent_id=agent_id)
    if telegram_pollers is not None:
        telegram_pollers.sync()
    try:
        server = _build_gateway_http_server_for_context(
            ctx,
            agent_id=agent_id,
            host=host,
            port=port,
            web_token=token,
            telegram_pollers=telegram_pollers,
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"Le port {port} est déjà utilisé. Relance avec `maurice web --port 0` "
                "pour choisir un port libre automatiquement."
            ) from exc
        raise
    bound_host, bound_port = server.address
    url_host = "127.0.0.1" if bound_host in {"", "0.0.0.0", "::"} else bound_host
    if ":" in url_host and not url_host.startswith("["):
        url_host = f"[{url_host}]"
    url = f"http://{url_host}:{bound_port}?token={token}"
    print(f"Maurice web chat ({ctx.scope}) listening on {url}")
    if ctx.active_project_root is not None:
        print(f"Working directory: {ctx.active_project_root}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Maurice web chat stopped")
    finally:
        server.shutdown()
        if telegram_pollers is not None:
            telegram_pollers.stop()


def _web_telegram_pollers_for_context(
    ctx: MauriceContext,
    *,
    agent_id: str | None,
) -> _WebTelegramPollers | None:
    if ctx.scope != "global":
        return None
    if MauriceClient(ctx).is_running():
        return None
    return _WebTelegramPollers(ctx.context_root, agent_id=agent_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maurice",
        description="Maurice: un assistant, ouvert sur un dossier ou disponible comme assistant de bureau.",
        epilog=(
            "Parcours rapides:\n"
            "  Dossier : cd /chemin/du/projet && maurice\n"
            "  Web    : maurice web\n"
            "  Bureau : maurice setup  # choisir global, puis maurice start\n"
            "\n"
            "Le contexte global n'est pas un second produit: il utilise la même brique agentique "
            "avec un workspace et une mémoire centralisés, comme un niveau au-dessus du dossier."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--session",
        default="default",
        help="Session id for interactive chat when no subcommand is provided.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")
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
    setup_parser = subparsers.add_parser("setup", help="Configure context level, provider, permissions, and workspace.")
    setup_parser.add_argument(
        "--show-mock",
        action="store_true",
        help="Show the mock provider option for tests and runtime development.",
    )
    start_parser = subparsers.add_parser("start", help="Start Maurice background services.")
    start_parser.add_argument("--workspace", help="Global workspace root to use.")
    start_parser.add_argument("--agent", help="Agent id to use. Defaults to configured agents.")
    start_parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval.")
    start_parser.add_argument("--no-telegram", action="store_true", help="Do not start Telegram polling.")
    start_parser.add_argument("--no-scheduler", action="store_true", help="Do not start the scheduler.")
    start_parser.add_argument("--no-gateway", action="store_true", help="Do not start the local HTTP gateway.")
    start_parser.add_argument("--no-browser", action="store_true", help="Do not open the browser after start.")
    start_parser.add_argument("--foreground", action="store_true", help="Keep services attached to this terminal.")
    stop_parser = subparsers.add_parser("stop", help="Stop a running Maurice instance.")
    stop_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    restart_parser = subparsers.add_parser("restart", help="Restart Maurice background services.")
    restart_parser.add_argument("--workspace", help="Global workspace root to use.")
    restart_parser.add_argument("--agent", help="Agent id to use. Defaults to configured agents.")
    restart_parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval.")
    restart_parser.add_argument("--no-telegram", action="store_true", help="Do not start Telegram polling.")
    restart_parser.add_argument("--no-scheduler", action="store_true", help="Do not start the scheduler.")
    restart_parser.add_argument("--no-gateway", action="store_true", help="Do not start the local HTTP gateway.")
    restart_parser.add_argument("--no-browser", action="store_true", help="Do not open the browser after restart.")
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

    chat_parser = subparsers.add_parser("chat", help="Start an interactive REPL in the current directory.")
    chat_parser.add_argument("--session", default="default", help="Session id to use.")
    chat_parser.add_argument("--dir", help="Project root (defaults to CWD).")
    web_parser = subparsers.add_parser("web", help="Open a browser chat for the configured context.")
    web_parser.add_argument("--dir", help="Project root for a local browser chat.")
    web_parser.add_argument("--workspace", help="Global workspace root for a global browser chat.")
    web_parser.add_argument("--agent", help="Agent id for global browser chat. Local chat uses main.")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Defaults to 127.0.0.1.")
    web_parser.add_argument("--port", type=int, default=0, help="Port to bind. Defaults to a free port.")
    web_parser.add_argument("--no-browser", action="store_true", help="Do not open the browser.")

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

    models_parser = subparsers.add_parser("models", help="Manage central model profiles.")
    models_subparsers = models_parser.add_subparsers(dest="models_command")
    models_list = models_subparsers.add_parser("list", help="List model profiles.")
    models_list.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    models_add = models_subparsers.add_parser("add", help="Add or update a model profile.")
    models_add.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    models_add.add_argument("--id", dest="profile_id", help="Profile id. Defaults to provider + model name.")
    models_add.add_argument("--provider", required=True, help="Provider id: mock, api, auth, openai, ollama.")
    models_add.add_argument("--protocol", help="Provider protocol, for example openai_chat_completions.")
    models_add.add_argument("--name", required=True, help="Model name.")
    models_add.add_argument("--base-url", help="Provider base URL.")
    models_add.add_argument("--credential", help="Credential name used by this profile.")
    models_add.add_argument("--tier", choices=["high", "middle", "low"], help="Optional quality/cost tier.")
    models_add.add_argument(
        "--capability",
        action="append",
        dest="capabilities",
        help="Capability tag, for example text, tools, vision. May be provided more than once.",
    )
    models_add.add_argument("--privacy", choices=["local", "cloud", "unknown"], help="Data locality hint.")
    models_add.add_argument("--default", action="store_true", help="Make this the default model profile.")
    models_default = models_subparsers.add_parser("default", help="Set the default model profile.")
    models_default.add_argument("profile_id", help="Existing model profile id.")
    models_default.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    models_assign = models_subparsers.add_parser("assign", help="Assign an ordered model fallback chain to an agent.")
    models_assign.add_argument("agent_id", help="Agent id to update.")
    models_assign.add_argument("model_chain", nargs="+", help="Ordered model profile ids.")
    models_assign.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")
    models_worker = models_subparsers.add_parser("worker", help="Assign the dev worker model chain for an agent.")
    models_worker.add_argument("agent_id", help="Agent id to update.")
    models_worker.add_argument("model_chain", nargs="*", help="Worker model profile ids. Empty = use the parent agent model chain.")
    models_worker.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Workspace root to use.")

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
    scheduler_configure = scheduler_subparsers.add_parser("configure", help="Configure daily automation times.")
    scheduler_configure.add_argument("--workspace", required=True, help="Workspace root to use.")
    scheduler_configure.add_argument("--dream-time", help="Local dreaming time, for example 09:00 or 9h.")
    scheduler_configure.add_argument("--daily-time", help="Local daily time, for example 09:30 or 9h30.")
    scheduler_configure.add_argument("--disable-dreaming", action="store_true", help="Disable scheduled dreaming.")
    scheduler_configure.add_argument("--disable-daily", action="store_true", help="Disable scheduled daily.")
    scheduler_configure.add_argument("--enable-dreaming", action="store_true", help="Enable scheduled dreaming.")
    scheduler_configure.add_argument("--enable-daily", action="store_true", help="Enable scheduled daily.")
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


def _build_onboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maurice onboard",
        description="Deprecated compatibility alias for legacy workspace onboarding.",
    )
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help=f"Workspace root to create or update. Defaults to {DEFAULT_WORKSPACE}.",
    )
    parser.add_argument(
        "--agent",
        nargs="?",
        const="",
        help="Onboard a new durable agent. If omitted after --agent, the wizard asks for the id.",
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="With --agent, update only this agent's model configuration.",
    )
    parser.add_argument(
        "--permission-profile",
        choices=["safe", "limited", "power"],
        default="safe",
        help="Default permission profile for the main agent.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask for model, gateway, and provider basics after workspace creation.",
    )
    return parser


def _run_onboard_command(args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "onboard":
        return _run_onboard_command(_build_onboard_parser().parse_args(raw_argv[1:]))

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.command == "doctor":
        _doctor(workspace_root=Path(args.workspace) if args.workspace else None)
        return 0

    if args.command == "install":
        _install(workspace_root=Path(args.workspace) if args.workspace else None)
        return 0

    if args.command == "setup":
        if args.show_mock:
            os.environ["MAURICE_SETUP_SHOW_MOCK"] = "1"
        try:
            return 0 if run_setup() else 1
        except (EOFError, KeyboardInterrupt):
            print("\nAnnulé.", file=sys.stderr)
            return 1

    if args.command == "run":
        active_project = resolve_project_root(Path.cwd(), confirm=False)
        try:
            result = run_one_turn(
                workspace_root=Path(args.workspace),
                message=args.message,
                session_id=args.session,
                agent_id=args.agent,
                source_metadata=(
                    {"active_project_root": str(active_project)}
                    if active_project is not None
                    else None
                ),
            )
        except (AgentError, ProviderError) as exc:
            raise SystemExit(str(exc)) from exc
        if result.assistant_text:
            print(result.assistant_text)
        for tool_result in result.tool_results:
            print(tool_result.summary)
        if result.status != "completed":
            detail = f": {result.error}" if result.error else ""
            raise SystemExit(f"Turn failed{detail}")
        return 0

    if args.command == "start":
        workspace = _resolve_service_workspace(args.workspace, command="start")
        if args.foreground:
            _start_services(
                workspace,
                agent_id=args.agent,
                poll_seconds=args.poll_seconds,
                telegram=not args.no_telegram,
                scheduler=not args.no_scheduler,
                gateway=not args.no_gateway,
                open_browser=not args.no_browser,
            )
        else:
            _start_services_daemon(
                workspace,
                agent_id=args.agent,
                poll_seconds=args.poll_seconds,
                telegram=not args.no_telegram,
                scheduler=not args.no_scheduler,
                gateway=not args.no_gateway,
                open_browser=not args.no_browser,
            )
        return 0

    if args.command == "stop":
        _stop_services(Path(args.workspace))
        return 0

    if args.command == "restart":
        workspace = _resolve_service_workspace(args.workspace, command="restart")
        _restart_services_daemon(
            workspace,
            agent_id=args.agent,
            poll_seconds=args.poll_seconds,
            telegram=not args.no_telegram,
            scheduler=not args.no_scheduler,
            gateway=not args.no_gateway,
            open_browser=not args.no_browser,
        )
        return 0

    if args.command == "web":
        _web_chat(
            dir_arg=args.dir,
            workspace_arg=args.workspace,
            agent_id=args.agent,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
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

    if args.command == "models":
        if args.models_command == "list":
            _models_list(Path(args.workspace))
            return 0
        if args.models_command == "add":
            _models_add(
                Path(args.workspace),
                profile_id=args.profile_id,
                provider=args.provider,
                protocol=args.protocol,
                name=args.name,
                base_url=args.base_url,
                credential=args.credential,
                tier=args.tier,
                capabilities=args.capabilities,
                privacy=args.privacy,
                make_default=args.default,
            )
            return 0
        if args.models_command == "default":
            _models_default(Path(args.workspace), profile_id=args.profile_id)
            return 0
        if args.models_command == "assign":
            _models_assign(Path(args.workspace), agent_id=args.agent_id, model_chain=args.model_chain)
            return 0
        if args.models_command == "worker":
            _models_worker(Path(args.workspace), agent_id=args.agent_id, model_chain=args.model_chain)
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
        if args.scheduler_command == "configure":
            _scheduler_configure(
                Path(args.workspace),
                dream_time=args.dream_time,
                daily_time=args.daily_time,
                enable_dreaming=args.enable_dreaming,
                disable_dreaming=args.disable_dreaming,
                enable_daily=args.enable_daily,
                disable_daily=args.disable_daily,
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

    if args.command in (None, "chat"):
        from maurice.host.repl import launch
        cwd = Path(args.dir).resolve() if hasattr(args, "dir") and args.dir else None
        session_id = args.session if hasattr(args, "session") else "default"
        return launch(cwd=cwd, session_id=session_id)

    parser.print_help()
    return 0
















_ASCII_MAURICE = """\
[#E8761A]███╗   ███╗ █████╗ ██╗   ██╗██████╗ ██╗ ██████╗███████╗[/]
[#E8761A]████╗ ████║██╔══██╗██║   ██║██╔══██╗██║██╔════╝██╔════╝[/]
[#DA6010]██╔████╔██║███████║██║   ██║██████╔╝██║██║     █████╗  [/]
[#C04E08]██║╚██╔╝██║██╔══██║██║   ██║██╔══██╗██║██║     ██╔══╝  [/]
[#A03A00]██║ ╚═╝ ██║██║  ██║╚██████╔╝██║  ██║██║╚██████╗███████╗[/]
[#7A2A00]╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝╚══════╝[/]"""






































































































def _credential_value(workspace: Path, name: str) -> str:
    record = load_workspace_credentials(workspace).credentials.get(name)
    return record.value if record is not None else ""
































def _credential_value(workspace: Path, name: str) -> str:
    credential = load_workspace_credentials(workspace).credentials.get(name)
    return credential.value if credential is not None else ""
































































































































































































































if __name__ == "__main__":
    raise SystemExit(main())
