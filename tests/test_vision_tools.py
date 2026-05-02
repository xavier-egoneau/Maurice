from __future__ import annotations

import base64

from maurice.kernel.permissions import PermissionContext
from maurice.host.vision_backend import ollama_vision_backend
from maurice.system_skills.vision.tools import analyze, inspect


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def context_with_project(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    workspace.mkdir()
    runtime.mkdir()
    project.mkdir()
    return PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        active_project_root=str(project),
    )


def test_vision_inspect_reads_local_image_metadata(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "content" / "pixel.png"
    image.parent.mkdir()
    image.write_bytes(PNG_1X1)

    result = inspect({"path": "pixel.png"}, permission_context)

    assert result.ok is True
    assert result.data["format"] == "png"
    assert result.data["mime_type"] == "image/png"
    assert result.data["width"] == 1
    assert result.data["height"] == 1
    assert result.artifacts[0].type == "image"


def test_vision_relative_path_prefers_active_project(tmp_path) -> None:
    permission_context = context_with_project(tmp_path)
    image = tmp_path / "project" / "pixel.png"
    image.write_bytes(PNG_1X1)

    result = inspect({"path": "pixel.png"}, permission_context)

    assert result.ok is True
    assert result.data["path"] == str(image.resolve())


def test_vision_analyze_requires_backend(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "content" / "pixel.png"
    image.parent.mkdir()
    image.write_bytes(PNG_1X1)

    result = analyze({"path": "pixel.png", "prompt": "Describe it"}, permission_context)

    assert result.ok is False
    assert result.error.code == "backend_unconfigured"


def test_vision_analyze_uses_injected_backend(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "content" / "pixel.png"
    image.parent.mkdir()
    image.write_bytes(PNG_1X1)

    def backend(payload):
        return {
            "summary": f"Image is {payload['image']['width']} pixel wide.",
            "description": "A tiny image.",
        }

    result = analyze(
        {"path": "pixel.png", "prompt": "Describe it"},
        permission_context,
        backend=backend,
    )

    assert result.ok is True
    assert result.trust == "external_untrusted"
    assert result.data["analysis"]["description"] == "A tiny image."
    assert result.data["trust_note"] == "image-derived content is model/backend output"


def test_ollama_vision_backend_posts_image_to_local_model(tmp_path) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(PNG_1X1)
    captured = {}

    def transport(url, payload, headers, timeout_seconds):
        captured.update(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"message": {"content": "A tiny test image."}}

    backend = ollama_vision_backend(
        {"base_url": "http://ollama.local", "model": "gemma4", "timeout_seconds": 7},
        transport=transport,
    )

    result = backend({"prompt": "Decris", "image": {"path": str(image)}})

    assert result["description"] == "A tiny test image."
    assert result["model"] == "gemma4"
    assert captured["url"] == "http://ollama.local/api/chat"
    assert captured["timeout_seconds"] == 7
    assert captured["payload"]["messages"][0]["images"]
