from __future__ import annotations

import base64
import json

from maurice.kernel.contracts import ProviderChunkType, ProviderStatus, ToolDeclaration
from maurice.kernel.providers import (
    ChatGPTCodexProvider,
    _chatgpt_account_id_from_jwt,
    _tool_name_map,
    _to_chatgpt_input,
    _to_chatgpt_tools,
)


def test_chatgpt_provider_streams_text_usage_and_completed_status() -> None:
    captured = {}

    def transport(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        yield _sse({"type": "response.output_text.delta", "delta": "Salut "})
        yield _sse({"type": "response.output_text.delta", "delta": "humain"})
        yield _sse(
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                    "output": [],
                },
            }
        )

    provider = ChatGPTCodexProvider(
        token=_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}}),
        transport=transport,
    )

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Bonjour"}],
            model="gpt-test",
            tools=[],
            system="Kernel prompt",
        )
    )

    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["chatgpt-account-id"] == "acct_1"
    assert captured["payload"]["instructions"] == "Kernel prompt"
    assert [chunk.delta for chunk in chunks if chunk.type == ProviderChunkType.TEXT_DELTA] == [
        "Salut ",
        "humain",
    ]
    assert chunks[-2].usage.input_tokens == 3
    assert chunks[-2].usage.output_tokens == 5
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_chatgpt_provider_ignores_sse_event_lines() -> None:
    def transport(_url, _payload, _headers):
        yield "event: response.output_text.delta\n"
        yield _sse({"type": "response.output_text.delta", "delta": "Salut"})
        yield "event: response.completed\n"
        yield _sse({"type": "response.completed", "response": {"output": []}})

    provider = ChatGPTCodexProvider(token="token", transport=transport)

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Bonjour"}],
            model="gpt-test",
            tools=[],
            system="",
        )
    )

    assert chunks[0].delta == "Salut"
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_chatgpt_provider_normalizes_tool_calls() -> None:
    declaration = ToolDeclaration.model_validate(
        {
            "name": "filesystem.read",
            "owner_skill": "filesystem",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "permission": {"class": "fs.read", "scope": {}},
            "trust": {"input": "local_mutable", "output": "local_mutable"},
            "executor": "tools.read",
        }
    )

    def transport(_url, _payload, _headers):
        yield _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "filesystem_read",
                    "arguments": "{\"path\":\"notes.md\"}",
                },
            }
        )
        yield _sse({"type": "response.completed", "response": {"output": []}})

    provider = ChatGPTCodexProvider(token="token", transport=transport)

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Lis notes"}],
            model="gpt-test",
            tools=[declaration],
            system="",
        )
    )

    tool_chunk = next(chunk for chunk in chunks if chunk.type == ProviderChunkType.TOOL_CALL)
    assert tool_chunk.tool_call.id == "call_abc"
    assert tool_chunk.tool_call.name == "filesystem.read"
    assert tool_chunk.tool_call.arguments == {"path": "notes.md"}


def test_chatgpt_input_and_tool_conversion() -> None:
    declaration = ToolDeclaration.model_validate(
        {
            "name": "filesystem.read",
            "owner_skill": "filesystem",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "permission": {"class": "fs.read", "scope": {}},
            "trust": {"input": "local_mutable", "output": "local_mutable"},
            "executor": "tools.read",
        }
    )

    assert _to_chatgpt_input(
        [
            {"role": "user", "content": "Bonjour", "metadata": {}},
            {"role": "assistant", "content": "Salut", "metadata": {}},
            {
                "role": "tool",
                "content": "File read.",
                "metadata": {"tool_call_id": "call_1"},
            },
        ]
    ) == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Bonjour"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Salut"}],
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "File read.",
        },
    ]
    assert _to_chatgpt_tools([declaration], name_map=_tool_name_map([declaration])) == [
        {
            "type": "function",
            "name": "filesystem_read",
            "description": "Read a file.",
            "parameters": declaration.input_schema,
        }
    ]


def test_chatgpt_account_id_from_jwt_prefers_chatgpt_account_id() -> None:
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct_123",
                "user_id": "user_123",
            }
        }
    )

    assert _chatgpt_account_id_from_jwt(token) == "acct_123"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"
