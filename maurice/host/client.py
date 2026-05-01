"""Maurice socket client — connects to the background server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


SOCKET_RELATIVE = Path(".maurice") / "run" / "server.socket"
PID_RELATIVE = Path(".maurice") / "run" / "server.pid"

_START_TIMEOUT = 8.0   # seconds to wait for server to be ready
_POLL_INTERVAL = 0.1


def _socket_path(project_root: Path) -> Path:
    return project_root / SOCKET_RELATIVE


def _pid_path(project_root: Path) -> Path:
    return project_root / PID_RELATIVE


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


class MauriceClient:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # ------------------------------------------------------------------
    # lifecycle

    def is_running(self) -> bool:
        sp = _socket_path(self.project_root)
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
        run_dir = self.project_root / ".maurice" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "server.log"
        log_file = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "maurice.host.server", str(self.project_root)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        pid_path = _pid_path(self.project_root)
        pid_path.write_text(str(proc.pid), encoding="utf-8")

        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self.is_running():
                return
            time.sleep(_POLL_INTERVAL)
        raise RuntimeError(f"Maurice server did not start within {_START_TIMEOUT}s. Check {log_path}.")

    def ensure_running(self) -> None:
        """Start the server, restarting it if already running (picks up code changes)."""
        if self.is_running():
            self._kill_existing()
        self.start_server()

    def _kill_existing(self) -> None:
        """Gracefully shut down the running server without calling ensure_running."""
        sp = _socket_path(self.project_root)
        pid_path = _pid_path(self.project_root)
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
        for path in (sp, pid_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def connect(self) -> None:
        sp = _socket_path(self.project_root)
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

    # ------------------------------------------------------------------
    # turn

    def run_turn(
        self,
        message: str,
        *,
        session_id: str = "default",
        approval_callback: Any = None,   # callable(tool, args, class, reason) -> bool
    ) -> Iterator[dict[str, Any]]:
        """Stream events from a turn. Yields dicts with type field.

        Event types:
          text_delta    — {"type": "text_delta", "delta": str}
          tool_result   — {"type": "tool_result", "summary": str, "ok": bool, "error": str|None}
          approval_required — {"type": "approval_required", "tool": str, ...}
          done          — {"type": "done", "status": str}
          error         — {"type": "error", "message": str}
        """
        assert self._sock is not None, "call connect() first"
        _send(self._sock, {"type": "turn", "message": message, "session_id": session_id})
        while True:
            msg = _recv_line(self._sock, self._buf)
            if msg is None:
                break
            if msg.get("type") == "approval_required":
                approved = False
                if approval_callback is not None:
                    approved = approval_callback(
                        msg.get("tool", ""),
                        msg.get("arguments", {}),
                        msg.get("class", ""),
                        msg.get("reason", ""),
                    )
                _send(self._sock, {"type": "approval", "approved": approved})
                yield msg
                continue
            yield msg
            if msg.get("type") == "done":
                break
