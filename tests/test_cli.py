from __future__ import annotations

from pathlib import Path

import pytest

from maurice.host.agents import update_agent
from maurice.kernel.approvals import ApprovalStore
from maurice.host.cli import build_parser, main
from maurice.host.cli import _ollama_model_choices, run_one_turn
from maurice.host.credentials import load_credentials
from maurice.host.secret_capture import request_secret_capture
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file


def test_cli_onboard_doctor_and_run(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"

    assert main(["onboard", "--workspace", str(workspace), "--permission-profile", "limited"]) == 0
    onboard_output = capsys.readouterr().out
    assert "workspace initialized" in onboard_output

    bundle = load_workspace_config(workspace)
    assert bundle.kernel.permissions.profile == "limited"
    assert bundle.agents.agents["main"].credentials == []

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
    assert bundle.agents.agents["main"].model["provider"] == "auth"
    assert bundle.agents.agents["main"].model["name"] == "gpt-5.4-mini"


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

    monkeypatch.setattr("maurice.host.cli.urlrequest.urlopen", fake_urlopen)

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

    monkeypatch.setattr("maurice.host.cli._ollama_model_choices", fake_choices)
    answers = iter(["ollama", "cloud", "https://ollama.com", "ollama_cloud", "cloud-secret", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["onboard", "--workspace", str(workspace), "--agent", "main", "--model"]) == 0

    bundle = load_workspace_config(workspace)
    credentials = load_credentials(workspace / "credentials.yaml")
    assert bundle.agents.agents["main"].model == {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "minimax-m2.7:cloud",
        "base_url": "https://ollama.com",
        "credential": "ollama_cloud",
    }
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

        def run_turn(self, **_kwargs):
            return None

    monkeypatch.setattr("maurice.host.cli.AgentLoop", FakeAgentLoop)

    run_one_turn(
        workspace_root=workspace,
        message="salut",
        session_id="default",
        agent_id="coding",
    )

    assert captured["model"] == "llama3.2"


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

    credentials = load_credentials(workspace / "credentials.yaml")
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
    host_path = workspace / "config" / "host.yaml"
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
    monkeypatch.setattr("maurice.host.cli._telegram_bot_username", lambda _token: "test_bot")
    monkeypatch.setattr(
        "maurice.host.cli._telegram_get_updates",
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
    monkeypatch.setattr(
        "maurice.host.cli._telegram_send_chat_action",
        lambda token, chat_id, action="typing": calls.append(("action", token, chat_id, action)),
    )
    monkeypatch.setattr(
        "maurice.host.cli._telegram_send_message",
        lambda token, chat_id, text: calls.append(("message", token, chat_id, text)),
    )

    assert main(["gateway", "telegram-poll", "--workspace", str(workspace), "--once"]) == 0
    output = capsys.readouterr().out

    assert "Telegram poll complete: 1 message(s) routed." in output
    assert calls[0] == ("action", "test-token", 222, "typing")
    assert calls[-1] == ("message", "test-token", 222, "Mock response: salut")
    assert (workspace / "agents" / "main" / "telegram.offset").read_text(encoding="utf-8") == "8\n"


def test_cli_gateway_telegram_captures_pending_secret_before_model(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    host_path = workspace / "config" / "host.yaml"
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
    monkeypatch.setattr("maurice.host.cli._telegram_bot_username", lambda _token: "test_bot")
    monkeypatch.setattr(
        "maurice.host.cli._telegram_get_updates",
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
        "maurice.host.cli._telegram_send_chat_action",
        lambda token, chat_id, action="typing": calls.append(("action", token, chat_id, action)),
    )
    monkeypatch.setattr(
        "maurice.host.cli._telegram_send_message",
        lambda token, chat_id, text: calls.append(("message", token, chat_id, text)),
    )

    assert main(["gateway", "telegram-poll", "--workspace", str(workspace), "--once"]) == 0
    capsys.readouterr()

    credentials = load_credentials(workspace / "credentials.yaml")
    assert credentials.credentials["telegram_coding"].value == "123456:SECRET"
    assert calls == [("message", "test-token", 222, "Secret enregistre sous `telegram_coding`. Tu peux continuer.")]
    events = (workspace / "agents" / "main" / "events.jsonl").read_text(encoding="utf-8")
    assert "123456:SECRET" not in events


def test_cli_start_reports_no_services_when_disabled(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["start", "--workspace", str(workspace), "--no-telegram", "--no-scheduler"]) == 0

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

    monkeypatch.setattr("maurice.host.cli.subprocess.Popen", fake_popen)
    monkeypatch.setattr("maurice.host.cli._pid_is_running", lambda pid: pid == 12345)

    assert main(["start", "--workspace", str(workspace), "--no-telegram"]) == 0
    output = capsys.readouterr().out

    assert "Maurice started in background with pid 12345." in output
    assert "--foreground" in started["command"]
    assert "--no-telegram" in started["command"]
    assert started["kwargs"]["start_new_session"] is True


def test_cli_stop_reports_not_running(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    main(["onboard", "--workspace", str(workspace)])
    capsys.readouterr()

    assert main(["stop", "--workspace", str(workspace)]) == 0

    assert "Maurice is not running." in capsys.readouterr().out


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

    assert parser.parse_args(["start"]).workspace
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
    host_config = workspace / "config" / "host.yaml"
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
    assert list((workspace / "artifacts" / "dreams").glob("dream_*.json"))


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
