"""Web system skill tools."""

from __future__ import annotations

import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 20
USER_AGENT = "Maurice/0.1 web-skill"


def web_tool_executors(
    context: PermissionContext,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "web.fetch": lambda arguments: fetch(arguments, context),
        "web.search": lambda arguments: search(arguments, context, config or {}),
        "maurice.system_skills.web.tools.fetch": lambda arguments: fetch(arguments, context),
        "maurice.system_skills.web.tools.search": lambda arguments: search(arguments, context, config or {}),
    }


def fetch(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    opener: Any = urlopen,
) -> ToolResult:
    del context
    try:
        url = _require_url(arguments.get("url"))
        max_bytes = _positive_int(arguments.get("max_bytes"), DEFAULT_MAX_BYTES, "max_bytes")
        max_chars = _positive_int(arguments.get("max_chars"), DEFAULT_MAX_CHARS, "max_chars")
        timeout = _positive_int(
            arguments.get("timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS,
            "timeout_seconds",
        )
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))

    try:
        response = opener(_request(url), timeout=timeout)
        with response:
            status = getattr(response, "status", None) or getattr(response, "code", None)
            headers = getattr(response, "headers", Message())
            raw = response.read(max_bytes + 1)
    except HTTPError as exc:
        return _error("http_error", f"HTTP {exc.code} while fetching {url}", retryable=500 <= exc.code < 600)
    except URLError as exc:
        return _error("network_error", f"Could not fetch {url}: {exc.reason}", retryable=True)
    except OSError as exc:
        return _error("network_error", f"Could not fetch {url}: {exc}", retryable=True)

    truncated_bytes = len(raw) > max_bytes
    if truncated_bytes:
        raw = raw[:max_bytes]
    content_type = headers.get("content-type", "")
    text = _decode(raw, content_type)
    truncated_chars = len(text) > max_chars
    if truncated_chars:
        text = text[:max_chars]

    return ToolResult(
        ok=True,
        summary=f"Fetched {url}",
        data={
            "url": url,
            "host": urlparse(url).hostname,
            "status": status,
            "content_type": content_type,
            "text": text,
            "truncated": truncated_bytes or truncated_chars,
        },
        trust="external_untrusted",
        artifacts=[{"type": "url", "data": {"url": url}}],
        events=[{"name": "web.fetched", "payload": {"url": url, "status": status}}],
        error=None,
    )


def search(
    arguments: dict[str, Any],
    context: PermissionContext,
    config: dict[str, Any] | None = None,
    *,
    opener: Any = urlopen,
) -> ToolResult:
    del context
    config = config or {}
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error("invalid_arguments", "web.search requires a non-empty query.")

    try:
        base_url = _require_url(arguments.get("base_url") or config.get("base_url"))
        max_results = _positive_int(arguments.get("max_results"), int(config.get("max_results", 5)), "max_results")
        timeout = _positive_int(
            arguments.get("timeout_seconds"),
            int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            "timeout_seconds",
        )
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))

    search_url = _searxng_search_url(base_url, query)
    try:
        response = opener(_request(search_url, accept="application/json"), timeout=timeout)
        with response:
            raw = response.read(DEFAULT_MAX_BYTES + 1)
    except HTTPError as exc:
        return _error("http_error", f"HTTP {exc.code} while searching {base_url}", retryable=500 <= exc.code < 600)
    except URLError as exc:
        return _error("network_error", f"Could not search {base_url}: {exc.reason}", retryable=True)
    except OSError as exc:
        return _error("network_error", f"Could not search {base_url}: {exc}", retryable=True)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _error("decode_error", f"Search endpoint did not return valid JSON: {exc}")

    results = []
    for item in payload.get("results", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content") or item.get("summary"),
                "score": item.get("score"),
                "engine": item.get("engine"),
            }
        )

    return ToolResult(
        ok=True,
        summary=f"Found {len(results)} web results.",
        data={
            "query": query,
            "base_url": base_url,
            "results": results,
        },
        trust="external_untrusted",
        artifacts=[{"type": "url", "data": {"url": search_url}}],
        events=[{"name": "web.searched", "payload": {"query": query, "count": len(results)}}],
        error=None,
    )


def _request(url: str, *, accept: str = "*/*") -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def _require_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("web tools require a non-empty url.")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("web tools only support http(s) URLs with a host.")
    return value.strip()


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _decode(raw: bytes, content_type: str) -> str:
    headers = Message()
    headers["content-type"] = content_type
    charset = headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _searxng_search_url(base_url: str, query: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    parsed = urlparse(urljoin(base, "search"))
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_items.update({"q": query, "format": "json"})
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def _error(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": retryable},
    )
