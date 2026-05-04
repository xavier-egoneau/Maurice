"""Unified host context resolution for Maurice."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from maurice.host.paths import maurice_home
from maurice.host.project import (
    approvals_path,
    config_path,
    ensure_maurice_dir,
    events_path,
    global_config_path,
    sessions_dir,
)
from maurice.host.workspace import ensure_agent_memory_migrated
from maurice.kernel.config import ConfigBundle, load_workspace_config
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillRoot


Scope = Literal["local", "global"]
Lifecycle = Literal["transient", "daemon"]


@dataclass(frozen=True)
class LocalConfig:
    """Merged machine + project config for a folder-scoped surface."""

    data: dict[str, Any]

    @property
    def permission_profile(self) -> str:
        return str(self.data.get("permission_profile") or "limited")

    @property
    def enabled_skills(self) -> list[str] | None:
        skills = self.data.get("skills")
        return list(skills) if isinstance(skills, list) else None

    @property
    def skills_config(self) -> dict[str, dict[str, Any]]:
        value = self.data.get("skills_config") or {}
        return value if isinstance(value, dict) else {}

    @property
    def provider(self) -> dict[str, Any]:
        value = self.data.get("provider") or {}
        return value if isinstance(value, dict) else {}

    @property
    def usage(self) -> dict[str, Any]:
        value = self.data.get("usage") or {}
        return value if isinstance(value, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        return self.data.setdefault(key, default)


@dataclass(frozen=True)
class MauriceContext:
    """Resolved runtime/state/content roots for one Maurice conversation surface."""

    scope: Scope
    lifecycle: Lifecycle
    runtime_root: Path
    context_root: Path
    state_root: Path
    content_root: Path
    config: ConfigBundle | LocalConfig
    sessions_path: Path
    events_path: Path
    approvals_path: Path
    memory_path: Path
    skill_roots: list[SkillRoot]
    agent_workspace_root: Path
    active_project_root: Path | None = None

    @property
    def run_root(self) -> Path:
        return self.state_root / "run"

    @property
    def server_socket_path(self) -> Path:
        return self.run_root / "server.socket"

    @property
    def server_pid_path(self) -> Path:
        return self.run_root / "server.pid"

    @property
    def server_meta_path(self) -> Path:
        return self.run_root / "server.meta"

    @property
    def permission_profile(self) -> str:
        if isinstance(self.config, LocalConfig):
            return self.config.permission_profile
        return self.config.kernel.permissions.profile

    @property
    def enabled_skills(self) -> list[str] | None:
        if isinstance(self.config, LocalConfig):
            return self.config.enabled_skills
        return list(self.config.kernel.skills) or None

    @property
    def skills_config(self) -> dict[str, dict[str, Any]]:
        if isinstance(self.config, LocalConfig):
            return self.config.skills_config
        return self.config.skills.skills


def resolve_local_context(
    project_root: Path,
    *,
    lifecycle: Lifecycle = "transient",
) -> MauriceContext:
    """Resolve a folder-centered Maurice context."""
    root = project_root.expanduser().resolve()
    state_root = ensure_maurice_dir(root)
    cfg = _load_local_config(root)
    agent_workspace = _local_agent_workspace(cfg)
    _ensure_agent_workspace_dirs(agent_workspace)
    return MauriceContext(
        scope="local",
        lifecycle=lifecycle,
        runtime_root=_runtime_root(),
        context_root=root,
        state_root=state_root,
        content_root=root,
        config=cfg,
        sessions_path=sessions_dir(root),
        events_path=events_path(root),
        approvals_path=approvals_path(root),
        memory_path=agent_workspace / "memory" / "memory.sqlite",
        skill_roots=_local_skill_roots(root, cfg),
        agent_workspace_root=agent_workspace,
        active_project_root=root,
    )


def resolve_global_context(
    workspace_root: Path,
    *,
    agent: Any | None = None,
    bundle: ConfigBundle | None = None,
    lifecycle: Lifecycle = "daemon",
    active_project: Path | None = None,
) -> MauriceContext:
    """Resolve the long-running assistant context for a workspace."""
    workspace = workspace_root.expanduser().resolve()
    active_project_root = active_project.expanduser().resolve() if active_project is not None else None
    cfg = bundle or load_workspace_config(workspace)
    agent_id = str(getattr(agent, "id", "main"))
    agent_workspace = (
        Path(agent.workspace).expanduser().resolve()
        if agent is not None and getattr(agent, "workspace", None)
        else workspace / "agents" / agent_id
    )
    memory_path = ensure_agent_memory_migrated(
        workspace,
        agent_id=agent_id,
        agent_workspace=agent_workspace,
    )
    event_stream = (
        Path(agent.event_stream).expanduser().resolve()
        if agent is not None and getattr(agent, "event_stream", None)
        else workspace / "agents" / agent_id / "events.jsonl"
    )
    return MauriceContext(
        scope="global",
        lifecycle=lifecycle,
        runtime_root=Path(cfg.host.runtime_root).expanduser().resolve(),
        context_root=workspace,
        state_root=workspace,
        content_root=workspace,
        config=cfg,
        sessions_path=workspace / "sessions",
        events_path=event_stream,
        approvals_path=agent_workspace / "approvals.json",
        memory_path=memory_path,
        skill_roots=[
            root if isinstance(root, SkillRoot) else SkillRoot.from_config(root)
            for root in cfg.host.skill_roots
        ],
        agent_workspace_root=agent_workspace,
        active_project_root=active_project_root,
    )


def build_command_callbacks(
    ctx: MauriceContext,
    *,
    command_registry: Any | None = None,
    model_summary: Any | None = None,
    agent_workspace: str | Path | None = None,
    agent_workspace_for: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard callback envelope passed to command handlers."""

    def reset_session(agent_id: str, session_id: str) -> str:
        store = SessionStore(ctx.sessions_path)
        try:
            store.reset(agent_id, session_id)
        except FileNotFoundError:
            store.create(agent_id, session_id=session_id)
        return session_id

    def compact_session(agent_id: str, session_id: str) -> str:
        from maurice.host.output import _compact_text

        store = SessionStore(ctx.sessions_path)
        try:
            session = store.load(agent_id, session_id)
        except FileNotFoundError:
            return "Session vide. Rien à compacter."
        if not session.messages:
            return "Session vide. Rien à compacter."
        message_count = len(session.messages)
        user_count = sum(1 for message in session.messages if message.role == "user")
        assistant_count = sum(1 for message in session.messages if message.role == "assistant")
        recent = [
            f"- {message.role}: {_compact_text(message.content, 200)}"
            for message in session.messages[-6:]
        ]
        store.reset(agent_id, session_id)
        return (
            f"Session compactée — {message_count} messages "
            f"({user_count} user, {assistant_count} assistant) effacés.\n\n"
            "Derniers éléments conservés :\n" + "\n".join(recent)
        )

    _agent_workspace_root = ctx.agent_workspace_root

    def _has_active_project(_agent_id: str, _session_id: str) -> bool:
        if ctx.active_project_root is not None:
            return True
        if _agent_workspace_root is None:
            return False
        state_path = Path(_agent_workspace_root) / ".dev_state.json"
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            return bool(data.get("active_project_path") or data.get("active_project"))
        except Exception:
            return False

    callbacks: dict[str, Any] = {
        "workspace": ctx.content_root,
        "context_root": ctx.context_root,
        "content_root": ctx.content_root,
        "state_root": ctx.state_root,
        "memory_path": ctx.memory_path,
        "scope": ctx.scope,
        "lifecycle": ctx.lifecycle,
        "compact_session": compact_session,
        "reset_session": reset_session,
        "new_session": reset_session,
        "has_active_project": _has_active_project,
    }
    if ctx.active_project_root is not None:
        callbacks["active_project_path"] = ctx.active_project_root
        callbacks["project_root"] = ctx.active_project_root
    if ctx.scope == "local":
        callbacks["agent_workspace"] = ctx.agent_workspace_root
    elif agent_workspace is not None:
        callbacks["agent_workspace"] = Path(agent_workspace).expanduser().resolve()
    if command_registry is not None:
        callbacks["command_registry"] = command_registry
    if model_summary is not None:
        callbacks["model_summary"] = model_summary
    if agent_workspace_for is not None:
        callbacks["agent_workspace_for"] = agent_workspace_for
    if extra:
        callbacks.update(extra)
    return callbacks


