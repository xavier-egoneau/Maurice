"""Agent runtime assembly — run_one_turn and provider wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maurice.host.auth import CHATGPT_CREDENTIAL_NAME, get_valid_chatgpt_access_token
from maurice.host.credentials import CredentialsStore, load_workspace_credentials
from maurice.host.delivery import _cancel_job_callback, _schedule_reminder_callback
from maurice.host.paths import maurice_home
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.classifier import Classifier
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import ConfigBundle, load_workspace_config
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
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader


def _effective_model_label(bundle: ConfigBundle, agent: Any = None) -> str:
    model = _effective_model_config(bundle, agent)
    return f"{model.get('provider') or 'mock'}:{model.get('protocol') or model.get('name') or 'mock'}"


def _default_agent(bundle: ConfigBundle) -> Any:
    return next((agent for agent in bundle.agents.agents.values() if agent.default), None)


def _resolve_agent(bundle: ConfigBundle, agent_id: str | None) -> Any:
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


def _active_dev_project_path(agent: Any | None) -> str | None:
    if agent is None:
        return None
    agent_workspace = Path(agent.workspace).expanduser().resolve()
    state_path = agent_workspace / ".dev_state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    active = payload.get("active_project") if isinstance(payload, dict) else None
    if not isinstance(active, str) or not active.strip():
        return None
    return str((agent_workspace / "content" / active.strip()).resolve())


def _agent_system_prompt(workspace: Path, *, agent: Any | None = None) -> str:
    from maurice.kernel.system_prompt import build_base_prompt
    agent_workspace = Path(agent.workspace).expanduser().resolve() if agent is not None else None
    agent_content = agent_workspace / "content" if agent_workspace is not None else workspace / "content"
    project = _active_dev_project_path(agent) if agent is not None else None
    return build_base_prompt(
        workspace=workspace,
        agent_content=agent_content,
        active_project=project,
        agent=agent,
    )


def _effective_model_config(bundle: ConfigBundle, agent: Any = None) -> dict[str, Any]:
    agent_model = getattr(agent, "model", None)
    if agent_model:
        return dict(agent_model)
    return bundle.kernel.model.model_dump(mode="json")


def _model_credential(model: dict[str, Any], credentials: CredentialsStore | None) -> Any:
    if credentials is None:
        return None
    name = model.get("credential")
    if not name and model.get("protocol") == "openai_chat_completions":
        name = "openai"
    if not name:
        return None
    return credentials.credentials.get(name)


def _provider_for_config(
    bundle: ConfigBundle,
    message: str,
    credentials: CredentialsStore | None = None,
    *,
    agent: Any = None,
) -> Any:
    model = _effective_model_config(bundle, agent)
    provider_name = model["provider"]
    if provider_name == "mock":
        return MockProvider([
            {"type": "text_delta", "delta": f"Mock response: {message}"},
            {"type": "status", "status": "completed"},
        ])
    if provider_name == "api":
        protocol = model.get("protocol")
        if not protocol:
            return UnsupportedProvider(code="missing_protocol", message="API provider requires kernel.model.protocol.")
        credential = _model_credential(model, credentials)
        return ApiProvider(
            protocol=protocol,
            api_key=credential.value if credential is not None else None,
            base_url=(model.get("base_url") or (credential.base_url if credential is not None else None)),
        )
    if provider_name == "auth":
        protocol = model.get("protocol") or "unknown"
        if protocol == "chatgpt_codex":
            credential_name = model.get("credential") or CHATGPT_CREDENTIAL_NAME
            if agent is not None and "*" not in agent.credentials and credential_name not in agent.credentials:
                return UnsupportedProvider(code="credential_not_allowed", message=f"Agent is not allowed to use credential: {credential_name}")
            token = get_valid_chatgpt_access_token(bundle.host.workspace_root, credential_name=credential_name)
            if not token:
                return UnsupportedProvider(code="auth_missing", message="ChatGPT auth requires a stored credential.")
            credential = _model_credential(model, credentials)
            return ChatGPTCodexProvider(
                token=token,
                base_url=(model.get("base_url") or (credential.base_url if credential is not None else None) or "https://chatgpt.com/backend-api/codex"),
            )
        return UnsupportedProvider(code="auth_provider_not_implemented", message=f"Auth provider protocol is not implemented yet: {protocol}")
    if provider_name == "openai":
        credential = (credentials.credentials.get(str(model.get("credential") or "openai")) if credentials is not None else None)
        return OpenAICompatibleProvider(
            api_key=credential.value if credential is not None else None,
            base_url=(model.get("base_url") or (credential.base_url if credential is not None else None) or "https://api.openai.com/v1"),
        )
    if provider_name == "ollama":
        credential = (credentials.credentials.get(str(model.get("credential"))) if credentials is not None and model.get("credential") else None)
        return OllamaCompatibleProvider(
            base_url=str(model.get("base_url") or "http://localhost:11434"),
            api_key=credential.value if credential is not None else "",
        )
    raise SystemExit(f"Unsupported provider: {provider_name}")


def run_one_turn(
    *,
    workspace_root: Path,
    message: str,
    session_id: str,
    agent_id: str | None = None,
    source_channel: str | None = None,
    source_peer_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    message_metadata: dict[str, Any] | None = None,
) -> TurnResult:
    bundle = load_workspace_config(workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    event_stream = (
        Path(agent.event_stream) if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
    permission_context = PermissionContext(
        workspace_root=bundle.host.workspace_root,
        runtime_root=bundle.host.runtime_root,
        maurice_home_root=str(maurice_home()),
        agent_workspace_root=agent.workspace,
        active_project_root=_active_dev_project_path(agent),
    )
    event_store = EventStore(event_stream)
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    registry = SkillLoader(
        bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills or None,
        available_credentials=credentials.credentials.keys(),
        event_store=event_store,
        agent_id=agent.id,
        session_id=session_id,
    ).load()
    model_config = _effective_model_config(bundle, agent)
    provider = _provider_for_config(bundle, message, credentials, agent=agent)
    skill_ctx = SkillContext(
        permission_context=permission_context,
        event_store=event_store,
        all_skill_configs=bundle.skills.skills,
        skill_roots=bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        agent_id=agent.id,
        session_id=session_id,
        extra={
            "schedule_reminder": _schedule_reminder_callback(
                workspace,
                agent.id,
                session_id=session_id,
                source_channel=source_channel,
                source_peer_id=source_peer_id,
                source_metadata=source_metadata,
            ),
            "cancel_job": _cancel_job_callback(workspace, agent.id),
        },
    )
    sessions_cfg = bundle.kernel.sessions
    approvals_cfg = bundle.kernel.approvals
    compaction_config = (
        CompactionConfig(
            context_window_tokens=sessions_cfg.context_window_tokens,
            trim_threshold=sessions_cfg.trim_threshold,
            summarize_threshold=sessions_cfg.summarize_threshold,
            reset_threshold=sessions_cfg.reset_threshold,
            keep_recent_turns=sessions_cfg.keep_recent_turns,
        )
        if sessions_cfg.compaction
        else None
    )
    classifier = None
    if approvals_cfg.mode == "auto":
        classifier_model = approvals_cfg.classifier_model or str(model_config.get("name") or bundle.kernel.model.name)
        classifier = Classifier(
            provider=provider,
            model=classifier_model,
            cache_ttl_seconds=approvals_cfg.classifier_cache_ttl_seconds,
        )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(workspace / "sessions"),
        event_store=event_store,
        permission_context=permission_context,
        permission_profile=agent.permission_profile,
        tool_executors=registry.build_executor_map(skill_ctx),
        approval_store=ApprovalStore(
            workspace / "agents" / agent.id / "approvals.json",
            event_store=event_store,
        ),
        model=str(model_config.get("name") or bundle.kernel.model.name),
        system_prompt=_agent_system_prompt(workspace, agent=agent),
        compaction_config=compaction_config,
        classifier=classifier,
    )
    return loop.run_turn(
        agent_id=agent.id,
        session_id=session_id,
        message=message,
        limits=limits,
        message_metadata=message_metadata,
    )
