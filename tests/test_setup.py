from __future__ import annotations

import yaml

from maurice.host.project import global_config_path
from maurice.host.setup import _provider_choices, _test_provider_connection, run_setup
from maurice.kernel.config import default_model_config, load_workspace_config


def test_run_setup_writes_usage_mode_and_tests_provider(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    monkeypatch.setenv("MAURICE_SETUP_SHOW_MOCK", "1")
    answers = iter(["local", "mock", "limited"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert config["provider"]["type"] == "mock"
    assert config["permission_profile"] == "limited"
    assert config["usage"]["mode"] == "local"
    assert "Provider vérifié" in capsys.readouterr().out


def test_setup_hides_mock_provider_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MAURICE_SETUP_SHOW_MOCK", raising=False)

    choices = [key for key, _label in _provider_choices()]

    assert "mock" not in choices
    assert "openai_api" in choices
    assert "openai_auth" in choices
    assert "ollama_local" in choices
    assert "ollama_api" in choices


def test_run_setup_accepts_numbered_choices(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    answers = iter(["1", "5", "claude-test", "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "secret")
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert config["usage"]["mode"] == "local"
    assert config["provider"]["protocol"] == "anthropic"
    assert config["provider"]["model"] == "claude-test"
    assert config["permission_profile"] == "power"


def test_run_setup_reports_provider_test_failure(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    monkeypatch.setenv("MAURICE_SETUP_SHOW_MOCK", "1")
    workspace = tmp_path / "global-workspace"
    answers = iter(["global", str(workspace), "mock", "safe"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.setup._test_provider_connection",
        lambda: (False, "bad credentials"),
    )

    assert run_setup() is False

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert config["usage"]["mode"] == "global"
    assert "bad credentials" in capsys.readouterr().err
    assert not workspace.exists()


def test_run_setup_global_initializes_workspace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    monkeypatch.setenv("MAURICE_SETUP_SHOW_MOCK", "1")
    workspace = tmp_path / "global-workspace"
    answers = iter(["global", str(workspace), "mock", "power"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    bundle = load_workspace_config(workspace)
    assert config["usage"]["mode"] == "global"
    assert config["usage"]["workspace"] == str(workspace.resolve())
    assert bundle.host.workspace_root == str(workspace.resolve())
    assert bundle.kernel.permissions.profile == "power"
    assert (workspace / "agents" / "main").is_dir()
    assert "Workspace assistant initialisé" in capsys.readouterr().out


def test_run_setup_chatgpt_uses_model_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    answers = iter(["local", "chatgpt", "2", "limited"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.setup.chatgpt_model_choices",
        lambda: [
            ("gpt-5", "GPT-5"),
            ("gpt-5.4-mini", "GPT-5.4 Mini"),
        ],
    )
    monkeypatch.setattr(
        "maurice.host.auth.ChatGPTAuthFlow.run",
        lambda _self: {"access_token": "token", "expires": 9999999999},
    )
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert config["provider"]["type"] == "auth"
    assert config["provider"]["model"] == "gpt-5.4-mini"


def test_run_setup_chatgpt_authenticates_before_model_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    answers = iter(["local", "chatgpt", "1", "limited"])
    authenticated = {"done": False}
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.auth.ChatGPTAuthFlow.run",
        lambda _self: authenticated.update(done=True) or {"access_token": "token", "expires": 9999999999},
    )

    def fake_model_choices():
        assert authenticated["done"] is True
        return [("gpt-5", "GPT-5")]

    monkeypatch.setattr("maurice.host.setup.chatgpt_model_choices", fake_model_choices)
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True


def test_run_setup_global_reuses_existing_chatgpt_auth(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "global-workspace"
    global_config_path().parent.mkdir(parents=True)
    global_config_path().write_text(
        "provider:\n"
        "  type: auth\n"
        "  protocol: chatgpt_codex\n"
        "  model: gpt-5\n"
        "  credential: chatgpt\n"
        "permission_profile: limited\n"
        "usage:\n"
        "  mode: local\n",
        encoding="utf-8",
    )
    credentials_path = tmp_path / ".maurice" / "credentials.yaml"
    credentials_path.write_text(
        "credentials:\n"
        "  chatgpt:\n"
        "    type: token\n"
        "    value: existing-token\n"
        "    refresh_token: existing-refresh\n"
        "    provider: chatgpt_codex\n",
        encoding="utf-8",
    )
    answers = iter(["global", str(workspace), "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.auth.ChatGPTAuthFlow.run",
        lambda _self: (_ for _ in ()).throw(AssertionError("auth flow should not run")),
    )
    monkeypatch.setattr("maurice.host.setup.chatgpt_model_choices", lambda: [("gpt-5", "GPT-5")])
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    bundle = load_workspace_config(workspace)
    credentials = yaml.safe_load(credentials_path.read_text(encoding="utf-8"))
    assert config["usage"]["mode"] == "global"
    assert config["provider"]["credential"] == "chatgpt"
    assert credentials["credentials"]["chatgpt"]["value"] == "existing-token"
    model = default_model_config(bundle)
    assert model["provider"] == "auth"
    assert model["credential"] == "chatgpt"
    assert "chatgpt" in bundle.agents.agents["main"].credentials
    assert "Auth ChatGPT existante conservée" in capsys.readouterr().out


def test_run_setup_chatgpt_can_redo_existing_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    credentials_path = tmp_path / ".maurice" / "credentials.yaml"
    credentials_path.parent.mkdir(parents=True)
    credentials_path.write_text(
        "credentials:\n"
        "  chatgpt:\n"
        "    type: token\n"
        "    value: stale-token\n"
        "    refresh_token: stale-refresh\n"
        "    provider: chatgpt_codex\n",
        encoding="utf-8",
    )
    answers = iter(["local", "chatgpt", "2", "1", "limited"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.auth.ChatGPTAuthFlow.run",
        lambda _self: {
            "access_token": "fresh-token",
            "refresh_token": "fresh-refresh",
            "expires": 9999999999,
        },
    )
    monkeypatch.setattr("maurice.host.setup.chatgpt_model_choices", lambda: [("gpt-5", "GPT-5")])
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    credentials = yaml.safe_load(credentials_path.read_text(encoding="utf-8"))
    assert credentials["credentials"]["chatgpt"]["value"] == "fresh-token"
    assert credentials["credentials"]["chatgpt"]["refresh_token"] == "fresh-refresh"


def test_setup_provider_check_sends_instructions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    (tmp_path / ".maurice").mkdir()
    global_config_path().write_text(
        "provider:\n  type: mock\n",
        encoding="utf-8",
    )
    captured = {}

    class FakeProvider:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return [{"type": "status", "status": "completed"}]

    monkeypatch.setattr("maurice.host.server._build_provider", lambda _cfg, _message: FakeProvider())

    assert _test_provider_connection() == (True, "")
    assert captured["system"]
    assert "setup connectivity check" in captured["system"]


def test_run_setup_ollama_uses_model_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    answers = iter(["local", "ollama", "", "2", "limited"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "maurice.host.setup.ollama_model_choices",
        lambda _base_url: [
            ("llama3.1", "Llama 3.1"),
            ("qwen2.5-coder", "Qwen Coder"),
        ],
    )
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    assert config["provider"]["type"] == "ollama"
    assert config["provider"]["model"] == "qwen2.5-coder"


def test_run_setup_ollama_api_writes_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    answers = iter(["local", "ollama_api", "https://ollama.com", "2", "limited"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "ollama-secret")
    monkeypatch.setattr(
        "maurice.host.setup.ollama_model_choices",
        lambda _base_url, api_key="": [
            ("llama3.1", "Llama 3.1"),
            ("gpt-oss:20b", "GPT OSS"),
        ],
    )
    monkeypatch.setattr("maurice.host.setup._test_provider_connection", lambda: (True, ""))

    assert run_setup() is True

    config = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))
    credentials = yaml.safe_load((tmp_path / ".maurice" / "credentials.yaml").read_text(encoding="utf-8"))
    assert config["provider"]["type"] == "ollama"
    assert config["provider"]["protocol"] == "ollama_chat"
    assert config["provider"]["base_url"] == "https://ollama.com"
    assert config["provider"]["credential"] == "ollama"
    assert config["provider"]["model"] == "gpt-oss:20b"
    assert credentials["credentials"]["ollama"]["value"] == "ollama-secret"