def _runtime_root() -> Path:
    module = sys.modules.get("maurice")
    if module is not None and getattr(module, "__file__", None):
        return Path(module.__file__).resolve().parent.parent
    return Path(__file__).resolve().parents[2]


def _load_local_config(project_root: Path) -> LocalConfig:
    """Merge global ~/.maurice/config.yaml with local .maurice/config.yaml."""
    import yaml

    cfg: dict[str, Any] = {}
    for path in (global_config_path(), config_path(project_root)):
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                cfg = _merge_local_config(cfg, data)
    return LocalConfig(cfg)


def _local_agent_workspace(cfg: LocalConfig) -> Path:
    usage = cfg.usage
    workspace = usage.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        return (Path(workspace).expanduser().resolve() / "agents" / "main")
    return maurice_home() / "agents" / "main"


def _ensure_agent_workspace_dirs(agent_workspace: Path) -> None:
    for relative in ("content", "memory", "dreams", "reminders"):
        (agent_workspace / relative).mkdir(parents=True, exist_ok=True)


def _merge_local_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if key == "skill_roots" and isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        elif key == "host" and isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_host_config(current, value)
        elif (
            key == "skills_config"
            and isinstance(current, dict)
            and isinstance(value, dict)
        ):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    return merged


def _merge_host_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if key == "skill_roots" and isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    return merged


def _local_skill_roots(project_root: Path, cfg: LocalConfig) -> list[SkillRoot]:
    roots = [
        SkillRoot(
            path=str(Path(__file__).parent.parent / "system_skills"),
            origin="system",
            mutable=False,
        ),
    ]
    roots.extend(_configured_skill_roots(cfg.get("skill_roots")))

    host_cfg = cfg.get("host")
    if isinstance(host_cfg, dict):
        roots.extend(_configured_skill_roots(host_cfg.get("skill_roots")))

    for path in (maurice_home() / "skills", project_root / "skills"):
        if path.exists():
            roots.append(SkillRoot(path=str(path), origin="user", mutable=True))
    return _dedupe_skill_roots(roots)


def _configured_skill_roots(value: Any) -> list[SkillRoot]:
    if not isinstance(value, list):
        return []
    roots: list[SkillRoot] = []
    for item in value:
        if isinstance(item, str):
            roots.append(SkillRoot(path=item, origin="user", mutable=True))
        elif isinstance(item, dict) and item.get("path"):
            roots.append(
                SkillRoot(
                    path=str(item["path"]),
                    origin=str(item.get("origin") or "user"),
                    mutable=bool(item.get("mutable", True)),
                )
            )
    return roots


def _dedupe_skill_roots(roots: list[SkillRoot]) -> list[SkillRoot]:
    result: list[SkillRoot] = []
    seen: set[str] = set()
    for root in roots:
        key = str(Path(root.path).expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result
