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
    template: str | None,
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
        template_id=template,
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
            "model_chain": list(agent.model_chain),
        },
    )



def _resolve_run_profile(
    workspace_root: Path,
    *,
    parent_agent_id: str,
    base_agent_id: str | None,
    template_id: str | None,
    inline_profile: str | None,
) -> tuple[str, dict[str, object]]:
    selectors = [value for value in (base_agent_id, template_id, inline_profile) if value]
    if len(selectors) > 1:
        raise SystemExit("--base-agent, --template and --inline-profile cannot be combined")
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
        profile.setdefault("model_chain", [])
        profile["inline"] = True
        return str(profile["id"]), profile
    if template_id:
        return _subagent_template_profile(workspace_root, template_id)
    return _base_agent_profile(workspace_root, base_agent_id or parent_agent_id)


def _subagent_template_profile(workspace_root: Path, template_id: str) -> tuple[str, dict[str, object]]:
    bundle = load_workspace_config(workspace_root)
    try:
        template = bundle.kernel.subagents.templates[template_id]
    except KeyError as exc:
        raise SystemExit(f"Unknown subagent template: {template_id}") from exc
    missing = [profile_id for profile_id in template.model_chain if profile_id not in bundle.kernel.models.entries]
    if missing:
        raise SystemExit(f"Unknown model profile(s) in subagent template {template_id}: {', '.join(missing)}")
    return (
        template.id,
        {
            "id": template.id,
            "template": True,
            "description": template.description,
            "skills": list(template.skills),
            "credentials": list(template.credentials),
            "permission_profile": template.permission_profile,
            "channels": list(template.channels),
            "model_chain": list(template.model_chain),
        },
    )


def _runs_templates_list(workspace_root: Path) -> None:
    bundle = load_workspace_config(workspace_root)
    templates = sorted(bundle.kernel.subagents.templates.values(), key=lambda item: item.id)
    if not templates:
        print("No subagent templates.")
        return
    for template in templates:
        models = ", ".join(template.model_chain) if template.model_chain else "-"
        print(f"{template.id} profile={template.permission_profile} models={models} {template.description}".rstrip())


def _runs_templates_add(
    workspace_root: Path,
    *,
    template_id: str,
    description: str,
    permission_profile: str,
    skills: list[str] | None,
    credentials: list[str] | None,
    model_chain: list[str] | None,
) -> None:
    bundle = load_workspace_config(workspace_root)
    if template_id in bundle.kernel.subagents.templates:
        raise SystemExit(f"Subagent template already exists: {template_id}")
    model_chain = model_chain or []
    missing = [profile_id for profile_id in model_chain if profile_id not in bundle.kernel.models.entries]
    if missing:
        raise SystemExit(f"Unknown model profile(s): {', '.join(missing)}")
    if permission_profile not in {"safe", "limited", "power"}:
        raise SystemExit("Permission profile must be safe, limited or power.")
    kernel_path = kernel_config_path(workspace_root)
    kernel_data = read_yaml_file(kernel_path)
    template = {
        "id": template_id,
        "description": description,
        "skills": skills or [],
        "credentials": credentials or [],
        "permission_profile": permission_profile,
        "channels": [],
        "model_chain": model_chain,
    }
    kernel_data.setdefault("kernel", {}).setdefault("subagents", {}).setdefault("templates", {})[template_id] = template
    write_yaml_file(kernel_path, kernel_data)
    print(f"Subagent template created: {template_id}")



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
