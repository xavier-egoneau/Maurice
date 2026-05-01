"""Tests for the Unix socket server, client, and project root resolution."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from maurice.host.project import (
    ensure_maurice_dir,
    find_project_root,
    resolve_project_root,
    config_path,
    sessions_dir,
    events_path,
    approvals_path,
    maurice_dir,
    GITIGNORE_CONTENT,
)
from maurice.host.server import MauriceServer, _send, _recv_line
from maurice.host.client import MauriceClient


# ---------------------------------------------------------------------------
# project root resolution

class TestFindProjectRoot:
    def test_finds_git_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        assert find_project_root(sub) == tmp_path

    def test_returns_none_when_no_git(self, tmp_path):
        assert find_project_root(tmp_path) is None

    def test_stops_at_filesystem_root(self, tmp_path):
        # Should not raise, just return None
        result = find_project_root(Path("/"))
        assert result is None or result == Path("/")

    def test_git_root_is_the_direct_cwd(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert find_project_root(tmp_path) == tmp_path


class TestResolveProjectRoot:
    def test_returns_git_root_without_confirm(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "src"
        sub.mkdir()
        result = resolve_project_root(sub, confirm=False)
        assert result == tmp_path

    def test_returns_cwd_when_no_git_and_confirm_false(self, tmp_path):
        result = resolve_project_root(tmp_path, confirm=False)
        assert result == tmp_path


class TestEnsureMauriceDir:
    def test_creates_dir(self, tmp_path):
        m = ensure_maurice_dir(tmp_path)
        assert m.is_dir()
        assert m == tmp_path / ".maurice"

    def test_creates_gitignore(self, tmp_path):
        ensure_maurice_dir(tmp_path)
        gi = tmp_path / ".maurice" / ".gitignore"
        assert gi.exists()
        assert gi.read_text() == GITIGNORE_CONTENT

    def test_idempotent(self, tmp_path):
        ensure_maurice_dir(tmp_path)
        ensure_maurice_dir(tmp_path)  # should not raise
        assert (tmp_path / ".maurice").is_dir()

    def test_does_not_overwrite_existing_gitignore(self, tmp_path):
        ensure_maurice_dir(tmp_path)
        gi = tmp_path / ".maurice" / ".gitignore"
        gi.write_text("custom\n")
        ensure_maurice_dir(tmp_path)
        assert gi.read_text() == "custom\n"


class TestProjectPaths:
    def test_sessions_dir(self, tmp_path):
        assert sessions_dir(tmp_path) == tmp_path / ".maurice" / "sessions"

    def test_events_path(self, tmp_path):
        assert events_path(tmp_path) == tmp_path / ".maurice" / "events.jsonl"

    def test_approvals_path(self, tmp_path):
        assert approvals_path(tmp_path) == tmp_path / ".maurice" / "approvals.json"

    def test_config_path(self, tmp_path):
        assert config_path(tmp_path) == tmp_path / ".maurice" / "config.yaml"


# ---------------------------------------------------------------------------
# protocol helpers

class TestProtocol:
    def _loopback(self):
        """Returns a connected (server_conn, client_conn) socket pair."""
        a, b = socket.socketpair()
        return a, b

    def test_send_recv_roundtrip(self):
        a, b = self._loopback()
        _send(a, {"type": "ping", "x": 42})
        buf = bytearray()
        msg = _recv_line(b, buf)
        assert msg == {"type": "ping", "x": 42}
        a.close()
        b.close()

    def test_recv_multiple_messages(self):
        a, b = self._loopback()
        _send(a, {"type": "a"})
        _send(a, {"type": "b"})
        buf = bytearray()
        m1 = _recv_line(b, buf)
        m2 = _recv_line(b, buf)
        assert m1["type"] == "a"
        assert m2["type"] == "b"
        a.close()
        b.close()

    def test_recv_returns_none_on_closed(self):
        a, b = self._loopback()
        a.close()
        buf = bytearray()
        assert _recv_line(b, buf) is None
        b.close()


# ---------------------------------------------------------------------------
# server + client integration

def _write_mock_config(project_root: Path) -> None:
    ensure_maurice_dir(project_root)
    cfg = config_path(project_root)
    cfg.write_text("provider:\n  type: mock\n", encoding="utf-8")


def _start_server_thread(server: MauriceServer, socket_path: Path) -> threading.Thread:
    t = threading.Thread(target=server.serve, args=(socket_path,), daemon=True)
    t.start()
    # wait until socket is ready
    for _ in range(50):
        if socket_path.exists():
            time.sleep(0.05)
            break
        time.sleep(0.05)
    return t


def _run_connection(server: MauriceServer, server_sock: socket.socket) -> None:
    """Run _handle_connection in the current thread (for testing)."""
    server._handle_connection(server_sock)


class TestMauriceServer:
    def _make_pair(self) -> tuple[socket.socket, socket.socket]:
        """Return a (server_side, client_side) connected socket pair."""
        return socket.socketpair()

    def test_ping_pong(self, tmp_path):
        _write_mock_config(tmp_path)
        server = MauriceServer(tmp_path)
        srv_sock, cli_sock = self._make_pair()

        t = threading.Thread(target=_run_connection, args=(server, srv_sock), daemon=True)
        t.start()

        buf = bytearray()
        _send(cli_sock, {"type": "ping"})
        msg = _recv_line(cli_sock, buf)
        assert msg == {"type": "pong"}

        cli_sock.close()
        t.join(timeout=2.0)

    def test_turn_returns_done(self, tmp_path):
        _write_mock_config(tmp_path)
        server = MauriceServer(tmp_path)
        srv_sock, cli_sock = self._make_pair()

        t = threading.Thread(target=_run_connection, args=(server, srv_sock), daemon=True)
        t.start()

        buf = bytearray()
        _send(cli_sock, {"type": "turn", "message": "hello", "session_id": "t1"})

        events = []
        while True:
            msg = _recv_line(cli_sock, buf)
            events.append(msg)
            if msg["type"] == "done":
                break

        cli_sock.close()
        t.join(timeout=5.0)

        types = [e["type"] for e in events]
        assert "done" in types
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "completed"

    def test_shutdown(self, tmp_path):
        _write_mock_config(tmp_path)
        server = MauriceServer(tmp_path)
        srv_sock, cli_sock = self._make_pair()

        t = threading.Thread(target=_run_connection, args=(server, srv_sock), daemon=True)
        t.start()

        buf = bytearray()
        _send(cli_sock, {"type": "shutdown"})
        msg = _recv_line(cli_sock, buf)
        assert msg == {"type": "ok"}
        cli_sock.close()
        t.join(timeout=2.0)
        assert not server._running

    def test_approval_protocol_roundtrip(self, tmp_path):
        """Protocol: client handles approval_required, sends approval, gets done."""
        srv_sock, cli_sock = self._make_pair()

        def fake_server(conn: socket.socket) -> None:
            buf = bytearray()
            _recv_line(conn, buf)  # consume the turn request
            _send(conn, {"type": "approval_required", "tool": "filesystem.write",
                         "arguments": {"path": "x"}, "class": "fs.write", "reason": "test"})
            resp = _recv_line(conn, bytearray())
            status = "completed" if resp and resp.get("approved") else "blocked"
            _send(conn, {"type": "done", "status": status})

        t = threading.Thread(target=fake_server, args=(srv_sock,), daemon=True)
        t.start()

        approved_tools: list[str] = []

        def my_approval(tool, args, cls, reason):
            approved_tools.append(tool)
            return True

        client = MauriceClient(tmp_path)
        client._sock = cli_sock
        client._buf = bytearray()
        events = list(client.run_turn("x", session_id="s", approval_callback=my_approval))
        cli_sock.close()
        t.join(timeout=2.0)

        assert "filesystem.write" in approved_tools
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "completed"


class TestMauriceClient:
    def test_is_running_false_when_no_socket(self, tmp_path):
        client = MauriceClient(tmp_path)
        assert not client.is_running()

    def test_run_turn_streams_done(self, tmp_path):
        _write_mock_config(tmp_path)
        server = MauriceServer(tmp_path)
        srv_sock, cli_sock = self._make_pair()

        t = threading.Thread(target=_run_connection, args=(server, srv_sock), daemon=True)
        t.start()

        client = MauriceClient(tmp_path)
        client._sock = cli_sock
        client._buf = bytearray()

        # Send the turn request manually since we bypassed connect()
        _send(cli_sock, {"type": "turn", "message": "bonjour", "session_id": "s1"})
        events = []
        buf = bytearray()
        while True:
            msg = _recv_line(cli_sock, buf)
            events.append(msg)
            if msg["type"] == "done":
                break

        cli_sock.close()
        t.join(timeout=5.0)

        types = [e["type"] for e in events]
        assert "done" in types
        assert any(e["type"] == "text_delta" for e in events)

    def _make_pair(self):
        return socket.socketpair()
