"""Auto-split from cli.py."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
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
from maurice.host.agent_wizard import clear_agent_creation_wizard, handle_agent_creation_wizard
from maurice.host.agents import archive_agent, create_agent, delete_agent, disable_agent, list_agents, update_agent
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME, ChatGPTAuthFlow, clear_chatgpt_auth,
    get_valid_chatgpt_access_token, load_chatgpt_auth, save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.command_registry import CommandRegistry, default_command_registry
from maurice.host.client import _desired_server_meta, _server_meta_compatible
from maurice.host.context import MauriceContext, resolve_global_context
from maurice.host.credentials import (
    CredentialRecord, CredentialsStore, credentials_path,
    ensure_workspace_credentials_migrated, load_workspace_credentials, write_workspace_credentials,
)
from maurice.host.commands.gateway_server import _gateway_serve_until_stopped
from maurice.host.commands.scheduler import _scheduler_serve_until_stopped
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
    _supports_color, _color, _print_title, _print_dim, _print_host_checks,
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
from maurice.host.server import MauriceServer
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



def _start_services(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
    telegram: bool,
    scheduler: bool,
    gateway: bool,
    open_browser: bool = True,
) -> None:
    bundle = _load_or_initialize_service_bundle(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    ctx = _global_service_context(workspace, agent_id=agent_id, bundle=bundle)
    pid_path = _pid_path(workspace)
    existing_pid = _read_pid(pid_path)
    if existing_pid and _pid_is_running(existing_pid):
        _check_running_service_meta(ctx, existing_pid)
        raise SystemExit(f"Maurice already appears to be running with pid {existing_pid}.")
    _write_pid(pid_path, os.getpid())
    _write_service_meta(ctx, os.getpid())
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
        for channel_name, _channel_config in _telegram_channel_configs(bundle):
            if agent_id and _channel_config.get("agent") not in {None, agent_id}:
                continue
            workers.append(
                threading.Thread(
                    target=_telegram_poll_until_stopped,
                    args=(Path(bundle.host.workspace_root), agent_id, max(poll_seconds, 0.1), stop_event),
                    kwargs={"channel_name": channel_name},
                    daemon=True,
                )
            )
    if gateway:
        workers.append(
            threading.Thread(
                target=_gateway_serve_until_stopped,
                args=(Path(bundle.host.workspace_root), agent_id, max(poll_seconds, 0.1), stop_event),
                daemon=True,
            )
        )
    if not workers:
        _remove_service_state(ctx, pid_path)
        print("No Maurice services to start.")
        return
    server = MauriceServer(ctx)
    server_thread = threading.Thread(
        target=server.serve,
        args=(ctx.server_socket_path,),
        daemon=True,
    )
    workers.insert(0, server_thread)
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda _signum, _frame: stop_event.set())
    print("Maurice started. Press Ctrl+C to stop.")
    for worker in workers:
        worker.start()
        if worker is server_thread:
            _wait_for_socket(ctx.server_socket_path)
    if gateway and open_browser:
        _open_gateway_ui(bundle)
    try:
        while any(worker.is_alive() for worker in workers):
            time.sleep(0.2)
    finally:
        stop_event.set()
        server._running = False
        for worker in workers:
            worker.join(timeout=5)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        _remove_service_state(ctx, pid_path)
        print("Maurice stopped")



def _start_services_daemon(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
    telegram: bool,
    scheduler: bool,
    gateway: bool,
    open_browser: bool = True,
) -> None:
    bundle = _load_or_initialize_service_bundle(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    ctx = _global_service_context(workspace, agent_id=agent_id, bundle=bundle)
    pid_path = _pid_path(workspace)
    existing_pid = _read_pid(pid_path)
    if existing_pid and _pid_is_running(existing_pid):
        _check_running_service_meta(ctx, existing_pid)
        raise SystemExit(f"Maurice already appears to be running with pid {existing_pid}.")
    if not scheduler and not gateway and not (telegram and _telegram_channel_configured(bundle)):
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
    if not gateway:
        command.append("--no-gateway")
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
            _write_service_meta(ctx, pid)
            print(f"Maurice started in background with pid {pid}.")
            print(f"Logs: {log_path}")
            if gateway and open_browser:
                _open_gateway_ui(bundle)
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        _write_service_meta(ctx, process.pid)
        print(f"Maurice start requested in background with pid {process.pid}.")
        print(f"Logs: {log_path}")
        if gateway and open_browser:
            _open_gateway_ui(bundle)
        return
    raise SystemExit(f"Maurice failed to start. See logs: {log_path}")



def _stop_services(workspace_root: Path) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    ctx = _global_service_context(workspace, agent_id=None, bundle=bundle)
    pid_path = _pid_path(workspace)
    pid = _read_pid(pid_path)
    if not pid:
        print("Maurice is not running.")
        return
    if not _pid_is_running(pid):
        _remove_service_state(ctx, pid_path)
        print("Maurice is not running. Removed stale pid file.")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Stop requested for Maurice pid {pid}.")



def _restart_services_daemon(
    workspace_root: Path,
    *,
    agent_id: str | None,
    poll_seconds: float,
    telegram: bool,
    scheduler: bool,
    gateway: bool,
    open_browser: bool = True,
) -> None:
    bundle = _load_or_initialize_service_bundle(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    ctx = _global_service_context(workspace, agent_id=agent_id, bundle=bundle)
    pid_path = _pid_path(workspace)
    pid = _read_pid(pid_path)
    if pid and _pid_is_running(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"Stop requested for Maurice pid {pid}.")
        _wait_for_stop(pid_path, pid)
    elif pid:
        _remove_service_state(ctx, pid_path)
        print("Removed stale Maurice pid file.")
    else:
        print("Maurice is not running. Starting it now.")
    _start_services_daemon(
        workspace,
        agent_id=agent_id,
        poll_seconds=poll_seconds,
        telegram=telegram,
        scheduler=scheduler,
        gateway=gateway,
        open_browser=open_browser,
    )



def _wait_for_stop(pid_path: Path, pid: int, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            _remove_pid(pid_path)
            return
        if _read_pid(pid_path) != pid:
            return
        time.sleep(0.1)
    _remove_pid(pid_path)


def _load_or_initialize_service_bundle(workspace_root: Path) -> ConfigBundle:
    workspace = Path(workspace_root).expanduser().resolve()
    if _service_host_config_missing(workspace):
        runtime_root = Path(__file__).resolve().parents[2]
        initialize_workspace(workspace, runtime_root, permission_profile="safe")
        print(f"Maurice workspace initialized: {workspace}")
    try:
        return load_workspace_config(workspace)
    except Exception as exc:
        raise SystemExit(
            "Maurice workspace config is invalid. "
            f"Run `maurice onboard --workspace {workspace}` to repair it.\n"
            f"Details: {exc}"
        ) from exc


def _service_host_config_missing(workspace: Path) -> bool:
    host_path = host_config_path(workspace)
    if not host_path.exists():
        return True
    host = read_yaml_file(host_path).get("host")
    return (
        not isinstance(host, dict)
        or not host.get("runtime_root")
        or not host.get("workspace_root")
    )


def _wait_for_socket(socket_path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.05)


def _gateway_ui_url(bundle: ConfigBundle) -> str:
    host = bundle.host.gateway.host
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{bundle.host.gateway.port}"


def _wait_for_gateway_ui(url: str, *, timeout_seconds: float = 3.0) -> bool:
    health_url = f"{url.rstrip('/')}/health"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlrequest.urlopen(health_url, timeout=0.25) as response:
                if response.status < 500:
                    return True
        except (OSError, urlerror.URLError):
            time.sleep(0.1)
    return False


def _open_gateway_ui(bundle: ConfigBundle) -> None:
    url = _gateway_ui_url(bundle)
    if not _wait_for_gateway_ui(url):
        print(f"Maurice chat is not reachable yet: {url}")
        return
    print(f"Opening Maurice chat: {url}")
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"Could not open browser automatically: {exc}")


def _global_service_context(
    workspace: Path,
    *,
    agent_id: str | None,
    bundle: ConfigBundle,
) -> MauriceContext:
    agent = _resolve_agent(bundle, agent_id)
    return resolve_global_context(workspace, agent=agent, bundle=bundle)


def _write_service_meta(ctx: MauriceContext, pid: int) -> None:
    meta = {
        **_desired_server_meta(ctx),
        "pid": pid,
        "started_at": time.time(),
    }
    ctx.server_pid_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.server_pid_path.write_text(f"{pid}\n", encoding="utf-8")
    ctx.server_meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


def _check_running_service_meta(ctx: MauriceContext, pid: int) -> None:
    if os.environ.get("MAURICE_DEV"):
        return
    try:
        current = json.loads(ctx.server_meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    desired = _desired_server_meta(ctx)
    if not _server_meta_compatible(current, desired):
        raise SystemExit(
            f"Maurice pid {pid} is running with incompatible service metadata. "
            "Use `maurice restart` to refresh the daemon."
        )


def _remove_service_state(ctx: MauriceContext, pid_path: Path) -> None:
    _remove_pid(pid_path)
    for path in (ctx.server_pid_path, ctx.server_meta_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass



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
