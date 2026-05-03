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
from maurice.host.context import resolve_global_context
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
from maurice.kernel.skills import SkillContext, SkillHooks, SkillLoader
from maurice.host.vision_backend import build_vision_backend
from maurice.system_skills.reminders.tools import fire_reminder

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



def _scheduler_configure(
    workspace_root: Path,
    *,
    dream_time: str | None,
    daily_time: str | None,
    enable_dreaming: bool,
    disable_dreaming: bool,
    enable_daily: bool,
    disable_daily: bool,
) -> None:
    workspace = Path(load_workspace_config(workspace_root).host.workspace_root)
    if enable_dreaming and disable_dreaming:
        raise SystemExit("Choose either --enable-dreaming or --disable-dreaming.")
    if enable_daily and disable_daily:
        raise SystemExit("Choose either --enable-daily or --disable-daily.")
    data = read_yaml_file(kernel_config_path(workspace))
    kernel = data.setdefault("kernel", {})
    scheduler = kernel.setdefault("scheduler", {})
    if dream_time is not None:
        scheduler["dreaming_time"] = _normalize_local_time_or_exit(dream_time)
    if daily_time is not None:
        scheduler["daily_time"] = _normalize_local_time_or_exit(daily_time)
    if enable_dreaming:
        scheduler["dreaming_enabled"] = True
    if disable_dreaming:
        scheduler["dreaming_enabled"] = False
    if enable_daily:
        scheduler["daily_enabled"] = True
    if disable_daily:
        scheduler["daily_enabled"] = False
    write_yaml_file(kernel_config_path(workspace), data)
    bundle = load_workspace_config(workspace)
    print(
        "Scheduler configured: "
        f"dreaming {'on' if bundle.kernel.scheduler.dreaming_enabled else 'off'} "
        f"at {bundle.kernel.scheduler.dreaming_time}, "
        f"daily {'on' if bundle.kernel.scheduler.daily_enabled else 'off'} "
        f"at {bundle.kernel.scheduler.daily_time}."
    )



def _scheduler_run_once(
    workspace_root: Path,
    *,
    agent_id: str | None,
    limit: int | None,
) -> None:
    store, agent = _job_store_for(workspace_root, agent_id)
    _ensure_configured_scheduler_jobs(workspace_root, agent.id)
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
    _ensure_configured_scheduler_jobs(workspace_root, agent.id)
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
    _ensure_configured_scheduler_jobs(workspace_root, agent.id)
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
    ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)
    return (
        JobStore(
            workspace / "agents" / agent.id / "jobs.json",
            event_store=EventStore(ctx.events_path),
        ),
        agent,
    )



def _ensure_configured_scheduler_jobs(workspace_root: Path, agent_id: str) -> None:
    bundle = load_workspace_config(workspace_root)
    if not bundle.kernel.scheduler.enabled:
        return
    workspace = Path(bundle.host.workspace_root)
    store = JobStore(workspace / "agents" / agent_id / "jobs.json")
    scheduler = bundle.kernel.scheduler
    agent = _resolve_agent(bundle, agent_id)
    enabled_skills = agent.skills or bundle.kernel.skills or None
    if scheduler.dreaming_enabled and (enabled_skills is None or "dreaming" in enabled_skills):
        _ensure_recurring_job(
            store,
            name="dreaming.run",
            owner="skill:dreaming",
            kind="system.dreaming.daily",
            agent_id=agent_id,
            session_id="dreaming",
            time_value=scheduler.dreaming_time,
            arguments={},
        )
    else:
        _cancel_recurring_job_kind(store, "system.dreaming.daily")
    if scheduler.daily_enabled and (enabled_skills is None or "daily" in enabled_skills):
        _ensure_recurring_job(
            store,
            name="daily.digest",
            owner="skill:daily",
            kind="system.daily.digest",
            agent_id=agent_id,
            session_id="daily",
            time_value=scheduler.daily_time,
            arguments={},
        )
    else:
        _cancel_recurring_job_kind(store, "system.daily.digest")



