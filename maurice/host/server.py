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

from maurice.host.project import (
    approvals_path,
    events_path,
    global_config_path,
    config_path,
    sessions_dir,
    ensure_maurice_dir,
)
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.config import KernelSessionsConfig
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader, SkillRoot


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


# --- config ------------------------------------------------------------------

def _load_project_config(project_root: Path) -> dict[str, Any]:
    """Merge global ~/.maurice/config.yaml with local .maurice/config.yaml."""
    import yaml

    cfg: dict[str, Any] = {}
    for path in (global_config_path(), config_path(project_root)):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg.update(data)
    return cfg


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
        return OllamaCompatibleProvider(
            base_url=provider_cfg.get("base_url", "http://localhost:11434"),
            api_key="",
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

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.cfg = _load_project_config(self.project_root)
        self._last_activity = time.monotonic()
        self._lock = threading.Lock()
        self._running = True

        ensure_maurice_dir(self.project_root)
        sessions_dir(self.project_root).mkdir(parents=True, exist_ok=True)

        from maurice.host.paths import maurice_home
        runtime_root = str(Path(sys.modules["maurice"].__file__).parent.parent)

        skill_roots = [
            SkillRoot(path=str(Path(__file__).parent.parent / "system_skills"), origin="system", mutable=False),
        ]
        enabled = self.cfg.get("skills") or None
        self.registry = SkillLoader(skill_roots, enabled_skills=enabled).load()

        from maurice.host.command_registry import CommandRegistry
        self.command_registry = CommandRegistry.from_skill_registry(self.registry)

        self.permission_context = PermissionContext(
            workspace_root=str(self.project_root),
            runtime_root=runtime_root,
            agent_workspace_root=str(self.project_root),
            active_project_root=str(self.project_root),
        )
        self.permission_profile = self.cfg.get("permission_profile", "limited")
        sessions_cfg = KernelSessionsConfig()
        self.compaction_config = CompactionConfig(
            context_window_tokens=sessions_cfg.context_window_tokens,
            trim_threshold=sessions_cfg.trim_threshold,
            summarize_threshold=sessions_cfg.summarize_threshold,
            reset_threshold=sessions_cfg.reset_threshold,
            keep_recent_turns=sessions_cfg.keep_recent_turns,
        )

    def _make_loop(self, provider: Any, conn: socket.socket, buf: bytearray) -> AgentLoop:
        event_store = EventStore(events_path(self.project_root))
        skill_ctx = SkillContext(
            permission_context=self.permission_context,
            event_store=event_store,
            all_skill_configs=self.cfg.get("skills_config", {}),
            skill_roots=[],
            enabled_skills=list(self.registry.loaded().keys()),
            agent_id="main",
            session_id=None,
        )

        def approval_callback(tool_name: str, arguments: dict, permission_class: str, reason: str) -> bool:
            _send(conn, {
                "type": "approval_required",
                "tool": tool_name,
                "arguments": arguments,
                "class": permission_class,
                "reason": reason,
            })
            msg = _recv_line(conn, buf)
            if msg is None:
                return False
            return bool(msg.get("approved", False))

        def text_delta_callback(delta: str) -> None:
            _send(conn, {"type": "text_delta", "delta": delta})

        def tool_started_callback(tool_name: str, arguments: dict) -> None:
            _send(conn, {"type": "tool_started", "tool": tool_name, "arguments": arguments})

        from maurice.kernel.system_prompt import build_base_prompt
        system_prompt = build_base_prompt(
            workspace=self.project_root,
            agent_content=self.project_root,
            active_project=self.project_root,
        )

        return AgentLoop(
            provider=provider,
            registry=self.registry,
            session_store=SessionStore(sessions_dir(self.project_root)),
            event_store=event_store,
            permission_context=self.permission_context,
            permission_profile=self.permission_profile,
            tool_executors=self.registry.build_executor_map(skill_ctx),
            approval_store=ApprovalStore(
                approvals_path(self.project_root),
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

    def _handle_turn(self, conn: socket.socket, buf: bytearray, msg: dict) -> None:
        message = msg.get("message", "")
        session_id = msg.get("session_id", "default")

        # Slash command routing — dispatch through CommandRegistry before the LLM
        if message.startswith("/"):
            self._handle_command(conn, buf, message, session_id)
            return

        self._run_agent_turn(conn, buf, message, session_id)

    def _handle_command(
        self, conn: socket.socket, buf: bytearray, message: str, session_id: str
    ) -> None:
        from maurice.host.command_registry import CommandContext

        def _compact_session(agent_id: str, sid: str) -> str:
            from maurice.kernel.session import SessionStore
            from maurice.host.output import _compact_text
            store = SessionStore(sessions_dir(self.project_root))
            try:
                session = store.load(agent_id, sid)
            except FileNotFoundError:
                return "Session vide. Rien à compacter."
            if not session.messages:
                return "Session vide. Rien à compacter."
            n = len(session.messages)
            u = sum(1 for m in session.messages if m.role == "user")
            a = sum(1 for m in session.messages if m.role == "assistant")
            recent = [
                f"- {m.role}: {_compact_text(m.content, 200)}"
                for m in session.messages[-6:]
            ]
            store.reset(agent_id, sid)
            return (
                f"Session compactée — {n} messages ({u} user, {a} assistant) effacés.\n\n"
                "Derniers éléments conservés :\n" + "\n".join(recent)
            )

        ctx = CommandContext(
            message_text=message,
            channel="cli",
            peer_id="local",
            agent_id="main",
            session_id=session_id,
            correlation_id="cli",
            callbacks={
                "workspace": str(self.project_root),
                "agent_workspace": str(self.project_root),
                "project_root": str(self.project_root),
                "compact_session": _compact_session,
            },
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
            self._run_agent_turn(conn, buf, message, session_id)
            return

        # Send command response text
        if result.text:
            _send(conn, {"type": "text_delta", "delta": result.text})

        # If the command delegates to the agent (agent_prompt), run it as a turn
        agent_prompt = result.metadata.get("agent_prompt")
        if agent_prompt:
            limits = result.metadata.get("agent_limits")
            self._run_agent_turn(conn, buf, agent_prompt, session_id, limits=limits)
            return

        _send(conn, {"type": "done", "status": "completed"})

    def _run_agent_turn(
        self,
        conn: socket.socket,
        buf: bytearray,
        message: str,
        session_id: str,
        *,
        limits: dict | None = None,
    ) -> None:
        provider = _build_provider(self.cfg, message)
        loop = self._make_loop(provider, conn, buf)
        try:
            result = loop.run_turn(
                agent_id="main",
                session_id=session_id,
                message=message,
                limits=limits,
            )
            if result.error:
                _send(conn, {"type": "error", "message": result.error})
            for tr in result.tool_results:
                _send(conn, {"type": "tool_result", "summary": tr.summary, "ok": tr.ok,
                             "error": tr.error.code if tr.error else None})
            _send(conn, {
                "type": "done",
                "status": result.status,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            })
        except Exception as exc:
            import traceback
            _send(conn, {"type": "error", "message": traceback.format_exc()})
            _send(conn, {"type": "done", "status": "failed"})

    def _idle_watchdog(self) -> None:
        while self._running:
            time.sleep(30)
            if time.monotonic() - self._last_activity > self.IDLE_TIMEOUT:
                self._running = False
                os.kill(os.getpid(), signal.SIGTERM)

    def serve(self, socket_path: Path) -> None:
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(socket_path))
        srv.listen(8)
        srv.settimeout(2.0)

        threading.Thread(target=self._idle_watchdog, daemon=True).start()

        def _shutdown(*_: Any) -> None:
            self._running = False

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
    """Entry point when spawned as a subprocess: python -m maurice.host.server <project_root>."""
    if len(sys.argv) < 2:
        print("usage: maurice-server <project_root>", file=sys.stderr)
        sys.exit(1)
    project_root = Path(sys.argv[1]).resolve()
    socket_path = project_root / ".maurice" / "run" / "server.socket"
    MauriceServer(project_root).serve(socket_path)


if __name__ == "__main__":
    main()
