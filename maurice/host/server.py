"""Maurice background server — Unix socket, one process per project root."""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from maurice.host.context import (
    MauriceContext,
    build_command_callbacks,
    resolve_global_context,
    resolve_local_context,
)
from maurice.host.runtime import build_global_agent_loop
from maurice.kernel.config import load_workspace_config
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.config import KernelSessionsConfig
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillHooks, SkillLoader
from maurice.kernel.tool_labels import tool_action_label
from maurice.host.vision_backend import build_vision_backend


# --- protocol helpers --------------------------------------------------------

def _send(conn: socket.socket, msg: dict[str, Any]) -> None:
    data = (json.dumps(msg) + "\n").encode()
    conn.sendall(data)


def _recv_line(conn: socket.socket, buf: bytearray) -> dict[str, Any] | None:
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)
    idx = buf.index(b"\n")
    line = buf[:idx]
    del buf[:idx + 1]
    return json.loads(line.decode())


# --- credentials -------------------------------------------------------------

def _read_credential(name: str) -> str | None:
    """Read a credential value from ~/.maurice/credentials.yaml."""
    try:
        from maurice.host.credentials import load_credentials, credentials_path
        store = load_credentials(credentials_path())
        record = store.credentials.get(name)
        return record.value if record else None
    except Exception:
        return None


def _build_provider(cfg: dict[str, Any], message: str = "") -> Any:
    from maurice.kernel.providers import (
        ApiProvider, ChatGPTCodexProvider, MockProvider,
        OllamaCompatibleProvider, OpenAICompatibleProvider, UnsupportedProvider,
    )
    from maurice.host.paths import maurice_home

    provider_cfg = cfg.get("provider") or {}
    kind = provider_cfg.get("type", "mock")

    if kind == "mock":
        return MockProvider([
            {"type": "text_delta", "delta": f"Mock: {message}"},
            {"type": "status", "status": "completed"},
        ])

    if kind in ("api", "anthropic"):
        credential = provider_cfg.get("credential", "anthropic")
        api_key = cfg.get("credentials", {}).get(credential) or _read_credential(credential)
        return ApiProvider(
            protocol=provider_cfg.get("protocol", "anthropic"),
            api_key=api_key,
            base_url=provider_cfg.get("base_url"),
        )

    if kind == "openai":
        credential = provider_cfg.get("credential", "openai")
        api_key = cfg.get("credentials", {}).get(credential) or _read_credential(credential)
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=provider_cfg.get("base_url", "https://api.openai.com/v1"),
        )

    if kind == "ollama":
        credential = provider_cfg.get("credential")
        api_key = (
            cfg.get("credentials", {}).get(credential) or _read_credential(credential)
            if credential
            else ""
        )
        return OllamaCompatibleProvider(
            base_url=provider_cfg.get("base_url", "http://localhost:11434"),
            api_key=api_key,
        )

    if kind == "auth":
        protocol = provider_cfg.get("protocol", "")
        if protocol == "chatgpt_codex":
            from maurice.host.auth import refresh_chatgpt_auth
            from maurice.host.credentials import load_credentials, write_credentials, credentials_path
            import time
            credential_name = provider_cfg.get("credential", "chatgpt")
            cred_path = credentials_path()
            store = load_credentials(cred_path)
            record = store.credentials.get(credential_name)
            token = record.value if record else None
            # Refresh if expired
            if record and hasattr(record, "expires"):
                expires = float(getattr(record, "expires", 0) or 0)
                if expires and time.time() > expires - 60:
                    try:
                        refreshed = refresh_chatgpt_auth(
                            getattr(record, "refresh_token", "")
                        )
                        token = refreshed["access_token"]
                        record.__dict__.update({
                            "value": token,
                            "expires": float(time.time() + refreshed.get("expires_in", 0)),
                        })
                        write_credentials(cred_path, store)
                    except Exception:
                        pass
            if not token:
                return UnsupportedProvider(
                    code="auth_missing",
                    message="Token ChatGPT manquant ou expiré. Relance `maurice` pour te reconnecter.",
                )
            return ChatGPTCodexProvider(
                token=token,
                base_url=provider_cfg.get("base_url", "https://chatgpt.com/backend-api/codex"),
            )
        return UnsupportedProvider(
            code="auth_protocol_unknown",
            message=f"Protocole auth non supporté : {protocol}",
        )

    return MockProvider([
        {"type": "text_delta", "delta": f"Mock: {message}"},
        {"type": "status", "status": "completed"},
    ])


