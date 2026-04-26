from __future__ import annotations

import base64

from maurice.kernel.permissions import PermissionContext
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


def test_vision_inspect_reads_local_image_metadata(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "pixel.png"
    image.write_bytes(PNG_1X1)

    result = inspect({"path": "pixel.png"}, permission_context)

    assert result.ok is True
    assert result.data["format"] == "png"
    assert result.data["mime_type"] == "image/png"
    assert result.data["width"] == 1
    assert result.data["height"] == 1
    assert result.artifacts[0].type == "image"


def test_vision_analyze_requires_backend(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "pixel.png"
    image.write_bytes(PNG_1X1)

    result = analyze({"path": "pixel.png", "prompt": "Describe it"}, permission_context)

    assert result.ok is False
    assert result.error.code == "backend_unconfigured"


def test_vision_analyze_uses_injected_backend(tmp_path) -> None:
    permission_context = context(tmp_path)
    image = tmp_path / "workspace" / "pixel.png"
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
