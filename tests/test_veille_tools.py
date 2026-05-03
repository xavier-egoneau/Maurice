from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.veille.tools import add_topic, build_dream_input, list_topics, remove_topic, run_watch


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(
            {
                "results": [
                    {
                        "title": "SQLite release notes",
                        "url": "https://example.test/sqlite",
                        "content": "A useful change for local storage.",
                        "engine": "test",
                    }
                ]
            }
        ).encode("utf-8")


class CapturingOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        return FakeResponse()


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def test_veille_topics_can_feed_dreaming_without_search_config(tmp_path) -> None:
    permission_context = context(tmp_path)
    added = add_topic({"topic": "SQLite security", "tags": ["database"]}, permission_context)

    dream_input = build_dream_input(permission_context)

    assert added.ok
    assert dream_input.skill == "veille"
    assert dream_input.signals[0].data["reason"] == "search_not_configured"
    assert "SQLite security" in dream_input.signals[0].summary


def test_veille_dream_input_uses_runtime_skill_config(tmp_path) -> None:
    permission_context = context(tmp_path)
    add_topic({"topic": "SQLite", "query": "SQLite release", "tags": ["database"]}, permission_context)

    dream_input = build_dream_input(
        permission_context,
        config={},
        all_skill_configs={"web": {"base_url": "http://search.test"}},
        opener=lambda _request, timeout: FakeResponse(),
    )

    assert dream_input.trust == "external_untrusted"
    assert dream_input.signals[0].data["results"][0]["title"] == "SQLite release notes"
    parsed = urlparse(dream_input.signals[0].data["results"][0]["url"])
    assert parsed.netloc == "example.test"


def test_veille_run_uses_configured_search_and_topic_management(tmp_path) -> None:
    permission_context = context(tmp_path)
    added = add_topic({"topic": "SQLite", "query": "SQLite release", "tags": ["database"]}, permission_context)
    topic_id = added.data["topic"]["id"]

    result = run_watch(
        {"topic_ids": [topic_id], "max_results": 1},
        permission_context,
        config={"base_url": "http://search.test"},
        opener=lambda _request, timeout: FakeResponse(),
    )
    listed = list_topics({}, permission_context)
    removed = remove_topic({"topic_id": topic_id}, permission_context)

    assert result.ok
    assert result.trust == "external_untrusted"
    assert result.data["signals"][0]["data"]["results"][0]["title"] == "SQLite release notes"
    assert listed.data["topics"][0]["last_checked_at"] is not None
    assert listed.data["topics"][0]["seen_urls"] == ["https://example.test/sqlite"]
    assert removed.ok


def test_veille_uses_day_time_range_and_skips_seen_results(tmp_path) -> None:
    permission_context = context(tmp_path)
    added = add_topic({"topic": "SQLite", "query": "SQLite release", "tags": ["database"]}, permission_context)
    topic_id = added.data["topic"]["id"]
    opener = CapturingOpener()

    first = run_watch(
        {"topic_ids": [topic_id]},
        permission_context,
        config={"base_url": "http://search.test"},
        opener=opener,
    )
    second = run_watch(
        {"topic_ids": [topic_id]},
        permission_context,
        config={"base_url": "http://search.test"},
        opener=opener,
    )

    query = parse_qs(urlparse(opener.urls[0]).query)
    assert query["time_range"] == ["day"]
    assert first.data["signals"][0]["data"]["reason"] == "fresh_results"
    assert first.data["signals"][0]["data"]["results"][0]["url"] == "https://example.test/sqlite"
    assert second.data["signals"] == []
    assert second.data["skipped"] == [
        {"topic_id": topic_id, "topic": "SQLite", "reason": "no_new_results"}
    ]
