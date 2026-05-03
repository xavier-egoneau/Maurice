from __future__ import annotations

from types import SimpleNamespace

from maurice.host.cli import _provider_for_config
from maurice.host.runtime import _effective_model_config
from maurice.host.credentials import CredentialRecord, CredentialsStore
from maurice.kernel.config import ConfigBundle
from maurice.kernel.providers import ApiProvider, FallbackProvider, MockProvider, OllamaCompatibleProvider, UnsupportedProvider


def _bundle(model: dict) -> ConfigBundle:
    return ConfigBundle.model_validate(
        {
            "host": {
                "runtime_root": "/runtime",
                "workspace_root": "/workspace",
                "skill_roots": [],
            },
            "kernel": {
                "models": {
                    "default": "test_model",
                    "entries": {"test_model": model},
                },
            },
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


def test_provider_config_uses_agent_model_chain_profile() -> None:
    bundle = ConfigBundle.model_validate(
        {
            "host": {
                "runtime_root": "/runtime",
                "workspace_root": "/workspace",
                "skill_roots": [],
            },
            "kernel": {
                "models": {
                    "default": "mock_kernel",
                    "entries": {
                        "mock_kernel": {"provider": "mock", "name": "kernel"},
                        "ollama_agent": {
                            "provider": "ollama",
                            "protocol": "ollama_chat",
                            "name": "agent-model",
                            "base_url": "http://localhost:11434",
                        },
                    },
                },
            },
            "agents": {"agents": {}},
            "skills": {"skills": {}},
        }
    )

    model = _effective_model_config(bundle, SimpleNamespace(model_chain=["ollama_agent"]))
    provider = _provider_for_config(
        bundle,
        "hello",
        agent=SimpleNamespace(model_chain=["ollama_agent"], credentials=[]),
    )

    assert model["name"] == "agent-model"
    assert isinstance(provider, OllamaCompatibleProvider)


def test_provider_config_falls_back_to_next_model_profile() -> None:
    bundle = ConfigBundle.model_validate(
        {
            "host": {
                "runtime_root": "/runtime",
                "workspace_root": "/workspace",
                "skill_roots": [],
            },
            "kernel": {
                "models": {
                    "default": "mock_kernel",
                    "entries": {
                        "chatgpt_primary": {
                            "provider": "auth",
                            "protocol": "chatgpt_codex",
                            "name": "gpt-5",
                            "credential": "chatgpt",
                        },
                        "ollama_fallback": {
                            "provider": "ollama",
                            "protocol": "ollama_chat",
                            "name": "gemma4",
                            "base_url": "http://localhost:11434",
                        },
                    },
                },
            },
            "agents": {"agents": {}},
            "skills": {"skills": {}},
        }
    )

    provider = _provider_for_config(
        bundle,
        "hello",
        agent=SimpleNamespace(model_chain=["chatgpt_primary", "ollama_fallback"], credentials=[]),
    )

    assert isinstance(provider, OllamaCompatibleProvider)


def test_fallback_provider_retries_next_model_before_output() -> None:
    first = MockProvider([
        {
            "type": "status",
            "status": "failed",
            "error": {"code": "api_down", "message": "primary unavailable"},
        }
    ])
    second = MockProvider([
        {"type": "text_delta", "delta": "ok"},
        {"type": "status", "status": "completed"},
    ])
    provider = FallbackProvider([(first, "primary"), (second, "fallback")])

    chunks = list(provider.stream(messages=[], model="ignored", tools=[], system="", limits={}))

    assert [chunk.delta for chunk in chunks if chunk.delta] == ["ok"]
    assert first.calls[0]["model"] == "primary"
    assert second.calls[0]["model"] == "fallback"