# --- server ------------------------------------------------------------------

class MauriceServer:
    IDLE_TIMEOUT = 600  # seconds before auto-shutdown

    def __init__(self, context: Path | MauriceContext) -> None:
        self.ctx = (
            context
            if isinstance(context, MauriceContext)
            else resolve_local_context(context)
        )
        self.cfg = self.ctx.config if isinstance(self.ctx.config, dict) else {}
        self._last_activity = time.monotonic()
        self._lock = threading.Lock()
        self._active_connections = 0
        self._active_turns: dict[tuple[str, str], threading.Event] = {}
        self._running = True

        self.ctx.sessions_path.mkdir(parents=True, exist_ok=True)

        self.registry = SkillLoader(
            self.ctx.skill_roots,
            enabled_skills=self.ctx.enabled_skills,
            scope=self.ctx.scope,
        ).load()

        from maurice.host.command_registry import CommandRegistry
        self.command_registry = CommandRegistry.from_skill_registry(self.registry)

        self.permission_context = (
            PermissionContext(
                workspace_root=str(self.ctx.content_root),
                runtime_root=str(self.ctx.runtime_root),
                agent_workspace_root=str(self.ctx.content_root),
                active_project_root=str(self.ctx.content_root),
            )
            if self.ctx.scope == "local"
            else None
        )
        self.permission_profile = self.ctx.permission_profile
        sessions_cfg = (
            KernelSessionsConfig()
            if isinstance(self.ctx.config, dict)
            else self.ctx.config.kernel.sessions
        )
        self.compaction_config = CompactionConfig(
            context_window_tokens=sessions_cfg.context_window_tokens,
            trim_threshold=sessions_cfg.trim_threshold,
            summarize_threshold=sessions_cfg.summarize_threshold,
            reset_threshold=sessions_cfg.reset_threshold,
            keep_recent_turns=sessions_cfg.keep_recent_turns,
        )

    def _make_loop(self, provider: Any, conn: socket.socket, buf: bytearray) -> AgentLoop:
        event_store = EventStore(self.ctx.events_path)
        skill_ctx = SkillContext(
            permission_context=self.permission_context,
            event_store=event_store,
            all_skill_configs=self.ctx.skills_config,
            skill_roots=self.ctx.skill_roots,
            enabled_skills=list(self.registry.loaded().keys()),
            agent_id="main",
            session_id=None,
            hooks=SkillHooks(
                context_root=str(self.ctx.context_root),
                content_root=str(self.ctx.content_root),
                state_root=str(self.ctx.state_root),
                memory_path=str(self.ctx.memory_path),
                scope=self.ctx.scope,
                lifecycle=self.ctx.lifecycle,
                vision_backend=build_vision_backend(self.ctx.skills_config.get("vision")),
            ),
        )

        def approval_callback(tool_name: str, arguments: dict, permission_class: str, reason: str) -> bool:
            _send(conn, {
                "type": "approval_required",
                "tool": tool_name,
                "label": tool_action_label(tool_name, arguments),
                "arguments": arguments,
                "class": permission_class,
                "reason": reason,
            })
            msg = _recv_line(conn, buf)
            if msg is None:
                return False
            return {
                "approved": bool(msg.get("approved", False)),
                "scope": msg.get("remember_scope") or msg.get("scope") or "exact",
            }

        def text_delta_callback(delta: str) -> None:
            _send(conn, {"type": "text_delta", "delta": delta})

        def tool_started_callback(tool_name: str, arguments: dict) -> None:
            _send(conn, {"type": "tool_started", "tool": tool_name, "arguments": arguments})

        from maurice.kernel.system_prompt import build_base_prompt
        system_prompt = build_base_prompt(
            workspace=self.ctx.content_root,
            agent_content=self.ctx.content_root,
            active_project=self.ctx.content_root,
        )

        return AgentLoop(
            provider=provider,
            registry=self.registry,
            session_store=SessionStore(self.ctx.sessions_path),
            event_store=event_store,
            permission_context=self.permission_context,
            permission_profile=self.permission_profile,
            tool_executors=self.registry.build_executor_map(skill_ctx),
            approval_store=ApprovalStore(
                self.ctx.approvals_path,
                event_store=event_store,
            ),
            model=self.cfg.get("provider", {}).get("model", "mock"),
            system_prompt=system_prompt,
            compaction_config=self.compaction_config,
            approval_callback=approval_callback,
            text_delta_callback=text_delta_callback,
            tool_started_callback=tool_started_callback,
        )

    def _handle_connection(self, conn: socket.socket) -> None:
        self._connection_opened()
        buf = bytearray()
        try:
            while True:
                msg = _recv_line(conn, buf)
                if msg is None:
                    break
                kind = msg.get("type")
                if kind == "ping":
                    _send(conn, {"type": "pong"})
                    continue
                if kind == "shutdown":
                    self._running = False
                    _send(conn, {"type": "ok"})
                    break
                if kind == "cancel_turn":
                    cancelled = self._cancel_turn(
                        agent_id=msg.get("agent_id"),
                        session_id=str(msg.get("session_id") or "default"),
                    )
                    _send(conn, {"type": "cancelled", "cancelled": cancelled})
                    continue
                if kind == "turn":
                    self._last_activity = time.monotonic()
                    self._handle_turn(conn, buf, msg)
                    self._last_activity = time.monotonic()
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._connection_closed()

    def _handle_turn(self, conn: socket.socket, buf: bytearray, msg: dict) -> None:
        message = msg.get("message", "")
        session_id = msg.get("session_id", "default")
        agent_id = msg.get("agent_id")

        # Slash command routing — dispatch through CommandRegistry before the LLM
        if message.startswith("/"):
            self._handle_command(
                conn,
                buf,
                message,
                session_id,
                source_metadata=msg.get("source_metadata"),
            )
            return

        self._run_agent_turn(
            conn,
            buf,
            message,
            session_id,
            agent_id=agent_id,
            limits=msg.get("limits"),
            source_channel=msg.get("source_channel"),
            source_peer_id=msg.get("source_peer_id"),
            source_metadata=msg.get("source_metadata"),
            message_metadata=msg.get("message_metadata"),
        )

    def _handle_command(
        self,
        conn: socket.socket,
        buf: bytearray,
        message: str,
        session_id: str,
        *,
        source_metadata: dict | None = None,
    ) -> None:
        from maurice.host.command_registry import CommandContext

        def _model_summary(agent_id: str) -> str:
            provider = self.cfg.get("provider") or {}
            kind = provider.get("type", "mock")
            model = provider.get("model", "")
            return f"{kind}:{model or 'mock'}"

        resolved_ctx = self._ctx_with_active_project(source_metadata)
        ctx = CommandContext(
            message_text=message,
            channel="cli",
            peer_id="local",
            agent_id="main",
            session_id=session_id,
            correlation_id="cli",
            callbacks=build_command_callbacks(
                resolved_ctx,
                command_registry=self.command_registry,
                model_summary=_model_summary,
            ),
        )
        try:
            result = self.command_registry.dispatch(ctx)
        except Exception as exc:
            import traceback
            _send(conn, {"type": "error", "message": traceback.format_exc()})
            _send(conn, {"type": "done", "status": "failed"})
            return

        if result is None:
            # Unknown command — fall through to LLM
            self._run_agent_turn(conn, buf, message, session_id, source_metadata=source_metadata)
            return

        # Send command response text
        if result.text:
            _send(conn, {"type": "text_delta", "delta": result.text})

        # If the command delegates to the agent (agent_prompt), run it as a turn
        agent_prompt = result.metadata.get("agent_prompt")
        if agent_prompt:
            limits = result.metadata.get("agent_limits")
            self._run_agent_turn(
                conn,
                buf,
                agent_prompt,
                session_id,
                limits=limits,
                source_metadata=source_metadata,
            )
            return

        _send(conn, {"type": "done", "status": "completed"})

    def _run_agent_turn(
        self,
        conn: socket.socket,
        buf: bytearray,
        message: str,
        session_id: str,
        *,
        agent_id: str | None = None,
        limits: dict | None = None,
        source_channel: str | None = None,
        source_peer_id: str | None = None,
        source_metadata: dict | None = None,
        message_metadata: dict | None = None,
    ) -> None:
        cancel_event = self._register_turn(agent_id=agent_id or "main", session_id=session_id)
        if self.ctx.scope == "global":
            try:
                self._run_global_agent_turn(
                    conn,
                    buf,
                    message,
                    session_id,
                    agent_id=agent_id,
                    limits=limits,
                    source_channel=source_channel,
                    source_peer_id=source_peer_id,
                    source_metadata=source_metadata,
                    message_metadata=message_metadata,
                    cancel_event=cancel_event,
                )
            finally:
                self._unregister_turn(agent_id=agent_id or "main", session_id=session_id, event=cancel_event)
            return
        provider = _build_provider(self.cfg, message)
        loop = self._make_loop(provider, conn, buf)
        try:
            result = loop.run_turn(
                agent_id="main",
                session_id=session_id,
                message=message,
                limits=limits,
                message_metadata=message_metadata,
                cancel_event=cancel_event,
            )
            if result.error:
                _send(conn, {"type": "error", "message": result.error})
            for tr in result.tool_results:
                _send(conn, {
                    "type": "tool_result",
                    "summary": tr.summary,
                    "ok": tr.ok,
                    "error": tr.error.code if tr.error else None,
                    "artifacts": [artifact.model_dump(mode="json") for artifact in tr.artifacts],
                })
            _send(conn, {
                "type": "done",
                "status": result.status,
                "correlation_id": result.correlation_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            })
        except Exception as exc:
            import traceback
            _send(conn, {"type": "error", "message": traceback.format_exc()})
            _send(conn, {"type": "done", "status": "failed"})
        finally:
            self._unregister_turn(agent_id=agent_id or "main", session_id=session_id, event=cancel_event)

    def _ctx_with_active_project(self, source_metadata: dict | None) -> MauriceContext:
        if self.ctx.scope != "global" or not isinstance(source_metadata, dict):
            return self.ctx
        active_project = source_metadata.get("active_project_root")
        if not isinstance(active_project, str) or not active_project.strip():
            return self.ctx
        return resolve_global_context(
            self.ctx.context_root,
            bundle=self.ctx.config if not isinstance(self.ctx.config, dict) else None,
            lifecycle=self.ctx.lifecycle,
            active_project=Path(active_project),
        )

    def _run_global_agent_turn(
        self,
        conn: socket.socket,
        buf: bytearray,
        message: str,
        session_id: str,
        *,
        agent_id: str | None = None,
        limits: dict | None = None,
        source_channel: str | None = None,
        source_peer_id: str | None = None,
        source_metadata: dict | None = None,
        message_metadata: dict | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        def approval_callback(tool_name: str, arguments: dict, permission_class: str, reason: str) -> bool:
            _send(conn, {
                "type": "approval_required",
                "tool": tool_name,
                "label": tool_action_label(tool_name, arguments),
                "arguments": arguments,
                "class": permission_class,
                "reason": reason,
            })
            msg = _recv_line(conn, buf)
            if msg is None:
                return False
            return {
                "approved": bool(msg.get("approved", False)),
                "scope": msg.get("remember_scope") or msg.get("scope") or "exact",
            }

        def text_delta_callback(delta: str) -> None:
            _send(conn, {"type": "text_delta", "delta": delta})

        def tool_started_callback(tool_name: str, arguments: dict) -> None:
            _send(conn, {"type": "tool_started", "tool": tool_name, "arguments": arguments})

        try:
            loop, agent = build_global_agent_loop(
                ctx=self.ctx,
                message=message,
                session_id=session_id,
                agent_id=agent_id,
                source_channel=source_channel,
                source_peer_id=source_peer_id,
                source_metadata=source_metadata,
                approval_callback=approval_callback,
                text_delta_callback=text_delta_callback,
                tool_started_callback=tool_started_callback,
            )
            result = loop.run_turn(
                agent_id=agent.id,
                session_id=session_id,
                message=message,
                limits=limits,
                message_metadata=message_metadata,
                cancel_event=cancel_event,
            )
            if result.error:
                _send(conn, {"type": "error", "message": result.error})
            for tr in result.tool_results:
                _send(conn, {
                    "type": "tool_result",
                    "summary": tr.summary,
                    "ok": tr.ok,
                    "error": tr.error.code if tr.error else None,
                    "artifacts": [artifact.model_dump(mode="json") for artifact in tr.artifacts],
                })
            _send(conn, {
                "type": "done",
                "status": result.status,
                "correlation_id": result.correlation_id,
                "assistant_text": result.assistant_text,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            })
        except Exception:
            import traceback
            _send(conn, {"type": "error", "message": traceback.format_exc()})
            _send(conn, {"type": "done", "status": "failed"})

    def _register_turn(self, *, agent_id: str, session_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._active_turns[(agent_id, session_id)] = event
        return event

    def _unregister_turn(self, *, agent_id: str, session_id: str, event: threading.Event) -> None:
        with self._lock:
            if self._active_turns.get((agent_id, session_id)) is event:
                del self._active_turns[(agent_id, session_id)]

    def _cancel_turn(self, *, agent_id: Any, session_id: str) -> bool:
        key = (str(agent_id or "main"), session_id)
        with self._lock:
            event = self._active_turns.get(key)
        if event is None:
            return False
        event.set()
        return True

    def _idle_watchdog(self) -> None:
        while self._running:
            time.sleep(30)
            if self._should_stop_for_idle(time.monotonic()):
                self._running = False
                os.kill(os.getpid(), signal.SIGTERM)

    def _connection_opened(self) -> None:
        with self._lock:
            self._active_connections += 1
            self._last_activity = time.monotonic()

    def _connection_closed(self) -> None:
        with self._lock:
            self._active_connections = max(0, self._active_connections - 1)
            self._last_activity = time.monotonic()

    def _should_stop_for_idle(self, now: float) -> bool:
        with self._lock:
            if self._active_connections:
                return False
            return now - self._last_activity > self.IDLE_TIMEOUT

    def serve(self, socket_path: Path) -> None:
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(socket_path))
        srv.listen(8)
        srv.settimeout(0.2)

        if self.ctx.lifecycle == "transient":
            threading.Thread(target=self._idle_watchdog, daemon=True).start()

        def _shutdown(*_: Any) -> None:
            self._running = False

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

        while self._running:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()
            except TimeoutError:
                continue
            except OSError:
                break

        srv.close()
        if socket_path.exists():
            socket_path.unlink()


def main() -> None:
    """Entry point when spawned as a subprocess."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--global":
        workspace = Path(sys.argv[2]).resolve()
        bundle = load_workspace_config(workspace)
        agent = next((item for item in bundle.agents.agents.values() if item.default), None)
        ctx = resolve_global_context(workspace, agent=agent, bundle=bundle)
        MauriceServer(ctx).serve(ctx.server_socket_path)
        return
    if len(sys.argv) < 2:
        print("usage: maurice-server [--global <workspace_root>] <project_root>", file=sys.stderr)
        sys.exit(1)
    project_root = Path(sys.argv[1]).resolve()
    ctx = resolve_local_context(project_root)
    MauriceServer(ctx).serve(ctx.server_socket_path)


if __name__ == "__main__":
    main()