def _ensure_recurring_job(
    store: JobStore,
    *,
    name: str,
    owner: str,
    kind: str,
    agent_id: str,
    session_id: str,
    time_value: str,
    arguments: dict[str, Any],
) -> None:
    normalized_time = _normalize_local_time_or_exit(time_value)
    for job in store.list():
        if job.payload.get("kind") != kind:
            continue
        same_schedule = (
            job.status == JobStatus.SCHEDULED
            and job.interval_seconds == 86400
            and job.payload.get("time") == normalized_time
        )
        if same_schedule:
            return
        if job.status == JobStatus.SCHEDULED:
            store.cancel(job.id)
    store.schedule(
        name=name,
        owner=owner,
        run_at=_next_local_time(normalized_time),
        interval_seconds=86400,
        payload={
            "kind": kind,
            "time": normalized_time,
            "agent_id": agent_id,
            "session_id": session_id,
            "arguments": arguments,
        },
    )



def _cancel_recurring_job_kind(store: JobStore, kind: str) -> None:
    for job in store.list(status=JobStatus.SCHEDULED):
        if job.payload.get("kind") == kind:
            store.cancel(job.id)



def _next_local_time(value: str, *, now: datetime | None = None) -> datetime:
    normalized = _normalize_local_time_or_exit(value)
    hour, minute = [int(part) for part in normalized.split(":", 1)]
    now_local = now.astimezone() if now else datetime.now().astimezone()
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)



def _normalize_local_time_or_exit(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "h" in raw:
        hour, _, minute = raw.partition("h")
        minute = minute or "00"
    elif ":" in raw:
        hour, _, minute = raw.partition(":")
    else:
        raise SystemExit(f"Invalid time {value!r}. Use HH:MM or 9h30.")
    if not hour.isdigit() or not minute.isdigit():
        raise SystemExit(f"Invalid time {value!r}. Use HH:MM or 9h30.")
    hour_int = int(hour)
    minute_int = int(minute)
    if hour_int > 23 or minute_int > 59:
        raise SystemExit(f"Invalid time {value!r}. Use HH:MM or 9h30.")
    return f"{hour_int:02d}:{minute_int:02d}"



def _scheduler_handlers(workspace_root: Path, agent_id: str):
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)
    event_store = EventStore(ctx.events_path)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    context = PermissionContext(
        workspace_root=str(ctx.content_root),
        runtime_root=str(ctx.runtime_root),
        maurice_home_root=str(maurice_home()),
        agent_workspace_root=agent.workspace,
        active_project_root=_active_dev_project_path(agent),
    )
    registry = SkillLoader(
        ctx.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills or None,
        available_credentials=credentials.credentials.keys(),
        scope=ctx.scope,
        event_store=event_store,
        agent_id=agent.id,
        session_id="scheduler",
    ).load()
    skill_ctx = SkillContext(
        permission_context=context,
        event_store=event_store,
        all_skill_configs=ctx.skills_config,
        skill_roots=ctx.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        agent_id=agent.id,
        session_id="scheduler",
        hooks=SkillHooks(
            context_root=str(ctx.context_root),
            content_root=str(ctx.content_root),
            state_root=str(ctx.state_root),
            memory_path=str(ctx.memory_path),
            scope=ctx.scope,
            lifecycle=ctx.lifecycle,
            vision_backend=build_vision_backend(ctx.skills_config.get("vision")),
            agents={
                item.id: item.model_dump(mode="json")
                for item in bundle.agents.agents.values()
                if item.status == "active"
            },
        ),
    )
    executors = registry.build_executor_map(skill_ctx)

    def run_dream(job):
        arguments = job.payload.get("arguments", {})
        result = executors["dreaming.run"](arguments if isinstance(arguments, dict) else {})
        if not result.ok:
            raise RuntimeError(result.summary)
        return result

    def run_daily(job):
        arguments = job.payload.get("arguments", {})
        daily = executors.get("daily.digest")
        if daily is None:
            text = _build_daily_digest(workspace, agent.id)
        else:
            result = daily(arguments if isinstance(arguments, dict) else {})
            if not result.ok:
                raise RuntimeError(result.summary)
            text = result.summary
        _deliver_daily_digest(workspace, job.payload, text, event_store=event_store)
        return text

    def run_reminder(job):
        arguments = job.payload.get("arguments", {})
        result = fire_reminder(arguments if isinstance(arguments, dict) else {}, context, event_store=event_store)
        if not result.ok:
            raise RuntimeError(result.summary)
        _deliver_reminder_result(workspace, job.payload, result.summary)
        return result

    return {"dreaming.run": run_dream, "daily.digest": run_daily, "reminders.fire": run_reminder}
