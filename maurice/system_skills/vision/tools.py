"""Vision system skill tools."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext

DEFAULT_MAX_BYTES = 10_000_000
SUPPORTED_FORMATS = {"png", "jpeg", "gif", "webp"}

VisionBackend = Callable[[dict[str, Any]], dict[str, Any]]


def build_executors(ctx: Any) -> dict[str, Any]:
    return vision_tool_executors(
        ctx.permission_context,
        backend=ctx.extra.get("vision_backend"),
        config=ctx.skill_config or None,
    )


def vision_tool_executors(
    context: PermissionContext,
    *,
    backend: VisionBackend | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "vision.inspect": lambda arguments: inspect(arguments, context),
        "vision.analyze": lambda arguments: analyze(arguments, context, backend=backend, config=config or {}),
        "maurice.system_skills.vision.tools.inspect": lambda arguments: inspect(arguments, context),
        "maurice.system_skills.vision.tools.analyze": lambda arguments: analyze(
            arguments,
            context,
            backend=backend,
            config=config or {},
        ),
    }


def inspect(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    try:
        image = _read_image(arguments, context)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    except FileNotFoundError as exc:
        return _error("not_found", str(exc))

    metadata = _image_metadata(image["raw"])
    if metadata["format"] not in SUPPORTED_FORMATS:
        return _error("unsupported_image", "vision.inspect supports png, jpeg, gif, and webp images.")
    data = {
        "path": image["path"],
        "bytes": len(image["raw"]),
        "sha256": hashlib.sha256(image["raw"]).hexdigest(),
        **metadata,
    }
    return ToolResult(
        ok=True,
        summary=f"Inspected image: {image['path']}",
        data=data,
        trust="local_mutable",
        artifacts=[{"type": "image", "path": image["path"], "data": data}],
        events=[{"name": "vision.image_inspected", "payload": {"path": image["path"], "format": metadata["format"]}}],
        error=None,
    )


def analyze(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    backend: VisionBackend | None = None,
    config: dict[str, Any] | None = None,
) -> ToolResult:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("invalid_arguments", "vision.analyze requires a non-empty prompt.")
    if backend is None:
        return _error("backend_unconfigured", "vision.analyze requires a configured vision backend.")

    inspected = inspect(arguments, context)
    if not inspected.ok:
        return inspected
    try:
        result = backend(
            {
                "prompt": prompt.strip(),
                "image": inspected.data,
                "config": config or {},
            }
        )
    except Exception as exc:
        return _error("backend_failed", f"vision backend failed: {exc}", retryable=True)
    if not isinstance(result, dict):
        return _error("backend_failed", "vision backend must return an object.")

    return ToolResult(
        ok=True,
        summary=result.get("summary") or "Image analyzed.",
        data={
            "image": inspected.data,
            "analysis": result,
            "trust_note": "image-derived content is model/backend output",
        },
        trust="external_untrusted",
        artifacts=inspected.artifacts,
        events=[{"name": "vision.image_analyzed", "payload": {"path": inspected.data["path"]}}],
        error=None,
    )


def _read_image(arguments: dict[str, Any], context: PermissionContext) -> dict[str, Any]:
    path = _resolve_path(arguments.get("path"), context)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    max_bytes = _positive_int(arguments.get("max_bytes"), DEFAULT_MAX_BYTES, "max_bytes")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"Image exceeds max_bytes: {len(raw)} > {max_bytes}")
    return {"path": str(path), "raw": raw}


def _resolve_path(value: Any, context: PermissionContext) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("vision tools require a non-empty path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(context.variables()["$workspace"]) / path
    return path.resolve()


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _image_metadata(raw: bytes) -> dict[str, Any]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return {"format": "png", "mime_type": "image/png", "width": width, "height": height}
    if raw.startswith(b"\xff\xd8"):
        size = _jpeg_size(raw)
        return {"format": "jpeg", "mime_type": "image/jpeg", **size}
    if raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        width, height = struct.unpack("<HH", raw[6:10])
        return {"format": "gif", "mime_type": "image/gif", "width": width, "height": height}
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return {"format": "webp", "mime_type": "image/webp", "width": None, "height": None}
    return {"format": "unknown", "mime_type": "application/octet-stream", "width": None, "height": None}


def _jpeg_size(raw: bytes) -> dict[str, int | None]:
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(raw):
            break
        length = int.from_bytes(raw[index : index + 2], "big")
        if length < 2 or index + length > len(raw):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(raw[index + 3 : index + 5], "big")
            width = int.from_bytes(raw[index + 5 : index + 7], "big")
            return {"width": width, "height": height}
        index += length
    return {"width": None, "height": None}


def _error(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data={},
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": retryable},
    )
