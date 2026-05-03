"""Persistent agent runtime — caches config + skill registry between turns."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from maurice.kernel.loop import TurnResult


class AgentRuntime:
    """Caches the expensive I/O (config YAML + skill manifests) across turns.

    The AgentLoop itself is rebuilt per turn so that per-turn context
    (source_channel, session_id for reminder callbacks, etc.) stays correct.
    The SkillRegistry — the expensive part — is reused.
    Call invalidate() after a config or skill change.
    """

    def __init__(self, workspace_root: Path, agent_id: str | None = None) -> None:
        self._workspace_root = Path(workspace_root)
        self._agent_id = agent_id
        self._lock = threading.Lock()
        self._bundle: Any = None
        self._ctx: Any = None
        self._registry: Any = None

    def _load_cache(self) -> None:
        from maurice.kernel.config import load_workspace_config
        from maurice.kernel.events import EventStore
        from maurice.kernel.skills import SkillLoader
        from maurice.host.context import resolve_global_context
        from maurice.host.credentials import load_workspace_credentials
        from maurice.host.runtime import _resolve_agent

        bundle = load_workspace_config(self._workspace_root)
        agent = _resolve_agent(bundle, self._agent_id)
        workspace = Path(bundle.host.workspace_root)
        ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)
        credentials = load_workspace_credentials(workspace).visible_to(agent.credentials)
        registry = SkillLoader(
            ctx.skill_roots,
            enabled_skills=agent.skills or bundle.kernel.skills or None,
            available_credentials=credentials.credentials.keys(),
            scope=ctx.scope,
            agent_id=agent.id,
            session_id="startup",
        ).load()
        self._bundle = bundle
        self._ctx = ctx
        self._registry = registry

        from maurice.host.docker_services import ensure_skill_services
        ensure_skill_services(registry)

    def run_turn(
        self,
        *,
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
        with self._lock:
            if self._bundle is None:
                self._load_cache()
            from maurice.host.runtime import build_global_agent_loop
            loop, agent = build_global_agent_loop(
                ctx=self._ctx,
                message=message,
                session_id=session_id,
                agent_id=agent_id or self._agent_id,
                source_channel=source_channel,
                source_peer_id=source_peer_id,
                source_metadata=source_metadata,
                _prebuilt_registry=self._registry,
            )
            return loop.run_turn(
                agent_id=agent.id,
                session_id=session_id,
                message=message,
                limits=limits,
                message_metadata=message_metadata,
                cancel_event=cancel_event,
            )

    def invalidate(self) -> None:
        with self._lock:
            self._bundle = None
            self._ctx = None
            self._registry = None


class RuntimeRegistry:
    """Process-level registry of AgentRuntime instances, one per (workspace, agent_id)."""

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        workspace_root: Path | str,
        agent_id: str | None = None,
    ) -> AgentRuntime:
        key = f"{workspace_root}:{agent_id or '__default__'}"
        with self._lock:
            if key not in self._runtimes:
                self._runtimes[key] = AgentRuntime(
                    workspace_root=Path(workspace_root),
                    agent_id=agent_id,
                )
            return self._runtimes[key]

    def invalidate(self, workspace_root: Path | str, agent_id: str | None = None) -> None:
        key = f"{workspace_root}:{agent_id or '__default__'}"
        with self._lock:
            if key in self._runtimes:
                self._runtimes[key].invalidate()

    def invalidate_all(self) -> None:
        with self._lock:
            for runtime in self._runtimes.values():
                runtime.invalidate()


_global_registry = RuntimeRegistry()


def global_registry() -> RuntimeRegistry:
    return _global_registry
