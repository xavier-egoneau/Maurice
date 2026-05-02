"""Host-provided vision backends for the vision skill."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib import request


DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_VISION_MODEL = "gemma4"


def build_vision_backend(config: dict[str, Any] | None = None):
    """Return the configured vision backend callable.

    The vision skill stays kernel-neutral; the host wires a local Ollama model by
    default so CLI, web, and daemon runtimes share the same behavior.
    """

    cfg = dict(config or {})
    backend = str(cfg.get("backend") or cfg.get("provider") or "ollama").strip().lower()
    if backend in {"", "none", "disabled", "off", "false"}:
        return None
    if backend != "ollama":
        return None
    return ollama_vision_backend(cfg)


def ollama_vision_backend(config: dict[str, Any] | None = None, *, transport: Any = None):
    cfg = dict(config or {})
    base_url = str(cfg.get("base_url") or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    model = str(cfg.get("model") or DEFAULT_OLLAMA_VISION_MODEL)
    timeout_seconds = int(cfg.get("timeout_seconds") or 120)
    transport = transport or _urlopen_transport

    def backend(payload: dict[str, Any]) -> dict[str, Any]:
        image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
        image_path = Path(str(image.get("path") or "")).expanduser()
        if not image_path.is_file():
            raise FileNotFoundError(f"Image not found: {image_path}")
        prompt = str(payload.get("prompt") or "Describe this image.").strip()
        raw = image_path.read_bytes()
        request_payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(raw).decode("ascii")],
                }
            ],
            "stream": False,
            "think": False,
        }
        response = transport(
            f"{base_url}/api/chat",
            request_payload,
            {"Content-Type": "application/json"},
            timeout_seconds,
        )
        content = _ollama_content(response)
        return {
            "summary": "Image analyzed with Ollama.",
            "description": content,
            "model": model,
            "base_url": base_url,
        }

    return backend


def _urlopen_transport(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: int,
) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return response.read()


def _ollama_content(response: bytes | str | dict[str, Any] | Iterable[bytes | str]) -> str:
    if isinstance(response, dict):
        payload = response
    elif isinstance(response, (bytes, str)):
        raw = response.decode("utf-8") if isinstance(response, bytes) else response
        payload = json.loads(raw)
    else:
        chunks = []
        for item in response:
            chunks.append(item.decode("utf-8") if isinstance(item, bytes) else str(item))
        payload = json.loads("".join(chunks))
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = str(message.get("content") or payload.get("response") or "").strip()
    if not content:
        raise ValueError("Ollama vision response did not contain text.")
    return content
