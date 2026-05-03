"""Maurice socket client — connects to the background server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from maurice import __version__
from maurice.host.context import MauriceContext, resolve_local_context


_START_TIMEOUT = 8.0   # seconds to wait for server to be ready
_POLL_INTERVAL = 0.1


def _send(sock: socket.socket, msg: dict[str, Any]) -> None:
    sock.sendall((json.dumps(msg) + "\n").encode())


def _recv_line(sock: socket.socket, buf: bytearray) -> dict[str, Any] | None:
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)
    idx = buf.index(b"\n")
    line = buf[:idx]
    del buf[:idx + 1]
    return json.loads(line.decode())


def _desired_server_meta(ctx: MauriceContext) -> dict[str, Any]:
    return {
        "scope": ctx.scope,
        "lifecycle": ctx.lifecycle,
        "context_root": str(ctx.context_root),
        "state_root": str(ctx.state_root),
        "content_root": str(ctx.content_root),
        "config_hash": _config_hash(ctx),
        "runtime_hash": _runtime_hash(),
    }


def _server_meta_compatible(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def _config_hash(ctx: MauriceContext) -> str:
    payload = {
        "config": ctx.config,
        "skill_roots": [
            {
                "path": root.path,
                "origin": root.origin,
                "mutable": root.mutable,
            }
            for root in ctx.skill_roots
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _runtime_hash() -> str:
    hasher = hashlib.sha256(__version__.encode("utf-8"))
    for path in _runtime_hash_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        hasher.update(str(path.resolve()).encode("utf-8"))
        hasher.update(str(stat.st_mtime_ns).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
    return hasher.hexdigest()


def _runtime_hash_files() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    system_skill_files = sorted(
        path
        for path in (package_root / "system_skills").rglob("*")
        if path.suffix in {".py", ".yaml", ".md"}
    )
    files = [
        Path(__file__),
        Path(__file__).with_name("server.py"),
        Path(__file__).with_name("context.py"),
        Path(__file__).with_name("web_ui.html"),
        package_root / "kernel" / "providers.py",
        package_root / "kernel" / "loop.py",
        package_root / "kernel" / "permissions.py",
        package_root / "kernel" / "skills.py",
        package_root / "host" / "autonomy.py",
        package_root / "host" / "gateway.py",
        package_root / "host" / "vision_backend.py",
        package_root / "host" / "commands" / "gateway_server.py",
    ]
    return [*files, *system_skill_files]


class MauriceClient:
    def __init__(self, context: Path | MauriceContext) -> None:
        self.ctx = (
            context
            if isinstance(context, MauriceContext)
            else resolve_local_context(context.resolve())
        )
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # ------------------------------------------------------------------
    # lifecycle

    def is_running(self) -> bool:
        sp = self.ctx.server_socket_path
        if not sp.exists():
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(str(sp))
            _send(s, {"type": "ping"})
            data = bytearray()
            msg = _recv_line(s, data)
            s.close()
            return msg is not None and msg.get("type") == "pong"
        except OSError:
            return False

    def start_server(self) -> None:
        """Spawn the server subprocess and wait until it's ready."""
        run_dir = self.ctx.run_root
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "server.log"
        log_file = open(log_path, "a", encoding="utf-8")
        if self.ctx.scope == "global":
            command = [
                sys.executable,
                "-m",
                "maurice.host.server",
                "--global",
                str(self.ctx.context_root),
            ]
        else:
            command = [sys.executable, "-m", "maurice.host.server", str(self.ctx.context_root)]
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        pid_path = self.ctx.server_pid_path
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        self._write_meta(proc.pid)

        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self.is_running():
                return
            time.sleep(_POLL_INTERVAL)
        raise RuntimeError(f"Maurice server did not start within {_START_TIMEOUT}s. Check {log_path}.")

    def ensure_running(self) -> None:
        """Start the server, reusing it when the resolved context is compatible."""
        if self.is_running():
            if not os.environ.get("MAURICE_DEV") and self._meta_compatible():
                return
            self._kill_existing()
        self.start_server()

    def _kill_existing(self) -> None:
        """Gracefully shut down the running server without calling ensure_running."""
        sp = self.ctx.server_socket_path
        pid_path = self.ctx.server_pid_path
        # Try graceful shutdown via socket directly
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(str(sp))
            _send(s, {"type": "shutdown"})
            s.close()
            # Wait for socket to disappear
            for _ in range(20):
                if not sp.exists():
                    break
                time.sleep(0.1)
        except OSError:
            pass
        # Fall back to SIGTERM
        try:
            pid = int(pid_path.read_text().strip())
            import os, signal
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
        except Exception:
            pass
        # Clean up leftover files
        for path in (sp, pid_path, self.ctx.server_meta_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _meta_compatible(self) -> bool:
        try:
            current = json.loads(self.ctx.server_meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return _server_meta_compatible(current, _desired_server_meta(self.ctx))

    def _write_meta(self, pid: int) -> None:
        meta = {
            **_desired_server_meta(self.ctx),
            "pid": pid,
            "started_at": time.time(),
        }
        path = self.ctx.server_meta_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    def connect(self) -> None:
        sp = self.ctx.server_socket_path
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sp))
        self._sock = s
        self._buf = bytearray()

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def shutdown_server(self) -> None:
        self.ensure_running()
        self.connect()
        try:
            _send(self._sock, {"type": "shutdown"})
            _recv_line(self._sock, self._buf)
        finally:
            self.close()

    def cancel_turn(self, *, agent_id: str | None = None, session_id: str = "default") -> bool:
        if not self.is_running():
            return False
        self.connect()
        try:
            payload: dict[str, Any] = {
                "type": "cancel_turn",
                "session_id": session_id,
            }
            if agent_id is not None:
                payload["agent_id"] = agent_id
            _send(self._sock, payload)
            response = _recv_line(self._sock, self._buf) or {}
            return bool(response.get("cancelled"))
        finally:
            self.close()

    # ------------------------------------------------------------------
    # turn

    def run_turn(
        self,
        message: str,
        *,
        session_id: str = "default",
        agent_id: str | None = None,
        source_channel: str | None = None,
        source_peer_id: str | None = None,
        source_metadata: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        message_metadata: dict[str, Any] | None = None,
        approval_callback: Any = None,   # callable(tool, args, class, reason) -> bool
        approval_mode: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream events from a turn. Yields dicts with type field.

        Event types:
          text_delta    — {"type": "text_delta", "delta": str}
          tool_result   — {"type": "tool_result", "summary": str, "ok": bool,
                           "error": str|None, "artifacts": list}
          approval_required — {"type": "approval_required", "tool": str, ...}
          done          — {"type": "done", "status": str}
          error         — {"type": "error", "message": str}
        """
        assert self._sock is not None, "call connect() first"
        payload: dict[str, Any] = {
            "type": "turn",
            "message": message,
            "session_id": session_id,
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if source_channel is not None:
            payload["source_channel"] = source_channel
        if source_peer_id is not None:
            payload["source_peer_id"] = source_peer_id
        if source_metadata is not None:
            payload["source_metadata"] = source_metadata
        if limits is not None:
            payload["limits"] = limits
        if message_metadata is not None:
            payload["message_metadata"] = message_metadata
        if approval_mode is not None:
            payload["approval_mode"] = approval_mode
        _send(self._sock, payload)
        while True:
            msg = _recv_line(self._sock, self._buf)
            if msg is None:
                break
            if msg.get("type") == "approval_required":
                approved: bool | str | dict[str, Any] = False
                if approval_callback is not None:
                    approved = approval_callback(
                        msg.get("tool", ""),
                        msg.get("arguments", {}),
                        msg.get("class", ""),
                        msg.get("reason", ""),
                    )
                if isinstance(approved, dict):
                    response = {"type": "approval", **approved}
                elif isinstance(approved, str):
                    response = {
                        "type": "approval",
                        "approved": approved.strip().lower() in {"y", "yes", "o", "oui", "ok", "session", "tool_session"},
                        "remember_scope": "session" if approved.strip().lower() in {"session", "tool_session"} else "exact",
                    }
                else:
                    response = {"type": "approval", "approved": bool(approved)}
                _send(self._sock, response)
                yield msg
                continue
            yield msg
            if msg.get("type") == "done":
                break
