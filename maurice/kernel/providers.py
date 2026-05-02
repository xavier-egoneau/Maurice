"""Provider interfaces and test providers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import json
import os
from typing import Any, Protocol
from urllib import error
from urllib import request

from maurice.kernel.contracts import ProviderChunk, ProviderStatus, ToolDeclaration


class Provider(Protocol):
    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        pass


class MockProvider:
    """Deterministic provider for tests and early loop wiring."""

    def __init__(self, chunks: Sequence[ProviderChunk | dict[str, Any]]) -> None:
        self.chunks = [ProviderChunk.model_validate(chunk) for chunk in chunks]
        self.calls: list[dict[str, Any]] = []

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": list(tools),
                "system": system,
                "limits": limits or {},
            }
        )
        yield from self.chunks


class ApiProvider:
    """Generic URL/key provider selected by protocol."""

    def __init__(
        self,
        *,
        protocol: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 120,
        transport: Any = None,
    ) -> None:
        self.protocol = protocol
        if protocol == "openai_chat_completions":
            self._provider: Provider = OpenAICompatibleProvider(
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
        elif protocol == "ollama_chat":
            self._provider = OllamaCompatibleProvider(
                base_url=base_url or "http://localhost:11434",
                api_key=api_key or "",
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
        else:
            self._provider = UnsupportedProvider(
                code="unsupported_protocol",
                message=f"Unsupported API provider protocol: {protocol}",
            )

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        yield from self._provider.stream(
            messages=messages,
            model=model,
            tools=tools,
            system=system,
            limits=limits,
        )


class UnsupportedProvider:
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        yield ProviderChunk(
            type="status",
            status=ProviderStatus.FAILED,
            error={
                "code": self.code,
                "message": self.message,
                "retryable": False,
            },
        )


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int = 120,
        transport: Any = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._urlopen_transport

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        if not self.api_key:
            yield ProviderChunk(
                type="status",
                status=ProviderStatus.FAILED,
                error={
                    "code": "auth_missing",
                    "message": "OpenAI-compatible provider requires an API key.",
                    "retryable": False,
                },
            )
            return

        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _to_openai_tools(tools)
            payload["tool_choice"] = "auto"
        if limits and limits.get("max_tokens"):
            payload["max_tokens"] = limits["max_tokens"]

        pending_tool_calls: dict[int, dict[str, str]] = {}
        stream_done = {"seen": False}
        try:
            for event in _iter_sse_json(
                self._transport(f"{self.base_url}/chat/completions", payload, self._headers()),
                done_flag=stream_done,
            ):
                usage = event.get("usage")
                if usage:
                    yield ProviderChunk(
                        type="usage",
                        usage={
                            "input_tokens": int(usage.get("prompt_tokens") or 0),
                            "output_tokens": int(usage.get("completion_tokens") or 0),
                        },
                    )
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        yield ProviderChunk(type="text_delta", delta=content)
                    for tool_call in delta.get("tool_calls") or []:
                        index = int(tool_call.get("index") or 0)
                        function = tool_call.get("function") or {}
                        acc = pending_tool_calls.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tool_call.get("id"):
                            acc["id"] += tool_call["id"]
                        if function.get("name"):
                            acc["name"] += function["name"]
                        if function.get("arguments"):
                            acc["arguments"] += function["arguments"]

            if not stream_done["seen"]:
                yield _failed_status(
                    "incomplete_stream",
                    "Provider stream ended before the completion marker.",
                    retryable=True,
                )
                return
            for index in sorted(pending_tool_calls):
                call = pending_tool_calls[index]
                if not call["name"]:
                    yield ProviderChunk(
                        type="status",
                        status=ProviderStatus.FAILED,
                        error={
                            "code": "invalid_tool_call",
                            "message": "OpenAI-compatible provider returned a tool call without a function name.",
                            "retryable": False,
                        },
                    )
                    return
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    yield ProviderChunk(
                        type="status",
                        status=ProviderStatus.FAILED,
                        error={
                            "code": "invalid_tool_arguments",
                            "message": "OpenAI-compatible provider returned tool arguments that are not valid JSON.",
                            "retryable": False,
                        },
                    )
                    return
                yield ProviderChunk(
                    type="tool_call",
                    tool_call={
                        "id": call["id"] or f"call_{index}",
                        "name": call["name"],
                        "arguments": arguments,
                    },
                )
            yield ProviderChunk(type="status", status=ProviderStatus.COMPLETED)
        except Exception as exc:
            yield ProviderChunk(
                type="status",
                status=ProviderStatus.FAILED,
                error={
                    "code": "provider_error",
                    "message": _safe_error_message(exc),
                    "retryable": True,
                },
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _urlopen_transport(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterable[bytes]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            yield from response


class ChatGPTCodexProvider:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://chatgpt.com/backend-api/codex",
        timeout_seconds: int = 120,
        transport: Any = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._urlopen_transport

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        if not self.token:
            yield ProviderChunk(
                type="status",
                status=ProviderStatus.FAILED,
                error={
                    "code": "auth_missing",
                    "message": "ChatGPT auth provider requires a stored access token.",
                    "retryable": False,
                },
            )
            return

        payload: dict[str, Any] = {
            "model": model,
            "input": _to_chatgpt_input(messages),
            "instructions": system or "You are Maurice.",
            "stream": True,
            "store": False,
        }
        tool_name_map = _tool_name_map(tools)
        if tools:
            payload["tools"] = _to_chatgpt_tools(tools, name_map=tool_name_map)

        streamed_text = False
        pending_tool_calls: list[dict[str, Any]] = []
        completed = False
        try:
            for event in _iter_sse_json(
                self._transport(f"{self.base_url}/responses", payload, self._headers())
            ):
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta") or ""
                    if delta:
                        streamed_text = True
                        yield ProviderChunk(type="text_delta", delta=delta)
                    continue
                if event_type == "response.output_item.done":
                    call = _normalize_chatgpt_response_tool_call(
                        event.get("item") or {},
                        name_map=tool_name_map,
                    )
                    if call is not None:
                        pending_tool_calls.append(call)
                    continue
                if event_type == "response.completed":
                    completed = True
                    response = event.get("response") or {}
                    usage = response.get("usage") or {}
                    if usage:
                        yield ProviderChunk(
                            type="usage",
                            usage={
                                "input_tokens": int(usage.get("input_tokens") or 0),
                                "output_tokens": int(usage.get("output_tokens") or 0),
                            },
                        )
                    for call in _chatgpt_tool_calls_from_response(response, name_map=tool_name_map) or pending_tool_calls:
                        yield ProviderChunk(type="tool_call", tool_call=call)
                    final_text = _chatgpt_response_text(response)
                    if final_text and not streamed_text:
                        yield ProviderChunk(type="text_delta", delta=final_text)
                    yield ProviderChunk(type="status", status=ProviderStatus.COMPLETED)
                    return
                if event_type == "response.incomplete":
                    response = event.get("response") or {}
                    details = response.get("incomplete_details") or event.get("incomplete_details") or {}
                    reason = details.get("reason") or "response_incomplete"
                    yield _failed_status(
                        "incomplete_response",
                        f"ChatGPT provider returned an incomplete response: {reason}",
                        retryable=True,
                    )
                    return
                if event_type == "response.failed":
                    error = event.get("error") or {}
                    yield ProviderChunk(
                        type="status",
                        status=ProviderStatus.FAILED,
                        error={
                            "code": error.get("code") or "provider_error",
                            "message": error.get("message") or "ChatGPT provider failed.",
                            "retryable": False,
                        },
                    )
                    return
            if not completed:
                yield _failed_status(
                    "incomplete_stream",
                    "ChatGPT provider stream ended before response.completed.",
                    retryable=True,
                )
        except Exception as exc:
            yield ProviderChunk(
                type="status",
                status=ProviderStatus.FAILED,
                error={
                    "code": "provider_error",
                    "message": _safe_error_message(exc),
                    "retryable": True,
                },
            )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        }
        account_id = _chatgpt_account_id_from_jwt(self.token)
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

    def _urlopen_transport(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterable[bytes]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            yield from response


class OllamaCompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        api_key: str = "",
        timeout_seconds: int = 120,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._urlopen_transport

    def stream(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        tools: Sequence[ToolDeclaration],
        system: str,
        limits: dict[str, Any] | None = None,
    ) -> Iterable[ProviderChunk]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_ollama_messages(messages, system),
            "stream": True,
            "think": False,
        }
        if tools:
            payload["tools"] = _to_ollama_tools(tools)
        if limits and limits.get("max_tokens"):
            payload["options"] = {"num_predict": limits["max_tokens"]}

        pending_tool_calls: list[dict[str, Any]] = []
        completed = False
        try:
            for item in self._transport(f"{self.base_url}/api/chat", payload, self._headers()):
                if isinstance(item, bytes):
                    item = item.decode("utf-8")
                line = item.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                message = chunk.get("message") or {}
                content = message.get("content") or ""
                if content:
                    yield ProviderChunk(type="text_delta", delta=content)
                for tool_call in message.get("tool_calls") or []:
                    normalized = _normalize_ollama_tool_call(
                        tool_call,
                        index=len(pending_tool_calls),
                    )
                    if normalized is None:
                        yield ProviderChunk(
                            type="status",
                            status=ProviderStatus.FAILED,
                            error={
                                "code": "invalid_tool_call",
                                "message": "Ollama returned a tool call without a function name.",
                                "retryable": False,
                            },
                        )
                        return
                    if isinstance(normalized["arguments"], str):
                        try:
                            normalized["arguments"] = json.loads(normalized["arguments"])
                        except json.JSONDecodeError:
                            yield ProviderChunk(
                                type="status",
                                status=ProviderStatus.FAILED,
                                error={
                                    "code": "invalid_tool_arguments",
                                    "message": "Ollama returned tool arguments that are not valid JSON.",
                                    "retryable": False,
                                },
                            )
                            return
                    pending_tool_calls.append(normalized)

                if chunk.get("done"):
                    completed = True
                    usage = {
                        "input_tokens": int(chunk.get("prompt_eval_count") or 0),
                        "output_tokens": int(chunk.get("eval_count") or 0),
                    }
                    if usage["input_tokens"] or usage["output_tokens"]:
                        yield ProviderChunk(type="usage", usage=usage)
                    for call in pending_tool_calls:
                        yield ProviderChunk(type="tool_call", tool_call=call)
                    yield ProviderChunk(type="status", status=ProviderStatus.COMPLETED)
                    return
            if not completed:
                yield _failed_status(
                    "incomplete_stream",
                    "Ollama provider stream ended before a done message.",
                    retryable=True,
                )
        except Exception as exc:
            yield ProviderChunk(
                type="status",
                status=ProviderStatus.FAILED,
                error={
                    "code": "provider_error",
                    "message": _safe_error_message(exc),
                    "retryable": True,
                },
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _urlopen_transport(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterable[bytes]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            yield from response


def _to_ollama_messages(messages: Sequence[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role") or "user"
        metadata = message.get("metadata") or {}
        if role == "tool_call":
            result.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_provider_tool_call(metadata)],
                }
            )
            continue
        payload = {
            "role": role,
            "content": message.get("content") or "",
        }
        if role == "tool" and metadata.get("tool_call_id"):
            payload["tool_call_id"] = metadata["tool_call_id"]
        result.append(payload)
    return result


def _to_openai_messages(messages: Sequence[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role") or "user"
        metadata = message.get("metadata") or {}
        if role == "tool_call":
            result.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_provider_tool_call(metadata)],
                }
            )
            continue
        if role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": metadata.get("tool_call_id") or "",
                    "content": message.get("content") or "",
                }
            )
        else:
            result.append({"role": role, "content": message.get("content") or ""})
    return result


def _to_chatgpt_input(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role") or "user"
        metadata = message.get("metadata") or {}
        content = message.get("content") or ""
        if role == "system":
            role = "user"
            content = f"[System context]\n{content}"

        if role == "tool_call":
            import json as _json
            args = metadata.get("tool_arguments") or {}
            raw_name = metadata.get("tool_name") or ""
            result.append({
                "type": "function_call",
                "call_id": metadata.get("tool_call_id") or "",
                "name": _safe_tool_name(raw_name),   # must match the declared safe name
                "arguments": _json.dumps(args) if isinstance(args, dict) else str(args),
            })
            continue

        if role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": metadata.get("tool_call_id") or "",
                    "output": content,
                }
            )
            continue

        content_type = "output_text" if role == "assistant" else "input_text"
        if content:
            result.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": content_type, "text": content}],
                }
            )
    return result


def _provider_tool_call(metadata: dict[str, Any]) -> dict[str, Any]:
    import json as _json

    args = metadata.get("tool_arguments") or {}
    return {
        "id": metadata.get("tool_call_id") or "",
        "type": "function",
        "function": {
            "name": metadata.get("tool_name") or "",
            "arguments": _json.dumps(args) if isinstance(args, dict) else str(args),
        },
    }


def _to_ollama_tools(tools: Sequence[ToolDeclaration]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {
                    "type": "object",
                    "properties": {},
                },
            },
        }
        for tool in tools
    ]


def _to_openai_tools(tools: Sequence[ToolDeclaration]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {
                    "type": "object",
                    "properties": {},
                },
            },
        }
        for tool in tools
    ]


def _to_chatgpt_tools(
    tools: Sequence[ToolDeclaration],
    *,
    name_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    name_map = name_map or {}
    return [
        {
            "type": "function",
            "name": name_map.get(tool.name, tool.name),
            "description": tool.description,
            "parameters": tool.input_schema or {
                "type": "object",
                "properties": {},
            },
        }
        for tool in tools
    ]


def _tool_name_map(tools: Sequence[ToolDeclaration]) -> dict[str, str]:
    used: set[str] = set()
    result: dict[str, str] = {}
    for tool in tools:
        safe_name = _safe_tool_name(tool.name)
        candidate = safe_name
        suffix = 2
        while candidate in used:
            candidate = f"{safe_name}_{suffix}"
            suffix += 1
        used.add(candidate)
        result[tool.name] = candidate
    return result


def _safe_tool_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in name)


def _reverse_tool_name_map(name_map: dict[str, str]) -> dict[str, str]:
    return {safe: original for original, safe in name_map.items()}


def _normalize_ollama_tool_call(
    tool_call: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any] | None:
    function = tool_call.get("function") or {}
    name = function.get("name") or tool_call.get("name")
    if not name:
        return None
    arguments = function.get("arguments", tool_call.get("arguments", {}))
    return {
        "id": tool_call.get("id") or f"call_{index}",
        "name": name,
        "arguments": arguments or {},
    }


def _normalize_chatgpt_response_tool_call(
    item: dict[str, Any],
    *,
    name_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if item.get("type") != "function_call":
        return None
    name = item.get("name") or ""
    call_id = item.get("call_id") or item.get("id") or ""
    if not name or not call_id:
        return None
    reverse_name_map = _reverse_tool_name_map(name_map or {})
    name = reverse_name_map.get(name, name)
    arguments = item.get("arguments") or "{}"
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    return {"id": call_id, "name": name, "arguments": arguments}


def _chatgpt_tool_calls_from_response(
    response: dict[str, Any],
    *,
    name_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        call = _normalize_chatgpt_response_tool_call(item, name_map=name_map)
        if call is not None:
            calls.append(call)
    return calls


def _chatgpt_response_text(response: dict[str, Any]) -> str:
    if response.get("output_text"):
        return response["output_text"]
    parts: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "".join(parts)


def _chatgpt_account_id_from_jwt(token: str) -> str:
    try:
        import base64

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        auth_claim = data.get("https://api.openai.com/auth", {})
        return auth_claim.get("chatgpt_account_id") or auth_claim.get("user_id", "")
    except Exception:
        return ""


def _failed_status(code: str, message: str, *, retryable: bool) -> ProviderChunk:
    return ProviderChunk(
        type="status",
        status=ProviderStatus.FAILED,
        error={"code": code, "message": message, "retryable": retryable},
    )


def _iter_sse_json(
    lines: Iterable[bytes | str],
    *,
    done_flag: dict[str, bool] | None = None,
) -> Iterable[dict[str, Any]]:
    for item in lines:
        if isinstance(item, bytes):
            item = item.decode("utf-8")
        for raw_line in item.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            line = line.removeprefix("data:").strip()
            if line == "[DONE]":
                if done_flag is not None:
                    done_flag["seen"] = True
                return
            yield json.loads(line)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, error.HTTPError):
        detail = ""
        try:
            raw = exc.read(2000)
            detail = raw.decode("utf-8", errors="replace").replace("\n", " ")
        except Exception:
            detail = ""
        message = f"HTTP {exc.code}: {exc.reason}"
        if detail:
            message = f"{message} - {detail}"
        return message[:500]
    message = str(exc) or exc.__class__.__name__
    return message.replace("\n", " ")[:500]
