from __future__ import annotations

import json

from maurice.kernel.contracts import ProviderChunkType, ProviderStatus, ToolDeclaration
from maurice.kernel.providers import (
    OllamaCompatibleProvider,
    _to_ollama_messages,
    _to_ollama_tools,
)


def test_ollama_provider_streams_text_usage_and_completed_status() -> None:
    captured = {}

    def transport(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        yield json.dumps({"message": {"content": "Salut "}}).encode()
        yield json.dumps({"message": {"content": "humain"}}).encode()
        yield json.dumps({"done": True, "prompt_eval_count": 3, "eval_count": 5}).encode()

    provider = OllamaCompatibleProvider(
        base_url="http://ollama.local/",
        api_key="secret",
        transport=transport,
    )

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Bonjour"}],
            model="llama3.2",
            tools=[],
            system="Kernel prompt",
        )
    )

    assert captured["url"] == "http://ollama.local/api/chat"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["model"] == "llama3.2"
    assert [chunk.delta for chunk in chunks if chunk.type == ProviderChunkType.TEXT_DELTA] == [
        "Salut ",
        "humain",
    ]
    assert chunks[-2].usage.input_tokens == 3
    assert chunks[-2].usage.output_tokens == 5
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_ollama_provider_normalizes_tool_calls() -> None:
    def transport(_url, _payload, _headers):
        yield json.dumps(
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "filesystem.read",
                                "arguments": {"path": "notes.md"},
                            }
                        }
                    ]
                }
            }
        )
        yield json.dumps({"done": True})

    provider = OllamaCompatibleProvider(transport=transport)

    chunks = list(
        provider.stream(
            messages=[{"role": "user", "content": "Lis notes"}],
            model="llama3.2",
            tools=[],
            system="",
        )
    )

    tool_chunk = next(chunk for chunk in chunks if chunk.type == ProviderChunkType.TOOL_CALL)
    assert tool_chunk.tool_call.id == "call_0"
    assert tool_chunk.tool_call.name == "filesystem.read"
    assert tool_chunk.tool_call.arguments == {"path": "notes.md"}
    assert chunks[-1].status == ProviderStatus.COMPLETED


def test_ollama_provider_reports_invalid_json_tool_arguments() -> None:
    def transport(_url, _payload, _headers):
        yield json.dumps(
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "filesystem.read",
                                "arguments": "{not json",
                            }
                        }
                    ]
                }
            }
        )

    provider = OllamaCompatibleProvider(transport=transport)

    chunks = list(
        provider.stream(
            messages=[],
            model="llama3.2",
            tools=[],
            system="",
        )
    )

    assert len(chunks) == 1
    assert chunks[0].status == ProviderStatus.FAILED
    assert chunks[0].error.code == "invalid_tool_arguments"


def test_ollama_message_and_tool_conversion() -> None:
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

    assert _to_ollama_messages(
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
        {"role": "tool", "content": "File read.", "tool_call_id": "call_1"},
    ]
    assert _to_ollama_tools([declaration]) == [
        {
            "type": "function",
            "function": {
                "name": "filesystem.read",
                "description": "Read a file.",
                "parameters": declaration.input_schema,
            },
        }
    ]
