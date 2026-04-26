from __future__ import annotations

import json
from io import BytesIO
from urllib.request import Request

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.web.tools import fetch, search


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def test_fetch_returns_bounded_external_text(tmp_path) -> None:
    calls = []

    def opener(request: Request, *, timeout: int):
        calls.append((request, timeout))
        return FakeResponse(
            "hello from the web".encode("utf-8"),
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    result = fetch(
        {"url": "https://example.com/page", "max_chars": 5},
        context(tmp_path),
        opener=opener,
    )

    assert result.ok
    assert result.trust == "external_untrusted"
    assert result.data["text"] == "hello"
    assert result.data["truncated"] is True
    assert calls[0][0].full_url == "https://example.com/page"
    assert calls[0][1] == 20


def test_fetch_rejects_non_http_urls(tmp_path) -> None:
    result = fetch({"url": "file:///etc/passwd"}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_arguments"


def test_search_parses_searxng_results(tmp_path) -> None:
    calls = []
    payload = {
        "results": [
            {
                "title": "Maurice",
                "url": "https://example.com/maurice",
                "content": "A result.",
                "score": 1.0,
                "engine": "test",
            },
            {
                "title": "Other",
                "url": "https://example.com/other",
                "content": "Another result.",
            },
        ]
    }

    def opener(request: Request, *, timeout: int):
        calls.append((request, timeout))
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    result = search(
        {
            "query": "maurice runtime",
            "base_url": "https://search.example",
            "max_results": 1,
        },
        context(tmp_path),
        opener=opener,
    )

    assert result.ok
    assert result.trust == "external_untrusted"
    assert result.data["results"] == [
        {
            "title": "Maurice",
            "url": "https://example.com/maurice",
            "content": "A result.",
            "score": 1.0,
            "engine": "test",
        }
    ]
    assert calls[0][0].full_url == "https://search.example/search?q=maurice+runtime&format=json"
    assert calls[0][1] == 20


def test_search_requires_base_url(tmp_path) -> None:
    result = search({"query": "maurice"}, context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_arguments"
