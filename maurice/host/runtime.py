"""Agent runtime assembly — run_one_turn and provider wiring."""

from __future__ import annotations

import json
from pathlib import Path
import re
from difflib import SequenceMatcher
from typing import Any

from maurice.host.auth import CHATGPT_CREDENTIAL_NAME, get_valid_chatgpt_access_token
from maurice.host.context import MauriceContext, resolve_global_context
from maurice.host.errors import AgentError, ProviderError
from maurice.host.credentials import CredentialsStore, load_workspace_credentials
from maurice.host.delivery import _cancel_job_callback, _schedule_reminder_callback
from maurice.host.paths import maurice_home
from maurice.host.project_registry import (
    list_known_projects,
    list_machine_projects,
    record_seen_project,
)
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.classifier import Classifier
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import ConfigBundle, default_model_config, load_workspace_config
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider,
    ChatGPTCodexProvider,
    FallbackProvider,
    MockProvider,
    OllamaCompatibleProvider,
    OpenAICompatibleProvider,
    UnsupportedProvider,
)
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillHooks, SkillLoader
from maurice.host.docker_services import ensure_skill_services
from maurice.host.vision_backend import build_vision_backend


def build_global_agent_loop(
    *,
    ctx: MauriceContext | None = None,
    workspace_root: Path | None = None,
    message: str,
    session_id: str,
    agent_id: str | None = None,
    source_channel: str | None = None,
    source_peer_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    approval_callback: Any = None,
    text_delta_callback: Any = None,
    tool_started_callback: Any = None,
    _prebuilt_registry: Any = None,
) -> tuple[AgentLoop, Any]:
    if ctx is not None:
        if not isinstance(ctx.config, ConfigBundle):
            raise TypeError("build_global_agent_loop requires a global MauriceContext")
        bundle = ctx.config
        initial_context_root = ctx.context_root
        initial_active_project = ctx.active_project_root
    elif workspace_root is not None:
        bundle = load_workspace_config(workspace_root)
        initial_context_root = Path(bundle.host.workspace_root)
        initial_active_project = None
    else:
        raise ValueError("Either ctx or workspace_root must be provided")
    agent = _resolve_agent(bundle, agent_id)
    workspace = Path(bundle.host.workspace_root)
    ctx = resolve_global_context(
        initial_context_root,
        agent=agent,
        bundle=bundle,
        active_project=initial_active_project,
    )
    event_store = EventStore(ctx.events_path)
    active_project_root, project_source = _turn_active_project_path(ctx, agent, source_metadata, message=message)
    if active_project_root:
        record_seen_project(agent.workspace, active_project_root)
        _write_active_dev_project_path(agent, active_project_root)
        if project_source == "inferred":
            event_store.emit(
                name="project.inferred",
                kind="fact",
                origin="host",
                agent_id=agent.id,
                session_id=session_id,
                payload={"project": active_project_root, "from_message": True},
            )
    permission_context = PermissionContext(
        workspace_root=str(ctx.content_root),
        runtime_root=str(ctx.runtime_root),
        maurice_home_root=str(maurice_home()),
        agent_workspace_root=agent.workspace,
        active_project_root=active_project_root,
    )
    credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
    if _prebuilt_registry is not None:
        registry = _prebuilt_registry
    else:
        registry = SkillLoader(
            ctx.skill_roots,
            enabled_skills=agent.skills or bundle.kernel.skills or None,
            available_credentials=credentials.credentials.keys(),
            scope=ctx.scope,
            event_store=event_store,
            agent_id=agent.id,
            session_id=session_id,
        ).load()
        ensure_skill_services(registry)
    provider, model_config = _provider_and_model_for_config(bundle, message, credentials, agent=agent)
    skill_ctx = SkillContext(
        permission_context=permission_context,
        event_store=event_store,
        all_skill_configs=ctx.skills_config,
        skill_roots=ctx.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        agent_id=agent.id,
        session_id=session_id,
        hooks=SkillHooks(
            context_root=str(ctx.context_root),
            content_root=str(ctx.content_root),
            state_root=str(ctx.state_root),
            memory_path=str(ctx.memory_path),
            scope=ctx.scope,
            lifecycle=ctx.lifecycle,
            schedule_reminder=_schedule_reminder_callback(
                workspace,
                agent.id,
                session_id=session_id,
                source_channel=source_channel,
                source_peer_id=source_peer_id,
                source_metadata=source_metadata,
            ),
            cancel_job=_cancel_job_callback(workspace, agent.id),
            vision_backend=build_vision_backend(ctx.skills_config.get("vision")),
            agents={
                item.id: item.model_dump(mode="json")
                for item in bundle.agents.agents.values()
                if item.status == "active"
            },
        ),
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
        classifier_model = approvals_cfg.classifier_model or str(model_config.get("name") or "mock")
        classifier = Classifier(
            provider=provider,
            model=classifier_model,
            cache_ttl_seconds=approvals_cfg.classifier_cache_ttl_seconds,
        )
    return (
        AgentLoop(
            provider=provider,
            registry=registry,
            session_store=SessionStore(ctx.sessions_path),
            event_store=event_store,
            permission_context=permission_context,
            permission_profile=agent.permission_profile,
            tool_executors=registry.build_executor_map(skill_ctx),
            approval_store=ApprovalStore(
                ctx.approvals_path,
                event_store=event_store,
            ),
            model=str(model_config.get("name") or "mock"),
            system_prompt=_agent_system_prompt(
                workspace,
                agent=agent,
                active_project=active_project_root,
            ),
            compaction_config=compaction_config,
            classifier=classifier,
            approval_callback=approval_callback,
            text_delta_callback=text_delta_callback,
            tool_started_callback=tool_started_callback,
        ),
        agent,
    )


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
            raise AgentError(f"Unknown agent: {agent_id}") from exc
        if agent.status != "active":
            raise AgentError(f"Agent is not active: {agent_id} ({agent.status})")
        return agent
    for agent in bundle.agents.agents.values():
        if agent.default and agent.status == "active":
            return agent
    try:
        agent = bundle.agents.agents["main"]
    except KeyError as exc:
        raise AgentError("No default agent configured") from exc
    if agent.status != "active":
        raise AgentError("No active default agent configured")
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
    if not isinstance(payload, dict):
        return None
    active_path = payload.get("active_project_path")
    if isinstance(active_path, str) and active_path.strip():
        return str(Path(active_path).expanduser().resolve())
    active = payload.get("active_project")
    if not isinstance(active, str) or not active.strip():
        return None
    return str((agent_workspace / "content" / active.strip()).resolve())


def _write_active_dev_project_path(agent: Any | None, project_root: str | Path) -> None:
    if agent is None:
        return
    project = Path(project_root).expanduser().resolve()
    if not project.exists():
        return
    state_path = Path(agent.workspace).expanduser().resolve() / ".dev_state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("active_project_path") == str(project) and "active_project" not in payload:
        return
    payload["active_project_path"] = str(project)
    payload.pop("active_project", None)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _turn_active_project_path(
    ctx: MauriceContext,
    agent: Any | None,
    source_metadata: dict[str, Any] | None,
    *,
    message: str | None = None,
) -> tuple[str | None, str]:
    """Return (path, source) where source is 'metadata', 'ctx', 'inferred', or 'dev_state'."""
    metadata_project = (
        source_metadata.get("active_project_root")
        if isinstance(source_metadata, dict)
        else None
    )
    if isinstance(metadata_project, str) and metadata_project.strip():
        return str(Path(metadata_project).expanduser().resolve()), "metadata"
    if ctx.active_project_root is not None:
        return str(ctx.active_project_root), "ctx"
    inferred = _infer_known_project_from_message(agent, message or "")
    if inferred is not None:
        return str(inferred), "inferred"
    return _active_dev_project_path(agent), "dev_state"


def _infer_known_project_from_message(agent: Any | None, message: str) -> Path | None:
    if agent is None or not _message_has_project_intent(message):
        return None
    candidates = _known_project_candidates(agent)
    if not candidates:
        return None
    scored = [
        (_project_message_score(project["name"], message), Path(project["path"]).expanduser().resolve())
        for project in candidates
        if isinstance(project.get("name"), str) and isinstance(project.get("path"), str)
    ]
    scored = [(score, path) for score, path in scored if score >= 0.72 and path.exists()]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
        return None
    return scored[0][1]


def _known_project_candidates(agent: Any) -> list[dict[str, str]]:
    projects: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for project in [*list_known_projects(agent.workspace), *list_machine_projects()]:
        path = project.get("path")
        if not isinstance(path, str):
            continue
        key = str(Path(path).expanduser().resolve())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        projects.append(project)
    return projects


_PROJECT_INTENT_MARKERS: frozenset[str] = frozenset({
    # français
    "projet", "dossier", "mets toi", "met toi", "ouvre", "ouvrir",
    "travaille sur", "critique", "explore", "resume", "résume",
    # english
    "project", "folder", "repo", "codebase", "open", "work on",
    "switch to", "look at", "review", "summarize",
})


def _message_has_project_intent(message: str) -> bool:
    normalized = _normalize_project_text(message)
    if not normalized:
        return False
    return any(marker in normalized for marker in _PROJECT_INTENT_MARKERS)


def _project_message_score(project_name: str, message: str) -> float:
    project_text = _normalize_project_text(project_name)
    message_text = _normalize_project_text(message)
    if not project_text or not message_text:
        return 0.0
    if project_text in message_text:
        return 1.0
    project_tokens = project_text.split()
    message_tokens = message_text.split()
    if not project_tokens or not message_tokens:
        return 0.0
    token_overlap = len(set(project_tokens) & set(message_tokens)) / len(set(project_tokens))
    if token_overlap >= 0.66 and len(set(project_tokens) & set(message_tokens)) >= 2:
        return max(0.78, token_overlap)
    window = max(1, len(project_tokens))
    best = 0.0
    for start in range(0, len(message_tokens)):
        for size in range(max(1, window - 1), window + 3):
            snippet = " ".join(message_tokens[start : start + size])
            if not snippet:
                continue
            best = max(best, SequenceMatcher(None, project_text, snippet).ratio())
    return best


def _normalize_project_text(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", str(value).lower())
    return " ".join(normalized.split())


def _agent_system_prompt(
    workspace: Path,
    *,
    agent: Any | None = None,
    active_project: str | Path | None = None,
) -> str:
    from maurice.kernel.system_prompt import build_base_prompt
    agent_workspace = Path(agent.workspace).expanduser().resolve() if agent is not None else None
    agent_content = (
        agent_workspace / "content"
        if agent_workspace is not None
        else workspace / "agents" / "main" / "content"
    )
    project = active_project if active_project is not None else (
        _active_dev_project_path(agent) if agent is not None else None
    )
    return build_base_prompt(
        workspace=workspace,
        agent_content=agent_content,
        active_project=project,
        known_projects=list_known_projects(agent_workspace) if agent_workspace is not None else None,
        agent=agent,
    )


def _effective_model_config(bundle: ConfigBundle, agent: Any = None) -> dict[str, Any]:
    chain = _effective_model_chain(bundle, agent)
    return dict(chain[0]) if chain else default_model_config(bundle)


def _effective_model_chain(bundle: ConfigBundle, agent: Any = None) -> list[dict[str, Any]]:
    model_ids = list(getattr(agent, "model_chain", None) or [])
    if model_ids:
        chain = [
            profile.model_dump(mode="json")
            for model_id in model_ids
            if (profile := bundle.kernel.models.entries.get(model_id)) is not None
        ]
        if chain:
            return chain
    default_model = bundle.kernel.models.entries.get(bundle.kernel.models.default)
    if default_model is not None:
        return [default_model.model_dump(mode="json")]
    return [default_model_config(bundle)]


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
    provider, _model = _provider_and_model_for_config(bundle, message, credentials, agent=agent)
    return provider


def _provider_and_model_for_config(
    bundle: ConfigBundle,
    message: str,
    credentials: CredentialsStore | None = None,
    *,
    agent: Any = None,
) -> tuple[Any, dict[str, Any]]:
    chain = _effective_model_chain(bundle, agent)
    attempts: list[tuple[Any, dict[str, Any]]] = []
    last_provider: Any | None = None
    last_model: dict[str, Any] | None = None
    for model in chain:
        provider = _provider_for_model_config(bundle, message, model, credentials, agent=agent)
        if not isinstance(provider, UnsupportedProvider):
            attempts.append((provider, model))
            continue
        last_provider = provider
        last_model = model
    if len(attempts) == 1:
        provider, model = attempts[0]
        return provider, model
    if len(attempts) > 1:
        provider_attempts = [
            (provider, str(model.get("name") or "mock"))
            for provider, model in attempts
        ]
        return FallbackProvider(provider_attempts), attempts[0][1]
    if last_provider is not None and last_model is not None:
        return last_provider, last_model
    fallback = default_model_config(bundle)
    return _provider_for_model_config(bundle, message, fallback, credentials, agent=agent), fallback


def _provider_for_model_config(
    bundle: ConfigBundle,
    message: str,
    model: dict[str, Any],
    credentials: CredentialsStore | None = None,
    *,
    agent: Any = None,
) -> Any:
    provider_name = model.get("provider") or "mock"
    if provider_name == "mock":
        return MockProvider([
            {"type": "text_delta", "delta": f"Mock response: {message}"},
            {"type": "status", "status": "completed"},
        ])
    if provider_name == "api":
        protocol = model.get("protocol")
        if not protocol:
            return UnsupportedProvider(code="missing_protocol", message="API provider requires a model protocol.")
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
    raise ProviderError(f"Unsupported provider: {provider_name}")


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
    cancel_event: Any | None = None,
) -> TurnResult:
    loop, agent = build_global_agent_loop(
        workspace_root=workspace_root,
        message=message,
        session_id=session_id,
        agent_id=agent_id,
        source_channel=source_channel,
        source_peer_id=source_peer_id,
        source_metadata=source_metadata,
    )
    return loop.run_turn(
        agent_id=agent.id,
        session_id=session_id,
        message=message,
        limits=limits,
        message_metadata=message_metadata,
        cancel_event=cancel_event,
    )
