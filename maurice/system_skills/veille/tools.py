"""Watch-topic skill tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field

from maurice.host.paths import workspace_skills_config_path
from maurice.kernel.config import read_yaml_file
from maurice.kernel.contracts import DreamInput, MauriceModel, ToolResult
from maurice.kernel.permissions import PermissionContext

DEFAULT_MAX_RESULTS = 3
DEFAULT_TIMEOUT_SECONDS = 20
MAX_BYTES = 1_000_000
USER_AGENT = "Maurice/0.1 veille-skill"
DEFAULT_TIME_RANGE = "day"
VALID_TIME_RANGES = {"day", "week", "month", "year"}
MAX_SEEN_URLS = 200


class WatchTopic(MauriceModel):
    id: str
    topic: str
    query: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_checked_at: datetime | None = None
    seen_urls: list[str] = Field(default_factory=list)


class WatchStoreFile(MauriceModel):
    topics: list[WatchTopic] = Field(default_factory=list)


def build_executors(ctx: Any) -> dict[str, Any]:
    config = _merged_config(ctx.permission_context, ctx.skill_config or {}, ctx.all_skill_configs or {})
    return veille_tool_executors(ctx.permission_context, config=config)


def veille_tool_executors(
    context: PermissionContext,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or _merged_config(context, {}, {})
    return {
        "veille.add_topic": lambda arguments: add_topic(arguments, context),
        "veille.list_topics": lambda arguments: list_topics(arguments, context),
        "veille.remove_topic": lambda arguments: remove_topic(arguments, context),
        "veille.run": lambda arguments: run_watch(arguments, context, config=config),
        "maurice.system_skills.veille.tools.add_topic": lambda arguments: add_topic(arguments, context),
        "maurice.system_skills.veille.tools.list_topics": lambda arguments: list_topics(arguments, context),
        "maurice.system_skills.veille.tools.remove_topic": lambda arguments: remove_topic(arguments, context),
        "maurice.system_skills.veille.tools.run_watch": lambda arguments: run_watch(
            arguments,
            context,
            config=config,
        ),
    }


def add_topic(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    topic = arguments.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return _error("invalid_arguments", "veille.add_topic requires a non-empty topic.")
    query = arguments.get("query") or topic
    if not isinstance(query, str) or not query.strip():
        return _error("invalid_arguments", "veille.add_topic query must be a string.")
    tags = arguments.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return _error("invalid_arguments", "veille.add_topic tags must be strings.")

    store = _load_store(context)
    watch = WatchTopic(
        id=f"watch_{uuid4().hex}",
        topic=topic.strip(),
        query=query.strip(),
        tags=tags,
    )
    store.topics.append(watch)
    _save_store(context, store)
    return ToolResult(
        ok=True,
        summary=f"Watch topic added: {watch.topic}",
        data={"topic": watch.model_dump(mode="json")},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "veille.topic_added", "payload": {"id": watch.id}}],
        error=None,
    )


def list_topics(_arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    store = _load_store(context)
    return ToolResult(
        ok=True,
        summary=f"Found {len(store.topics)} watch topic(s).",
        data={"topics": [topic.model_dump(mode="json") for topic in store.topics]},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "veille.topics_listed", "payload": {"count": len(store.topics)}}],
        error=None,
    )


def remove_topic(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    topic_id = arguments.get("topic_id")
    if not isinstance(topic_id, str) or not topic_id:
        return _error("invalid_arguments", "veille.remove_topic requires topic_id.")
    store = _load_store(context)
    kept = [topic for topic in store.topics if topic.id != topic_id]
    if len(kept) == len(store.topics):
        return _error("not_found", f"Unknown watch topic: {topic_id}")
    store.topics = kept
    _save_store(context, store)
    return ToolResult(
        ok=True,
        summary=f"Watch topic removed: {topic_id}",
        data={"topic_id": topic_id},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "veille.topic_removed", "payload": {"id": topic_id}}],
        error=None,
    )


def run_watch(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    config: dict[str, Any] | None = None,
    opener: Any = urlopen,
) -> ToolResult:
    try:
        max_results = _positive_int(arguments.get("max_results"), int((config or {}).get("max_results", DEFAULT_MAX_RESULTS)), "max_results")
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    topic_ids = arguments.get("topic_ids")
    if topic_ids is not None and (not isinstance(topic_ids, list) or not all(isinstance(item, str) for item in topic_ids)):
        return _error("invalid_arguments", "veille.run topic_ids must be strings.")

    collected = _collect_watch(context, config=config, max_results=max_results, topic_ids=topic_ids, opener=opener)
    return ToolResult(
        ok=True,
        summary=f"Watch produced {len(collected['signals'])} signal(s).",
        data=collected,
        trust="external_untrusted" if collected["searched"] else "local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "veille.ran", "payload": {"signal_count": len(collected["signals"])}}],
        error=None,
    )

def build_dream_input(
    context: PermissionContext,
    *,
    opener: Any = urlopen,
    config: dict[str, Any] | None = None,
    all_skill_configs: dict[str, dict[str, Any]] | None = None,
) -> DreamInput:
    config = _merged_config(context, config or {}, all_skill_configs or {})
    collected = _collect_watch(
        context,
        config=config,
        max_results=int(config.get("max_results", DEFAULT_MAX_RESULTS)),
        topic_ids=None,
        opener=opener,
        update_checked=True,
    )
    return DreamInput(
        skill="veille",
        trust="external_untrusted" if collected["searched"] else "local_mutable",
        freshness={"generated_at": datetime.now(UTC), "expires_at": None},
        signals=collected["signals"],
        limits=collected["limits"],
    )


def _collect_watch(
    context: PermissionContext,
    *,
    config: dict[str, Any] | None,
    max_results: int,
    topic_ids: list[str] | None,
    opener: Any,
    update_checked: bool = True,
) -> dict[str, Any]:
    store = _load_store(context)
    selected = [
        topic
        for topic in store.topics
        if topic_ids is None or topic.id in topic_ids
    ]
    base_url = _search_base_url(config or {})
    time_range = _search_time_range(config or {})
    fresh_only = _fresh_only(config or {})
    signals: list[dict[str, Any]] = []
    searched = False
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_updates: dict[str, list[str]] = {}
    for topic in selected:
        if not base_url:
            signals.append(_topic_signal(topic, [], reason="search_not_configured"))
            continue
        searched = True
        try:
            results = _search(
                base_url,
                topic.query,
                max_results=max_results,
                opener=opener,
                time_range=time_range,
            )
            seen_updates[topic.id] = [_result_key(result) for result in results if _result_key(result)]
            selected_results = _fresh_results(topic, results) if fresh_only else results
            if selected_results:
                reason = "fresh_results" if fresh_only else "search_completed"
                signals.append(_topic_signal(topic, selected_results, reason=reason))
            else:
                skipped.append(
                    {
                        "topic_id": topic.id,
                        "topic": topic.topic,
                        "reason": "no_new_results" if results else "no_results",
                    }
                )
        except Exception as exc:
            errors.append(f"{topic.topic}: {exc}")
            signals.append(_topic_signal(topic, [], reason="search_failed", error=str(exc)))
    if update_checked and selected:
        checked_at = datetime.now(UTC)
        selected_ids = {topic.id for topic in selected}
        for topic in store.topics:
            if topic.id in selected_ids:
                topic.last_checked_at = checked_at
                if seen_updates.get(topic.id):
                    topic.seen_urls = _merge_seen_urls(topic.seen_urls, seen_updates[topic.id])
        _save_store(context, store)
    limits = [
        f"At most {max_results} search results per watch topic.",
        f"Search time range: {time_range}.",
        "Only unseen result URLs are surfaced when fresh_only is enabled.",
        "Search results are external and must be verified before action.",
    ]
    if not base_url:
        limits.append("No SearxNG base URL configured; topics are surfaced without live search.")
    if errors:
        limits.append("Some watch searches failed: " + "; ".join(errors[:3]))
    if skipped:
        limits.append(f"{len(skipped)} watch topic(s) had no fresh result.")
    return {
        "topics": [topic.model_dump(mode="json") for topic in selected],
        "signals": signals,
        "searched": searched,
        "base_url": base_url,
        "time_range": time_range,
        "fresh_only": fresh_only,
        "skipped": skipped,
        "limits": limits,
        "errors": errors,
    }


def _topic_signal(
    topic: WatchTopic,
    results: list[dict[str, Any]],
    *,
    reason: str = "search_completed",
    error: str | None = None,
) -> dict[str, Any]:
    if results:
        titles = [str(result.get("title") or result.get("url") or "").strip() for result in results]
        summary = f"{topic.topic}: " + "; ".join(title for title in titles if title)
    else:
        summary = f"{topic.topic}: no live watch results ({reason})."
    return {
        "id": f"sig_veille_{topic.id}",
        "type": "watch_topic",
        "summary": summary,
        "data": {
            "topic": topic.model_dump(mode="json"),
            "results": results,
            "reason": reason,
            "error": error,
        },
    }


def _search(
    base_url: str,
    query: str,
    *,
    max_results: int,
    opener: Any,
    time_range: str | None,
) -> list[dict[str, Any]]:
    search_url = _searxng_search_url(base_url, query, time_range=time_range)
    try:
        response = opener(_request(search_url), timeout=DEFAULT_TIMEOUT_SECONDS)
        with response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"network error: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid search JSON: {exc}") from exc
    results = []
    for item in payload.get("results", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content") or item.get("summary"),
                "engine": item.get("engine"),
            }
        )
    return results


def _load_store(context: PermissionContext) -> WatchStoreFile:
    path = _store_path(context)
    if not path.exists():
        return WatchStoreFile()
    try:
        return WatchStoreFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return WatchStoreFile()


def _save_store(context: PermissionContext, store: WatchStoreFile) -> None:
    path = _store_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")


def _store_path(context: PermissionContext) -> Path:
    return Path(context.variables()["$agent_workspace"]) / "veille" / "topics.json"


def _merged_config(
    context: PermissionContext,
    skill_config: dict[str, Any],
    all_skill_configs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    config = _load_workspace_config(context)
    config.update(skill_config or {})
    web_config = all_skill_configs.get("web") or {}
    if web_config.get("base_url") and not config.get("base_url"):
        config["base_url"] = web_config["base_url"]
    return config


def _load_workspace_config(context: PermissionContext) -> dict[str, Any]:
    workspace = Path(context.variables()["$workspace"])
    data = read_yaml_file(workspace_skills_config_path(workspace))
    skills = data.get("skills") if isinstance(data.get("skills"), dict) else {}
    veille_config = skills.get("veille") if isinstance(skills.get("veille"), dict) else {}
    web_config = skills.get("web") if isinstance(skills.get("web"), dict) else {}
    config = dict(veille_config)
    if web_config.get("base_url") and not config.get("base_url"):
        config["base_url"] = web_config["base_url"]
    return config


def _search_base_url(config: dict[str, Any]) -> str:
    value = config.get("search_base_url") or config.get("base_url")
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return value.strip()


def _search_time_range(config: dict[str, Any]) -> str:
    value = config.get("time_range") or config.get("freshness")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_TIME_RANGE
    normalized = value.strip().lower()
    return normalized if normalized in VALID_TIME_RANGES else DEFAULT_TIME_RANGE


def _fresh_only(config: dict[str, Any]) -> bool:
    value = config.get("fresh_only")
    return True if value is None else bool(value)


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _searxng_search_url(base_url: str, query: str, *, time_range: str | None = None) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    parsed = urlparse(urljoin(base, "search"))
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_items.update({"q": query, "format": "json"})
    if time_range:
        query_items["time_range"] = time_range
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def _fresh_results(topic: WatchTopic, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {url for url in topic.seen_urls if url}
    fresh = []
    for result in results:
        key = _result_key(result)
        if key and key in seen:
            continue
        fresh.append(result)
    return fresh


def _result_key(result: dict[str, Any]) -> str:
    url = result.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    title = result.get("title")
    return title.strip() if isinstance(title, str) else ""


def _merge_seen_urls(existing: list[str], new: list[str]) -> list[str]:
    merged: list[str] = []
    for value in [*existing, *new]:
        if not value or value in merged:
            continue
        merged.append(value)
    return merged[-MAX_SEEN_URLS:]


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": False},
    )
