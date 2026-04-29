"""Model discovery helpers shared by onboarding surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from urllib import request as urlrequest


def chatgpt_model_choices() -> list[tuple[str, str]]:
    cache_path = Path.home() / ".codex" / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    rows: list[tuple[int, str, str]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = str(model.get("slug") or "").strip()
        if not slug:
            continue
        visibility = str(model.get("visibility") or "list")
        if visibility not in {"list", "default", ""}:
            continue
        priority = model.get("priority")
        if not isinstance(priority, int):
            priority = 999
        display_name = str(model.get("display_name") or slug).strip()
        description = str(model.get("description") or "").strip()
        label = display_name if not description else f"{display_name}: {description}"
        rows.append((priority, slug, label))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [(slug, label) for _, slug, label in rows]


def ollama_model_choices(base_url: str, *, api_key: str = "") -> list[tuple[str, str]]:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        req = urlrequest.Request(url, headers=headers)
        with urlrequest.urlopen(req, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    choices: list[tuple[str, str]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "").strip()
        if not name:
            continue
        details = model.get("details") if isinstance(model.get("details"), dict) else {}
        family = str(details.get("family") or "").strip()
        size = model.get("size")
        label_parts = [part for part in [family, format_bytes(size)] if part]
        choices.append((name, ", ".join(label_parts) or name))
    return sorted(choices, key=lambda item: item[0])


def format_bytes(value: object) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"
