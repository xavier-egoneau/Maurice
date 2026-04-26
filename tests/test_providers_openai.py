from __future__ import annotations

import json
from urllib.error import HTTPError

from maurice.kernel.contracts import ProviderChunkType, ProviderStatus, ToolDeclaration
from maurice.kernel.providers import (
    OpenAICompatibleProvider,
    _safe_error_message,
    _to_openai_messages,
    _to_openai_tools,
)


def test_openai_provider_streams_text_usage_and_completed_status() -> None:
    captured = {}

    def transport(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        yield _sse({"choices": [{"delta": {"content": "Salut "}}]})
        yield _sse({"choices": [{"delta": {"content": "humain"}}]})
        yield _sse({"usage": {"prompt_tokens": 3, "completion_tokens": 5}, "choices": []})
        yield "data: [DONE]\n\n"

    provider = OpenAICompatibleProvider(
        api_key="secret",
        base_url="https://api.test/v1/",
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

    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert [chunk.delta for chunk in chunks if chunk.type == ProviderChunkType.TEXT_DELTA] == [
        "Salut ",
        "humain",
    ]
    assert chunks[-2].usage.input_tokens == 3
    assert chunks[-2].usage.output_tokens == 5
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_openai_provider_normalizes_streamed_tool_call_fragments() -> None:
    def transport(_url, _payload, _headers):
        yield _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "function": {
                                        "name": "filesystem.read",
                                        "arguments": "{\"path\":",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        yield _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "\"notes.md\"}"},
                                }
                            ]
                        }
                    }
                ]
            }
        )
        yield "data: [DONE]\n\n"

    provider = OpenAICompatibleProvider(api_key="secret", transport=transport)

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Lis notes"}],
            model="gpt-test",
            tools=[],
            system="",
        )
    )

    tool_chunk = next(chunk for chunk in chunks if chunk.type == ProviderChunkType.TOOL_CALL)
    assert tool_chunk.tool_call.id == "call_abc"
    assert tool_chunk.tool_call.name == "filesystem.read"
    assert tool_chunk.tool_call.arguments == {"path": "notes.md"}
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_openai_provider_reports_missing_api_key() -> None:
    provider = OpenAICompatibleProvider(api_key="", transport=lambda *_args: [])

    chunks = list(
        provider.stream(
            messages=[],
            model="gpt-test",
            tools=[],
            system="",
        )
    )

    assert len(chunks) == 1
    assert chunks[0].status == ProviderStatus.FAILED
    assert chunks[0].error.code == "auth_missing"


def test_safe_error_message_includes_http_error_body() -> None:
    exc = HTTPError(
        "https://api.test",
        400,
        "Bad Request",
        hdrs={},
        fp=_BytesReader(b'{"error":{"message":"bad model"}}'),
    )

    assert "HTTP 400: Bad Request" in _safe_error_message(exc)
    assert "bad model" in _safe_error_message(exc)


def test_openai_provider_reports_invalid_json_tool_arguments() -> None:
    def transport(_url, _payload, _headers):
        yield _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "function": {
                                        "name": "filesystem.read",
                                        "arguments": "{not json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        yield "data: [DONE]\n\n"

    provider = OpenAICompatibleProvider(api_key="secret", transport=transport)

    chunks = list(
        provider.stream(
            messages=[],
            model="gpt-test",
            tools=[],
            system="",
        )
    )

    assert chunks[-1].status == ProviderStatus.FAILED
    assert chunks[-1].error.code == "invalid_tool_arguments"


def test_openai_message_and_tool_conversion() -> None:
    declaration = ToolDeclaration.model_validate(
        {
            "name": "filesystem.read",
            "owner_skill": "filesystem",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            "permission": {"class": "fs.read", "scope": {}},
            "trust": {"input": "local_mutable", "output": "local_mutable"},
            "executor": "tools.read",
        }
    )

    assert _to_openai_messages(
        [
            {"role": "user", "content": "Bonjour", "metadata": {}},
            {
                "role": "tool",
                "content": "File read.",
                "metadata": {"tool_call_id": "call_1"},
            },
        ],
        "System",
    ) == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Bonjour"},
        {"role": "tool", "tool_call_id": "call_1", "content": "File read."},
    ]
    assert _to_openai_tools([declaration]) == [
        {
            "type": "function",
            "function": {
                "name": "filesystem.read",
                "description": "Read a file.",
                "parameters": declaration.input_schema,
            },
        }
    ]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit: int = -1) -> bytes:
        return self.data

    def close(self) -> None:
        pass
