from __future__ import annotations

import json
import errno
from pathlib import Path

import pytest

from maurice.host.agents import update_agent
from maurice.kernel.approvals import ApprovalStore
from maurice.host.cli import build_parser, main
from maurice.host.cli import (
    _deliver_daily_digest,
    _ensure_configured_scheduler_jobs,
    _ollama_model_choices,
    _scheduler_handlers,
    run_one_turn,
)
from maurice.host.credentials import load_workspace_credentials
from maurice.host.paths import host_config_path
from maurice.host.project import global_config_path
from maurice.host.project_registry import list_known_projects
from maurice.host.secret_capture import request_secret_capture
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.scheduler import JobStatus, JobStore
from maurice.kernel.session import SessionStore


def test_cli_setup_runs_unified_setup(monkeypatch) -> None:
    called = []
    monkeypatch.setattr("maurice.host.cli.run_setup", lambda: called.append(True) or True)

    assert main(["setup"]) == 0

    assert called == [True]


def test_cli_help_exposes_setup_not_onboard(capsys) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])

    output = capsys.readouterr().out
    assert "setup" in output
    assert "Dossier : cd /chemin/du/projet && maurice" in output
    assert "Bureau : maurice setup" in output
    assert "onboard" not in output


def test_cli_onboard_doctor_and_run(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"

    assert main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"]) == 0
    onboard_output = capsys.readouterr().out
    assert "workspace initialized" in onboard_output

    bundle = load_workspace_config(workspace)
    assert bundle.kernel.permissions.profile == "limited"
    assert bundle.agents.agents["main"].credentials == []
    assert bundle.kernel.scheduler.dreaming_time == "09:00"
    assert bundle.kernel.scheduler.daily_time == "09:30"

    assert main(["doctor", "--workspace", str(workspace)]) == 0
    doctor_output = capsys.readouterr().out
    assert "workspace OK" in doctor_output

    assert main(["run", "--workspace", str(workspace), "--message", "bonjour"]) == 0
    run_output = capsys.readouterr().out
    assert "Mock response: bonjour" in run_output

    assert (workspace / "sessions" / "main" / "default.json").is_file()
    assert (workspace / "agents" / "main" / "events.jsonl").is_file()


def test_cli_interactive_onboard_configures_basics(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    answers = iter(
        [
            "limited",
            "ollama",
            "18792",
            "",
            "auto_heberge",
            "http://localhost:11434",
            "llama3.2",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--interactive", "--workspace", str(workspace)]) == 0
    output = capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert "Maurice onboarding complete." in output
    assert bundle.host.gateway.port == 18792
    assert bundle.kernel.model.provider == "ollama"
    assert bundle.kernel.model.protocol == "ollama_chat"
    assert bundle.kernel.model.name == "llama3.2"
    assert bundle.skills.skills["web"]["search_provider"] == "searxng"
    assert bundle.skills.skills["web"]["base_url"] == "http://localhost:8080"


def test_cli_interactive_onboard_keeps_existing_values_on_enter(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    first_answers = iter(
        [
            "limited",
            "ollama",
            "18792",
            "",
            "auto_heberge",
            "http://localhost:11434",
            "llama3.2",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(first_answers))

    assert main(["onboard", "--interactive", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    second_answers = iter(["", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(second_answers))

    assert main(["onboard", "--interactive", "--workspace", str(workspace)]) == 0
    output = capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert "Maurice onboarding complete." in output
    assert bundle.kernel.permissions.profile == "limited"
    assert bundle.host.gateway.port == 18792
    assert bundle.kernel.model.provider == "ollama"
    assert bundle.kernel.model.protocol == "ollama_chat"
    assert bundle.kernel.model.name == "llama3.2"
    assert bundle.kernel.model.base_url == "http://localhost:11434"
    assert bundle.skills.skills["web"]["search_provider"] == "searxng"
    assert bundle.skills.skills["web"]["base_url"] == "http://localhost:8080"


def test_cli_onboard_agent_model_updates_only_agent_model(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    main(["agents", "create", "coding", "--workspace", str(workspace)])
    capsys.readouterr()
    answers = iter(["ollama", "auto_heberge", "http://localhost:11434", "llama3.2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "coding", "--model"]) == 0
    output = capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert "Agent model updated: coding" in output
    assert bundle.kernel.model.provider == "mock"
    assert bundle.agents.agents["coding"].model == {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "llama3.2",
        "base_url": "http://localhost:11434",
        "credential": None,
    }


def test_cli_onboard_default_agent_model_updates_kernel_default(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    capsys.readouterr()
    answers = iter(["ollama", "auto_heberge", "http://localhost:11434", "llama3.2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "main", "--model"]) == 0

    bundle = load_workspace_config(workspace)
    assert bundle.kernel.model.provider == "ollama"
    assert bundle.kernel.model.protocol == "ollama_chat"
    assert bundle.kernel.model.name == "llama3.2"
    assert bundle.agents.agents["main"].model is None


def test_cli_onboard_agent_model_can_pick_chatgpt_model_from_cache(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    cache = home / ".codex" / "models_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        """
{
  "models": [
    {"slug": "gpt-5.5", "display_name": "GPT-5.5", "description": "frontier", "priority": 0, "visibility": "list"},
    {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini", "description": "fast", "priority": 1, "visibility": "list"}
  ]
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    capsys.readouterr()
    answers = iter(["", "2", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "main", "--model"]) == 0
    output = capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert "Modele ChatGPT" in output
    assert bundle.kernel.model.provider == "auth"
    assert bundle.kernel.model.name == "gpt-5.4-mini"
    assert bundle.agents.agents["main"].model is None


def test_ollama_model_choices_use_api_tags(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"models":[{"name":"llama3.2:latest","size":2147483648,"details":{"family":"llama"}}]}'

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["authorization"] = req.headers.get("Authorization")
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("maurice.host.commands.onboard.urlrequest.urlopen", fake_urlopen)

    assert _ollama_model_choices("http://localhost:11434", api_key="secret") == [
        ("llama3.2:latest", "llama, 2.0 GB")
    ]
    assert seen["url"] == "http://localhost:11434/api/tags"
    assert seen["authorization"] == "Bearer secret"


def test_cli_onboard_agent_model_configures_ollama_cloud_credential(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    capsys.readouterr()

    def fake_choices(base_url: str, *, api_key: str = ""):
        assert base_url == "https://ollama.com"
        assert api_key == "cloud-secret"
        return [("minimax-m2.7:cloud", "cloud")]

    monkeypatch.setattr("maurice.host.commands.onboard._ollama_model_choices", fake_choices)
    answers = iter(["ollama", "cloud", "https://ollama.com", "ollama_cloud", "cloud-secret", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "main", "--model"]) == 0

    bundle = load_workspace_config(workspace)
    credentials = load_workspace_credentials(workspace)
    assert bundle.kernel.model.model_dump(mode="json") == {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "minimax-m2.7:cloud",
        "base_url": "https://ollama.com",
        "credential": "ollama_cloud",
    }
    assert bundle.agents.agents["main"].model is None
    assert bundle.agents.agents["main"].credentials == ["ollama_cloud"]
    assert credentials.credentials["ollama_cloud"].value == "cloud-secret"


def test_run_one_turn_uses_agent_model_name(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    main(["agents", "create", "coding", "--workspace", str(workspace)])
    update_agent(
        workspace,
        agent_id="coding",
        model={
            "provider": "ollama",
            "protocol": "ollama_chat",
            "name": "llama3.2",
            "base_url": "http://localhost:11434",
            "credential": None,
        },
    )
    capsys.readouterr()
    captured = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs) -> None:
            captured["model"] = kwargs["model"]
            captured["session_root"] = kwargs["session_store"].root
            captured["event_path"] = kwargs["event_store"].path
            captured["approvals_path"] = kwargs["approval_store"].path

        def run_turn(self, **_kwargs):
            return None

    monkeypatch.setattr("maurice.host.runtime.AgentLoop", FakeAgentLoop)

    run_one_turn(
        workspace_root=workspace,
        message="salut",
        session_id="default",
        agent_id="coding",
    )

    assert captured["model"] == "llama3.2"
    assert captured["session_root"] == workspace / "sessions"
    assert captured["event_path"] == workspace / "agents" / "coding" / "events.jsonl"
    assert captured["approvals_path"] == workspace / "agents" / "coding" / "approvals.json"


def test_run_one_turn_records_seen_active_project(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "outside-project"
    project.mkdir()
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    capsys.readouterr()

    class FakeAgentLoop:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_turn(self, **_kwargs):
            return None

    monkeypatch.setattr("maurice.host.runtime.AgentLoop", FakeAgentLoop)

    run_one_turn(
        workspace_root=workspace,
        message="salut",
        session_id="default",
        agent_id="main",
        source_metadata={"active_project_root": str(project)},
    )

    projects = list_known_projects(workspace / "agents" / "main")
    assert projects[0]["name"] == "outside-project"
    assert projects[0]["path"] == str(project.resolve())


def test_cli_interactive_onboard_configures_telegram_bot(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    answers = iter(
        [
            "limited",
            "ollama",
            "18792",
            "y",
            "123456:ABC",
            "111,222",
            "n",
            "auto_heberge",
            "http://localhost:11434",
            "llama3.2",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--interactive", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    bundle = load_workspace_config(workspace)
    telegram = bundle.host.channels["telegram"]
    assert telegram["adapter"] == "telegram"
    assert telegram["enabled"] is True
    assert telegram["agent"] == "main"
    assert telegram["credential"] == "telegram_bot"
    assert telegram["allowed_users"] == [111, 222]
    assert telegram["allowed_chats"] == []
    assert telegram["status"] == "configured_pending_adapter"
    assert bundle.agents.agents["main"].channels == ["telegram"]
    assert "telegram_bot" not in bundle.agents.agents["main"].credentials

    credentials = load_workspace_credentials(workspace)
    assert credentials.credentials["telegram_bot"].type == "token"
    assert credentials.credentials["telegram_bot"].value == "123456:ABC"
    assert credentials.credentials["telegram_bot"].provider == "telegram_bot"


def test_cli_onboard_agent_creates_durable_agent(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    capsys.readouterr()
    answers = iter(
        [
            "safe",
            "filesystem,memory",
            "",
            "telegram",
            "n",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "coding"]) == 0
    output = capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert "Agent onboarded: coding" in output
    assert bundle.agents.agents["coding"].skills == ["filesystem", "memory"]
    assert bundle.agents.agents["coding"].channels == ["telegram"]
    assert bundle.agents.agents["coding"].permission_profile == "safe"


def test_cli_gateway_telegram_poll_routes_allowed_message(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    host_path = host_config_path(workspace)
    host_data = read_yaml_file(host_path)
    host_data["host"]["channels"]["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
        "allowed_users": [111],
        "allowed_chats": [],
    }
    write_yaml_file(host_path, host_data)
    credentials_path = workspace / "credentials.yaml"
    credentials_path.write_text(
        """
credentials:
  telegram_bot:
    type: token
    value: test-token
    provider: telegram_bot
""",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr("maurice.host.telegram._telegram_bot_username", lambda _token: "test_bot")
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server._telegram_get_updates",
        lambda _token, offset=None, timeout_seconds=0: [
            {
                "update_id": 7,
                "message": {
                    "message_id": 1,
                    "from": {"id": 111},
                    "chat": {"id": 222},
                    "text": "salut",
                },
            }
        ],
    )
    # _telegram_send_chat_action is called inside telegram.py's _telegram_start_chat_action thread
    monkeypatch.setattr(
        "maurice.host.telegram._telegram_send_chat_action",
        lambda token, chat_id, action="typing": calls.append(("action", token, chat_id, action)),
    )
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server._telegram_send_message",
        lambda token, chat_id, text: calls.append(("message", token, chat_id, text)),
    )

    assert main(["gateway", "telegram-poll", "--workspace", str(workspace), "--once"]) == 0
    output = capsys.readouterr().out

    assert "Telegram poll complete: 1 message(s) routed." in output
    # chat action fires in a background thread — verify message was sent
    assert any(c[0] == "message" for c in calls)
    assert calls[-1][:3] == ("message", "test-token", 222)
    assert calls[-1][3].startswith("Mock response: salut")
    assert "context" in calls[-1][3]
    assert (workspace / "agents" / "main" / "telegram.offset").read_text(encoding="utf-8") == "8\n"


def test_cli_gateway_telegram_captures_pending_secret_before_model(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    host_path = host_config_path(workspace)
    host_data = read_yaml_file(host_path)
    host_data["host"]["channels"]["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
        "allowed_users": [111],
        "allowed_chats": [],
    }
    write_yaml_file(host_path, host_data)
    (workspace / "credentials.yaml").write_text(
        """
credentials:
  telegram_bot:
    type: token
    value: test-token
    provider: telegram_bot
""",
        encoding="utf-8",
    )
    request_secret_capture(
        workspace,
        agent_id="main",
        session_id="telegram:111",
        credential="telegram_coding",
        provider="telegram_bot",
        secret_type="token",
    )
    calls = []
    monkeypatch.setattr("maurice.host.telegram._telegram_bot_username", lambda _token: "test_bot")
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server._telegram_get_updates",
        lambda _token, offset=None, timeout_seconds=0: [
            {
                "update_id": 7,
                "message": {
                    "message_id": 1,
                    "from": {"id": 111},
                    "chat": {"id": 222},
                    "text": "123456:SECRET",
                },
            }
        ],
    )
    monkeypatch.setattr(
        "maurice.host.telegram._telegram_send_chat_action",
        lambda token, chat_id, action="typing": calls.append(("action", token, chat_id, action)),
    )
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server._telegram_send_message",
        lambda token, chat_id, text: calls.append(("message", token, chat_id, text)),
    )

    assert main(["gateway", "telegram-poll", "--workspace", str(workspace), "--once"]) == 0
    capsys.readouterr()

    credentials = load_workspace_credentials(workspace)
    assert credentials.credentials["telegram_coding"].value == "123456:SECRET"
    assert calls == [("message", "test-token", 222, "Secret enregistre sous `telegram_coding`. Tu peux continuer.")]
    events = (workspace / "agents" / "main" / "events.jsonl").read_text(encoding="utf-8")
    assert "123456:SECRET" not in events


def test_cli_start_reports_no_services_when_disabled(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["start", "--workspace", str(workspace), "--no-telegram", "--no-scheduler", "--no-gateway"]) == 0

    assert "No Maurice services to start." in capsys.readouterr().out
    assert not (workspace / "maurice.pid").exists()


def test_cli_start_daemon_spawns_foreground_command(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    started = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        started["kwargs"] = kwargs
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--workspace", str(workspace), "--no-telegram", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert "Maurice started in background with pid 12345." in output
    assert "--foreground" in started["command"]
    assert "--no-telegram" in started["command"]
    assert started["kwargs"]["start_new_session"] is True
    meta = json.loads((workspace / "run" / "server.meta").read_text(encoding="utf-8"))
    assert meta["scope"] == "global"
    assert meta["lifecycle"] == "daemon"
    assert meta["context_root"] == str(workspace.resolve())
    assert meta["pid"] == 12345


def test_cli_start_daemon_enables_telegram_by_default_when_configured(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    host_config = read_yaml_file(host_config_path(workspace))
    host_config.setdefault("host", {}).setdefault("channels", {})["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
    }
    write_yaml_file(host_config_path(workspace), host_config)
    capsys.readouterr()
    started = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--workspace", str(workspace), "--no-scheduler", "--no-gateway", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert "--foreground" in started["command"]
    assert "--no-telegram" not in started["command"]
    assert "Services: telegram." in output


def test_start_services_foreground_can_start_telegram_worker(tmp_path, capsys, monkeypatch) -> None:
    from maurice.host.commands import service
    from maurice.host.workspace import initialize_workspace

    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    host_config = read_yaml_file(host_config_path(workspace))
    host_config.setdefault("host", {}).setdefault("channels", {})["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
    }
    write_yaml_file(host_config_path(workspace), host_config)
    calls = []

    class FakeServer:
        def __init__(self, ctx):
            self.ctx = ctx
            self._running = True

        def serve(self, socket_path):
            calls.append(("server", socket_path.name))

    def fake_telegram_poll(workspace_root, agent_id, poll_seconds, stop_event, *, channel_name):
        calls.append(("telegram", channel_name))
        stop_event.set()

    monkeypatch.setattr(service, "MauriceServer", FakeServer)
    monkeypatch.setattr(service, "_wait_for_socket", lambda _socket_path: None)
    monkeypatch.setattr(service, "_telegram_poll_until_stopped", fake_telegram_poll)

    service._start_services(
        workspace,
        agent_id=None,
        poll_seconds=0.1,
        telegram=True,
        scheduler=False,
        gateway=False,
        open_browser=False,
    )
    output = capsys.readouterr().out

    assert ("telegram", "telegram") in calls
    assert "Services: telegram." in output


def test_cli_start_opens_gateway_ui_by_default(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    started = {}
    opened = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)
    def fake_wait_for_gateway_ui(url):
        opened.append(("wait", url))
        return True

    monkeypatch.setattr("maurice.host.commands.service._wait_for_gateway_ui", fake_wait_for_gateway_ui)
    monkeypatch.setattr("maurice.host.commands.service.webbrowser.open", lambda url: opened.append(("open", url)))

    assert main(["start", "--workspace", str(workspace), "--no-telegram"]) == 0
    output = capsys.readouterr().out

    assert "--foreground" in started["command"]
    assert "Opening Maurice chat: http://127.0.0.1:18791" in output
    assert opened == [
        ("wait", "http://127.0.0.1:18791"),
        ("open", "http://127.0.0.1:18791"),
    ]


def test_cli_start_does_not_open_dead_gateway_ui(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    opened = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(_command, **_kwargs):
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)
    monkeypatch.setattr("maurice.host.commands.service._wait_for_gateway_ui", lambda _url: False)
    monkeypatch.setattr("maurice.host.commands.service.webbrowser.open", lambda url: opened.append(url))

    assert main(["start", "--workspace", str(workspace), "--no-telegram"]) == 0
    output = capsys.readouterr().out

    assert "Maurice chat is not reachable yet: http://127.0.0.1:18791" in output
    assert opened == []


def test_cli_start_refuses_implicit_global_mode_when_configured_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    write_yaml_file(global_config_path(), {"usage": {"mode": "local"}})

    with pytest.raises(SystemExit) as exc:
        main(["start", "--no-telegram", "--no-browser"])

    assert "démarrer dans un dossier" in str(exc.value)
    assert "maurice setup" in str(exc.value)
    assert "choisis `global`" in str(exc.value)
    assert "`--workspace /chemin/du/workspace-global`" in str(exc.value)


def test_cli_start_uses_configured_global_workspace(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "configured-global"
    write_yaml_file(global_config_path(), {"usage": {"mode": "global", "workspace": str(workspace)}})
    started = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--no-telegram", "--no-browser"]) == 0
    capsys.readouterr()

    assert str(workspace.resolve()) in started["command"]


def test_cli_web_uses_explicit_local_dir(tmp_path, capsys, monkeypatch) -> None:
    opened = []
    captured = {}

    class FakeServer:
        address = ("127.0.0.1", 43210)

        def serve_forever(self):
            captured["served"] = True

        def shutdown(self):
            captured["shutdown"] = True

    def fake_build(ctx, **kwargs):
        captured["ctx"] = ctx
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr("maurice.host.cli._build_gateway_http_server_for_context", fake_build)
    monkeypatch.setattr("maurice.host.cli.webbrowser.open", lambda url: opened.append(url))

    assert main(["web", "--dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out

    assert captured["ctx"].scope == "local"
    assert captured["ctx"].content_root == tmp_path.resolve()
    assert captured["kwargs"]["port"] == 0
    assert captured["kwargs"]["web_token"]
    assert captured["served"] is True
    assert captured["shutdown"] is True
    assert opened == [f"http://127.0.0.1:43210?token={captured['kwargs']['web_token']}"]
    assert f"Maurice web chat (local) listening on {opened[0]}" in output
    assert f"Working directory: {tmp_path.resolve()}" in output


def test_cli_web_uses_configured_global_workspace(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "configured-global"
    project = tmp_path / "outside-project"
    project.mkdir()
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    write_yaml_file(global_config_path(), {"usage": {"mode": "global", "workspace": str(workspace)}})
    captured = {}

    class FakeServer:
        address = ("127.0.0.1", 43211)

        def serve_forever(self):
            captured["served"] = True

        def shutdown(self):
            captured["shutdown"] = True

    def fake_build(ctx, **kwargs):
        captured["ctx"] = ctx
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr("maurice.host.cli._build_gateway_http_server_for_context", fake_build)
    monkeypatch.setattr("maurice.host.cli.webbrowser.open", lambda _url: None)
    monkeypatch.chdir(project)

    assert main(["web", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert captured["ctx"].scope == "global"
    assert captured["ctx"].context_root == workspace.resolve()
    assert captured["ctx"].active_project_root == project.resolve()
    assert captured["kwargs"]["web_token"]
    assert captured["served"] is True
    assert captured["shutdown"] is True
    assert (
        "Maurice web chat (global) listening on "
        f"http://127.0.0.1:43211?token={captured['kwargs']['web_token']}"
    ) in output
    assert f"Working directory: {project.resolve()}" in output


def test_cli_web_does_not_start_telegram_polling_when_global_daemon_runs(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "configured-global"
    project = tmp_path / "outside-project"
    project.mkdir()
    main(["onboard", "--workspace", str(workspace)])
    host_data = read_yaml_file(host_config_path(workspace))
    host_data.setdefault("host", {}).setdefault("channels", {})["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
    }
    write_yaml_file(host_config_path(workspace), host_data)
    write_yaml_file(global_config_path(), {"usage": {"mode": "global", "workspace": str(workspace)}})
    capsys.readouterr()
    captured = {}

    class FakeClient:
        def __init__(self, ctx) -> None:
            captured["client_ctx"] = ctx

        def is_running(self) -> bool:
            return True

    class FakeServer:
        address = ("127.0.0.1", 43212)

        def serve_forever(self):
            captured["served"] = True

        def shutdown(self):
            captured["shutdown"] = True

    def fake_build(ctx, **kwargs):
        captured["ctx"] = ctx
        captured["kwargs"] = kwargs
        return FakeServer()

    def fail_pollers(*_args, **_kwargs):
        raise AssertionError("maurice web should not start Telegram polling while daemon is running")

    monkeypatch.setattr("maurice.host.cli.MauriceClient", FakeClient)
    monkeypatch.setattr("maurice.host.cli._WebTelegramPollers", fail_pollers)
    monkeypatch.setattr("maurice.host.cli._build_gateway_http_server_for_context", fake_build)
    monkeypatch.chdir(project)

    assert main(["web", "--no-browser"]) == 0

    assert captured["client_ctx"].scope == "global"
    assert captured["kwargs"]["telegram_pollers"] is None
    assert captured["ctx"].active_project_root == project.resolve()
    assert captured["served"] is True
    assert captured["shutdown"] is True


def test_cli_web_reports_occupied_port_without_traceback(tmp_path, monkeypatch) -> None:
    def fake_build(_ctx, **_kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr("maurice.host.cli._build_gateway_http_server_for_context", fake_build)

    with pytest.raises(SystemExit) as exc:
        main(["web", "--dir", str(tmp_path), "--port", "18791", "--no-browser"])

    assert "Le port 18791 est déjà utilisé" in str(exc.value)
    assert "maurice web --port 0" in str(exc.value)


def test_cli_start_initializes_missing_workspace(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    started = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        started["kwargs"] = kwargs
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--workspace", str(workspace), "--no-telegram", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert "Maurice workspace initialized:" in output
    assert "Maurice started in background with pid 12345." in output
    assert "--foreground" in started["command"]
    assert load_workspace_config(workspace).host.workspace_root == str(workspace.resolve())


def test_cli_start_initializes_empty_host_config(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    host_config_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    host_config_path(workspace).write_text("{}\n", encoding="utf-8")
    started = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started["command"] = command
        (workspace / "maurice.pid").write_text("12345\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--workspace", str(workspace), "--no-telegram", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert "Maurice workspace initialized:" in output
    assert "--foreground" in started["command"]
    assert load_workspace_config(workspace).host.workspace_root == str(workspace.resolve())


def test_cli_stop_reports_not_running(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["stop", "--workspace", str(workspace)]) == 0

    assert "Maurice is not running." in capsys.readouterr().out


def test_cli_restart_stops_then_starts_daemon(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    (workspace / "maurice.pid").write_text("111\n", encoding="utf-8")
    killed = []
    started = {}

    class FakeProcess:
        pid = 222

        def poll(self):
            return None

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    def fake_popen(command, **kwargs):
        started["command"] = command
        started["kwargs"] = kwargs
        (workspace / "maurice.pid").write_text("222\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("maurice.host.commands.service.os.kill", fake_kill)
    monkeypatch.setattr("maurice.host.commands.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.commands.service._pid_is_running", lambda pid: pid in {111, 222})
    monkeypatch.setattr("maurice.host.commands.service._wait_for_stop", lambda _path, _pid: (workspace / "maurice.pid").unlink())

    assert main(["restart", "--workspace", str(workspace), "--no-telegram", "--no-browser"]) == 0
    output = capsys.readouterr().out

    assert killed and killed[0][0] == 111
    assert "Stop requested for Maurice pid 111." in output
    assert "Maurice started in background with pid 222." in output
    assert "--no-telegram" in started["command"]
    assert started["kwargs"]["start_new_session"] is True
    assert json.loads((workspace / "run" / "server.meta").read_text(encoding="utf-8"))["pid"] == 222


def test_cli_logs_alias_reads_service_logs(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(["run", "--workspace", str(workspace), "--message", "bonjour"])
    capsys.readouterr()

    assert main(["logs", "--workspace", str(workspace), "--limit", "5"]) == 0

    assert "main default" in capsys.readouterr().out


def test_cli_dashboard_shows_runtime_summary(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["dashboard", "--workspace", str(workspace)]) == 0
    output = capsys.readouterr().out

    assert "MAURICE" in output
    assert "Agent" in output
    assert "* main" in output
    assert "Automatismes" in output
    assert "Sessions" in output
    assert "Capacites" in output


def test_dashboard_parser_supports_watch_mode() -> None:
    args = build_parser().parse_args(["dashboard", "--watch", "--plain", "--refresh-seconds", "0.5"])

    assert args.watch is True
    assert args.plain is True
    assert args.refresh_seconds == 0.5


def test_common_commands_have_default_workspace() -> None:
    parser = build_parser()

    assert parser.parse_args(["start"]).workspace is None
    assert parser.parse_args(["stop"]).workspace
    assert parser.parse_args(["logs"]).workspace
    assert parser.parse_args(["run", "--message", "bonjour"]).workspace


def test_cli_install_and_service_status_logs(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"

    assert main(["install"]) == 0
    install_output = capsys.readouterr().out
    assert "Maurice install: ok" in install_output
    assert "python: ok" in install_output

    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["install", "--workspace", str(workspace)]) == 0
    install_workspace_output = capsys.readouterr().out
    assert "workspace_dirs: ok" in install_workspace_output

    assert main(["service", "status", "--workspace", str(workspace)]) == 0
    status_output = capsys.readouterr().out
    assert "Maurice service status: ok" in status_output
    assert "default_agent: ok - main" in status_output
    assert "scheduler: ok - enabled" in status_output
    assert "gateway: ok - 127.0.0.1:18791" in status_output
    assert "daemon_context: warn - not running" in status_output

    assert main(["service", "logs", "--workspace", str(workspace)]) == 0
    assert "No service logs." in capsys.readouterr().out

    main(["run", "--workspace", str(workspace), "--message", "bonjour"])
    capsys.readouterr()

    assert main(["service", "logs", "--workspace", str(workspace), "--limit", "5"]) == 0
    logs_output = capsys.readouterr().out
    assert "main default" in logs_output


def test_cli_monitor_snapshot_and_events(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(["run", "--workspace", str(workspace), "--message", "bonjour"])
    capsys.readouterr()

    assert main(["monitor", "snapshot", "--workspace", str(workspace)]) == 0
    snapshot_output = capsys.readouterr().out
    assert "Runtime:" in snapshot_output
    assert "Agents: 1" in snapshot_output
    assert "Events:" in snapshot_output

    assert main(["monitor", "events", "--workspace", str(workspace), "--limit", "1"]) == 0
    events_output = capsys.readouterr().out
    assert "turn.completed" in events_output


def test_cli_migration_inspect_and_dry_run(tmp_path, capsys) -> None:
    jarvis = tmp_path / "jarvis"
    (jarvis / "skills" / "notes").mkdir(parents=True)
    (jarvis / "skills" / "notes" / "skill.yaml").write_text(
        """
name: notes
version: 0.1.0
origin: user
mutable: true
description: Notes.
config_namespace: skills.notes
requires:
  binaries: []
  credentials: []
dependencies:
  skills: []
  optional_skills: []
permissions: []
tools: []
backend: null
storage: null
dreams:
  attachment: dreams.md
events:
  state_publisher: null
""",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["migration", "inspect", "--jarvis", str(jarvis)]) == 0
    inspect_output = capsys.readouterr().out
    assert "Jarvis migration report" in inspect_output
    assert "skill: candidate" in inspect_output

    assert main(["migration", "run", "--jarvis", str(jarvis), "--workspace", str(workspace), "--dry-run"]) == 0
    run_output = capsys.readouterr().out
    assert "Mode: dry-run" in run_output
    assert not (workspace / "skills" / "notes").exists()


def test_cli_self_update_validate_and_apply(tmp_path, capsys) -> None:
    from maurice.kernel.permissions import PermissionContext
    from maurice.system_skills.self_update.tools import propose

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "hello.txt").write_text("old\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    bundle = load_workspace_config(workspace)
    host_config = host_config_path(workspace)
    host_config.write_text(
        host_config.read_text(encoding="utf-8").replace(bundle.host.runtime_root, str(runtime)),
        encoding="utf-8",
    )
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    result = propose(
        {
            "target_type": "host",
            "target_name": "hello",
            "runtime_path": "$runtime/hello.txt",
            "summary": "Update hello.",
            "patch": "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "risk": "low",
            "test_plan": "$ true",
            "mode": "proposal_only",
        },
        context,
    )
    proposal_id = result.data["proposal"]["id"]
    capsys.readouterr()

    assert main(["self-update", "list", "--workspace", str(workspace)]) == 0
    assert proposal_id in capsys.readouterr().out

    assert main(["self-update", "validate", proposal_id, "--workspace", str(workspace)]) == 0
    assert "Proposal validation: ok" in capsys.readouterr().out

    assert main(["self-update", "apply", proposal_id, "--workspace", str(workspace), "--confirm-approval"]) == 0
    assert "Proposal apply: applied" in capsys.readouterr().out
    assert (runtime / "hello.txt").read_text(encoding="utf-8") == "new\n"


def test_cli_run_rejects_unknown_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    with pytest.raises(SystemExit, match="Unknown agent"):
        main(
            [
                "run",
                "--workspace",
                str(workspace),
                "--agent",
                "missing",
                "--message",
                "bonjour",
            ]
        )


def test_cli_agents_create_list_update_and_run(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])

    assert (
        main(
            [
                "agents",
                "create",
                "coding",
                "--workspace",
                str(workspace),
                "--permission-profile",
                "safe",
                "--skill",
                "filesystem",
                "--skill",
                "memory",
                "--credential",
                "llm",
            ]
        )
        == 0
    )
    assert "Agent created: coding" in capsys.readouterr().out

    bundle = load_workspace_config(workspace)
    assert bundle.agents.agents["coding"].skills == ["filesystem", "memory"]
    assert bundle.agents.agents["coding"].credentials == ["llm"]
    assert bundle.agents.agents["coding"].permission_profile == "safe"
    assert (workspace / "agents" / "coding").is_dir()
    assert (workspace / "agents" / "coding" / "events.jsonl").is_file()

    assert main(["agents", "list", "--workspace", str(workspace)]) == 0
    list_output = capsys.readouterr().out
    assert "main default status=active profile=limited" in list_output
    assert "coding - status=active profile=safe" in list_output

    assert (
        main(
            [
                "agents",
                "update",
                "coding",
                "--workspace",
                str(workspace),
                "--default",
                "--credential",
                "openai",
                "--channel",
                "local",
            ]
        )
        == 0
    )
    assert "Agent updated: coding" in capsys.readouterr().out
    bundle = load_workspace_config(workspace)
    assert bundle.agents.agents["coding"].default is True
    assert bundle.agents.agents["main"].default is False
    assert bundle.agents.agents["coding"].channels == ["local"]
    assert bundle.agents.agents["coding"].credentials == ["openai"]

    assert main(["run", "--workspace", str(workspace), "--message", "salut"]) == 0
    assert "Mock response: salut" in capsys.readouterr().out
    assert (workspace / "sessions" / "coding" / "default.json").is_file()


def test_cli_agents_require_confirmation_for_permission_elevation(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "safe"])

    with pytest.raises(SystemExit, match="more permissive"):
        main(
            [
                "agents",
                "create",
                "ops",
                "--workspace",
                str(workspace),
                "--permission-profile",
                "limited",
            ]
        )

    assert (
        main(
            [
                "agents",
                "create",
                "ops",
                "--workspace",
                str(workspace),
                "--permission-profile",
                "limited",
                "--confirm-permission-elevation",
            ]
        )
        == 0
    )
    assert "Agent created: ops" in capsys.readouterr().out


def test_cli_agents_lifecycle_disable_archive_delete(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    main(["agents", "create", "coding", "--workspace", str(workspace)])
    main(["agents", "create", "ops", "--workspace", str(workspace)])

    assert main(["agents", "disable", "coding", "--workspace", str(workspace)]) == 0
    assert "Agent disabled: coding" in capsys.readouterr().out
    bundle = load_workspace_config(workspace)
    assert bundle.agents.agents["coding"].status == "disabled"

    with pytest.raises(SystemExit, match="not active"):
        main(
            [
                "run",
                "--workspace",
                str(workspace),
                "--agent",
                "coding",
                "--message",
                "bonjour",
            ]
        )

    assert main(["agents", "archive", "ops", "--workspace", str(workspace)]) == 0
    assert "Agent archived: ops" in capsys.readouterr().out
    bundle = load_workspace_config(workspace)
    assert bundle.agents.agents["ops"].status == "archived"

    with pytest.raises(SystemExit, match="destructive"):
        main(["agents", "delete", "ops", "--workspace", str(workspace)])

    assert main(["agents", "delete", "ops", "--workspace", str(workspace), "--confirm"]) == 0
    assert "Agent deleted: ops" in capsys.readouterr().out
    bundle = load_workspace_config(workspace)
    assert "ops" not in bundle.agents.agents
    assert not (workspace / "agents" / "ops").exists()


def test_cli_agents_cannot_remove_default_or_last_active_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    with pytest.raises(SystemExit, match="last active agent|default agent"):
        main(["agents", "disable", "main", "--workspace", str(workspace)])

    main(["agents", "create", "coding", "--workspace", str(workspace)])
    with pytest.raises(SystemExit, match="default agent"):
        main(["agents", "archive", "main", "--workspace", str(workspace)])


def test_cli_runs_lifecycle(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert (
        main(
            [
                "runs",
                "create",
                "--workspace",
                str(workspace),
                "--task",
                "Run tests.",
                "--write-path",
                "$workspace/runs/**",
                "--write-path",
                "maurice/kernel/**",
                "--permission-class",
                "fs.read",
                "--base-agent",
                "main",
                "--context-summary",
                "Maurice run lifecycle work.",
                "--relevant-file",
                "maurice/kernel/runs.py",
                "--constraint",
                "Keep scope tight.",
                "--plan-step",
                "Add mission packet.",
                "--requires-self-check",
                "--can-request-install",
                "--package-manager",
                "pip",
            ]
        )
        == 0
    )
    created_output = capsys.readouterr().out
    assert "Run created: run_" in created_output
    run_id = created_output.strip().split(": ")[1]

    assert main(["runs", "list", "--workspace", str(workspace)]) == 0
    list_output = capsys.readouterr().out
    assert run_id in list_output
    assert "created" in list_output
    mission = (workspace / "runs" / run_id / "mission.json").read_text(encoding="utf-8")
    assert "Maurice run lifecycle work." in mission
    assert "maurice/kernel/runs.py" in mission
    assert "requires_self_check" in mission
    assert '"base_agent": "main"' in mission
    assert '"permission_profile": "safe"' in mission

    assert main(["runs", "start", run_id, "--workspace", str(workspace)]) == 0
    assert f"Run started: {run_id}" in capsys.readouterr().out

    assert main(["runs", "execute", run_id, "--workspace", str(workspace)]) == 0
    assert f"Run autonomy stopped: {run_id}" in capsys.readouterr().out
    session = (workspace / "runs" / run_id / "session.json").read_text(encoding="utf-8")
    assert "Subagent mission packet loaded." in session
    assert (workspace / "runs" / run_id / "autonomy_report.json").is_file()

    assert (
        main(
            [
                "runs",
                "checkpoint",
                run_id,
                "--workspace",
                str(workspace),
                "--summary",
                "Half done.",
            ]
        )
        == 0
    )
    assert f"Run checkpointed: {run_id}" in capsys.readouterr().out
    assert (workspace / "runs" / run_id / "checkpoint.json").is_file()

    assert main(["runs", "resume", run_id, "--workspace", str(workspace)]) == 0
    assert f"Run resumed: {run_id}" in capsys.readouterr().out

    assert (
        main(
            [
                "runs",
                "checkpoint",
                run_id,
                "--workspace",
                str(workspace),
                "--summary",
                "Unsafe pause.",
                "--unsafe-to-resume",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="safe to resume"):
        main(["runs", "resume", run_id, "--workspace", str(workspace)])

    assert (
        main(
            [
                "runs",
                "complete",
                run_id,
                "--workspace",
                str(workspace),
                "--summary",
                "Done.",
                "--changed-file",
                "maurice/kernel/runs.py",
                "--verification-command",
                "pytest tests/test_runs.py",
                "--verification-status",
                "passed",
                "--verification-summary",
                "117 passed",
                "--risk",
                "Executor integration is pending.",
            ]
        )
        == 0
    )
    assert f"Run completed: {run_id}" in capsys.readouterr().out
    final = (workspace / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    assert "maurice/kernel/runs.py" in final
    assert "pytest tests/test_runs.py" in final
    assert "Executor integration is pending." in final

    assert (
        main(
            [
                "runs",
                "review",
                run_id,
                "--workspace",
                str(workspace),
                "--status",
                "accepted",
                "--summary",
                "Parent accepted the run.",
            ]
        )
        == 0
    )
    assert f"Run reviewed: {run_id} accepted" in capsys.readouterr().out
    review = (workspace / "runs" / run_id / "parent_review.json").read_text(encoding="utf-8")
    assert "Parent accepted the run." in review


def test_cli_runs_create_rejects_unknown_base_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    with pytest.raises(SystemExit, match="Unknown base agent"):
        main(
            [
                "runs",
                "create",
                "--workspace",
                str(workspace),
                "--task",
                "Run tests.",
                "--base-agent",
                "missing",
            ]
        )


def test_cli_runs_create_accepts_inline_profile(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert (
        main(
            [
                "runs",
                "create",
                "--workspace",
                str(workspace),
                "--task",
                "Inline task.",
                "--inline-profile",
                '{"id":"inline_coder","skills":["filesystem"],"permission_profile":"safe"}',
            ]
        )
        == 0
    )
    run_id = capsys.readouterr().out.strip().split(": ")[1]
    mission = (workspace / "runs" / run_id / "mission.json").read_text(encoding="utf-8")
    assert '"base_agent": "inline_coder"' in mission
    assert '"inline": true' in mission
    assert '"filesystem"' in mission


def test_cli_runs_create_rejects_inline_profile_with_base_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    with pytest.raises(SystemExit, match="cannot be combined"):
        main(
            [
                "runs",
                "create",
                "--workspace",
                str(workspace),
                "--task",
                "Inline task.",
                "--base-agent",
                "main",
                "--inline-profile",
                '{"id":"inline_coder"}',
            ]
        )


def test_cli_runs_complete_enforces_self_check(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(
        [
            "runs",
            "create",
            "--workspace",
            str(workspace),
            "--task",
            "Implement.",
            "--requires-self-check",
        ]
    )
    run_id = capsys.readouterr().out.strip().split(": ")[1]

    with pytest.raises(SystemExit, match="self-check"):
        main(
            [
                "runs",
                "complete",
                run_id,
                "--workspace",
                str(workspace),
                "--summary",
                "Done.",
            ]
        )


def test_cli_runs_coordination_flow(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(["runs", "create", "--workspace", str(workspace), "--task", "A"])
    run_a = capsys.readouterr().out.strip().split(": ")[1]
    main(["runs", "create", "--workspace", str(workspace), "--task", "B"])
    run_b = capsys.readouterr().out.strip().split(": ")[1]

    assert (
        main(
            [
                "runs",
                "coordinate",
                run_a,
                "--workspace",
                str(workspace),
                "--affects",
                run_b,
                "--impact",
                "Schema changed.",
                "--requested-action",
                "Update run B mission.",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Coordination requested: coord_" in output
    coordination_id = output.strip().split(": ")[1]

    assert main(["runs", "coordination-list", "--workspace", str(workspace)]) == 0
    list_output = capsys.readouterr().out
    assert coordination_id in list_output
    assert run_a in list_output
    assert run_b in list_output

    assert main(["runs", "coordination-ack", coordination_id, "--workspace", str(workspace)]) == 0
    assert "Coordination acknowledged" in capsys.readouterr().out

    assert main(["runs", "coordination-resolve", coordination_id, "--workspace", str(workspace)]) == 0
    assert "Coordination resolved" in capsys.readouterr().out


def test_cli_runs_approval_bridge_pauses_run_and_resolves(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(
        [
            "runs",
            "create",
            "--workspace",
            str(workspace),
            "--task",
            "Needs dependency",
            "--can-request-install",
            "--package-manager",
            "pip",
        ]
    )
    run_id = capsys.readouterr().out.strip().split(": ")[1]

    assert (
        main(
            [
                "runs",
                "request-approval",
                run_id,
                "--workspace",
                str(workspace),
                "--type",
                "dependency",
                "--reason",
                "Need pytest-httpserver.",
                "--scope",
                "package_manager=pip",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Run approval requested: runappr_" in output
    approval_id = output.strip().split(": ")[1]

    assert (workspace / "runs" / run_id / "checkpoint.json").is_file()
    assert main(["runs", "list", "--workspace", str(workspace), "--state", "paused"]) == 0
    assert run_id in capsys.readouterr().out

    assert main(["runs", "approvals-list", "--workspace", str(workspace)]) == 0
    approvals_output = capsys.readouterr().out
    assert approval_id in approvals_output
    assert "dependency" in approvals_output

    assert main(["runs", "approvals-approve", approval_id, "--workspace", str(workspace)]) == 0
    assert "Run approval approved" in capsys.readouterr().out


def test_cli_runs_dependency_approval_respects_policy(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()
    main(["runs", "create", "--workspace", str(workspace), "--task", "No installs"])
    run_id = capsys.readouterr().out.strip().split(": ")[1]

    with pytest.raises(SystemExit, match="does not allow"):
        main(
            [
                "runs",
                "request-approval",
                run_id,
                "--workspace",
                str(workspace),
                "--type",
                "dependency",
                "--reason",
                "Need pytest-httpserver.",
                "--scope",
                "package_manager=pip",
            ]
        )


def test_cli_auth_status_and_logout(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    assert main(["auth", "status", "chatgpt", "--workspace", str(workspace)]) == 0
    assert "not connected" in capsys.readouterr().out

    assert main(["auth", "logout", "chatgpt", "--workspace", str(workspace)]) == 0
    assert "no credential found" in capsys.readouterr().out


def test_cli_approvals_list_approve_and_deny(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    store = ApprovalStore(workspace / "agents" / "main" / "approvals.json")
    first = store.request(
        agent_id="main",
        session_id="sess_1",
        correlation_id="turn_1",
        tool_name="filesystem.write",
        permission_class="fs.write",
        scope={"paths": ["notes.md"]},
        arguments={"path": "notes.md", "content": "hello"},
        summary="Approve write",
        reason="test",
    )
    second = store.request(
        agent_id="main",
        session_id="sess_1",
        correlation_id="turn_2",
        tool_name="network.fetch",
        permission_class="network.outbound",
        scope={"hosts": ["example.com"]},
        arguments={"host": "example.com"},
        summary="Approve network",
        reason="test",
    )

    assert main(["approvals", "list", "--workspace", str(workspace)]) == 0
    list_output = capsys.readouterr().out
    assert first.id in list_output
    assert second.id in list_output

    assert main(["approvals", "approve", first.id, "--workspace", str(workspace)]) == 0
    assert "approved" in capsys.readouterr().out

    assert main(["approvals", "deny", second.id, "--workspace", str(workspace)]) == 0
    assert "denied" in capsys.readouterr().out

    assert main(["approvals", "list", "--status", "pending", "--workspace", str(workspace)]) == 0
    assert "No approvals." in capsys.readouterr().out


def test_cli_scheduler_runs_due_dream_job(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])

    assert (
        main(
            [
                "scheduler",
                "schedule-dream",
                "--workspace",
                str(workspace),
                "--skill",
                "memory",
            ]
        )
        == 0
    )
    schedule_output = capsys.readouterr().out
    assert "Scheduled dreaming.run" in schedule_output

    assert main(["scheduler", "run-once", "--workspace", str(workspace)]) == 0
    run_output = capsys.readouterr().out
    assert "dreaming.run" in run_output
    assert "completed" in run_output
    assert list((workspace / "content" / "dreams").glob("dream_*.json"))


def test_scheduler_handlers_use_global_context(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    captured = {}

    class FakeRegistry:
        def build_executor_map(self, skill_ctx):
            captured["skill_ctx"] = skill_ctx
            return {
                "dreaming.run": lambda _arguments: None,
            }

    class FakeSkillLoader:
        def __init__(self, roots, **kwargs):
            captured["roots"] = roots
            captured["kwargs"] = kwargs

        def load(self):
            return FakeRegistry()

    monkeypatch.setattr("maurice.host.commands.scheduler.SkillLoader", FakeSkillLoader)

    handlers = _scheduler_handlers(workspace, "main")

    assert "dreaming.run" in handlers
    assert captured["kwargs"]["agent_id"] == "main"
    assert captured["skill_ctx"].hooks.scope == "global"
    assert captured["skill_ctx"].hooks.lifecycle == "daemon"
    assert captured["skill_ctx"].hooks.memory_path == str(
        workspace / "skills" / "memory" / "memory.sqlite"
    )
    assert {Path(root.path) for root in captured["roots"]} == {
        Path(root.path) for root in captured["skill_ctx"].skill_roots
    }


def test_cli_scheduler_configure_updates_automation_times(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])

    assert (
        main(
            [
                "scheduler",
                "configure",
                "--workspace",
                str(workspace),
                "--dream-time",
                "8h45",
                "--daily-time",
                "10:15",
            ]
        )
        == 0
    )

    bundle = load_workspace_config(workspace)
    output = capsys.readouterr().out
    assert "dreaming on at 08:45" in output
    assert bundle.kernel.scheduler.dreaming_time == "08:45"
    assert bundle.kernel.scheduler.daily_time == "10:15"


def test_scheduler_defaults_create_dream_and_daily_jobs(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])

    _ensure_configured_scheduler_jobs(workspace, "main")

    jobs = JobStore(workspace / "agents" / "main" / "jobs.json").list(status=JobStatus.SCHEDULED)
    by_kind = {job.payload.get("kind"): job for job in jobs}
    assert by_kind["system.dreaming.daily"].name == "dreaming.run"
    assert by_kind["system.dreaming.daily"].payload["time"] == "09:00"
    assert by_kind["system.daily.digest"].name == "daily.digest"
    assert by_kind["system.daily.digest"].payload["time"] == "09:30"
    assert by_kind["system.daily.digest"].interval_seconds == 86400


def test_daily_digest_delivers_to_configured_telegram_chats(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"])
    host_path = host_config_path(workspace)
    host_data = read_yaml_file(host_path)
    host_data["host"]["channels"]["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
        "allowed_users": [123],
        "allowed_chats": [123, 456],
    }
    write_yaml_file(host_path, host_data)
    sent = []
    monkeypatch.setattr("maurice.host.delivery._credential_value", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr("maurice.host.delivery._telegram_send_message", lambda token, chat_id, text: sent.append((token, chat_id, text)))

    _deliver_daily_digest(
        workspace,
        {"agent_id": "main", "session_id": "daily"},
        "Bonjour, voici ton daily Maurice.",
    )

    assert [item[1] for item in sent] == [123, 456]
    session = SessionStore(workspace / "sessions").load("main", "daily")
    assert session.messages[-1].content == "Bonjour, voici ton daily Maurice."


def test_cli_gateway_local_message_routes_through_runtime(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])

    assert (
        main(
            [
                "gateway",
                "local-message",
                "--workspace",
                str(workspace),
                "--peer",
                "peer_1",
                "--message",
                "bonjour",
            ]
        )
        == 0
    )

    assert "Mock response: bonjour" in capsys.readouterr().out
    assert (workspace / "sessions" / "main" / "local:peer_1.json").is_file()
