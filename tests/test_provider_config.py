from __future__ import annotations

from types import SimpleNamespace

from maurice.host.cli import _provider_for_config
from maurice.host.credentials import CredentialRecord, CredentialsStore
from maurice.kernel.config import ConfigBundle
from maurice.kernel.providers import ApiProvider, OllamaCompatibleProvider, UnsupportedProvider


def _bundle(model: dict) -> ConfigBundle:
    return ConfigBundle.model_validate(
        {
            "host": {
                "runtime_root": "/runtime",
                "workspace_root": "/workspace",
                "skill_roots": [],
            },
            "kernel": {"model": model},
            "agents": {"agents": {}},
            "skills": {"skills": {}},
        }
    )


def test_api_provider_uses_protocol_url_and_credential() -> None:
    bundle = _bundle(
        {
            "provider": "api",
            "protocol": "openai_chat_completions",
            "name": "gpt-test",
            "base_url": "https://api.test/v1",
            "credential": "llm",
        }
    )
    credentials = CredentialsStore(
        credentials={
            "llm": CredentialRecord(type="api_key", value="secret"),
        }
    )

    provider = _provider_for_config(bundle, "hello", credentials)

    assert isinstance(provider, ApiProvider)
    assert provider.protocol == "openai_chat_completions"


def test_api_provider_requires_protocol() -> None:
    provider = _provider_for_config(_bundle({"provider": "api", "name": "x"}), "hello")

    assert isinstance(provider, UnsupportedProvider)
    chunk = next(
        provider.stream(messages=[], model="x", tools=[], system="", limits={})
    )
    assert chunk.error.code == "missing_protocol"


def test_auth_provider_requires_stored_login_credential() -> None:
    provider = _provider_for_config(
        _bundle(
            {
                "provider": "auth",
                "protocol": "chatgpt_codex",
                "name": "gpt-test",
                "credential": "chatgpt",
            }
        ),
        "hello",
    )

    assert isinstance(provider, UnsupportedProvider)
    chunk = next(
        provider.stream(messages=[], model="x", tools=[], system="", limits={})
    )
    assert chunk.error.code == "auth_missing"


def test_auth_provider_rejects_agent_without_credential_access() -> None:
    provider = _provider_for_config(
        _bundle(
            {
                "provider": "auth",
                "protocol": "chatgpt_codex",
                "name": "gpt-test",
                "credential": "chatgpt",
            }
        ),
        "hello",
        CredentialsStore(
            credentials={
                "chatgpt": CredentialRecord(type="token", value="secret"),
            }
        ),
        agent=SimpleNamespace(credentials=[]),
    )

    assert isinstance(provider, UnsupportedProvider)
    chunk = next(
        provider.stream(messages=[], model="x", tools=[], system="", limits={})
    )
    assert chunk.error.code == "credential_not_allowed"


def test_provider_config_uses_agent_model_override() -> None:
    provider = _provider_for_config(
        _bundle({"provider": "mock", "name": "kernel"}),
        "hello",
        agent=SimpleNamespace(
            model={
                "provider": "ollama",
                "protocol": "ollama_chat",
                "name": "agent-model",
                "base_url": "http://localhost:11434",
            }
        ),
    )

    assert isinstance(provider, OllamaCompatibleProvider)
