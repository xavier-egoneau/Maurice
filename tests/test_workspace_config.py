from __future__ import annotations

import pytest

from maurice.host.credentials import load_credentials
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import load_workspace_config


def test_initialize_workspace_creates_expected_shape(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()

    created = initialize_workspace(workspace, runtime, permission_profile="limited")

    assert created == workspace.resolve()
    for relative in ("agents/main", "skills", "sessions", "artifacts", "config"):
        assert (workspace / relative).is_dir()

    bundle = load_workspace_config(workspace)
    assert bundle.host.runtime_root == str(runtime.resolve())
    assert bundle.host.workspace_root == str(workspace.resolve())
    assert bundle.kernel.permissions.profile == "limited"
    assert bundle.agents.agents["main"].permission_profile == "limited"


def test_initialize_workspace_rejects_runtime_workspace_collision(tmp_path) -> None:
    with pytest.raises(ValueError):
        initialize_workspace(tmp_path, tmp_path)


def test_credentials_are_loaded_separately_from_config(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)

    (workspace / "credentials.yaml").write_text(
        "credentials:\n  openai:\n    type: api_key\n    value: secret\n",
        encoding="utf-8",
    )

    bundle = load_workspace_config(workspace)
    credentials = load_credentials(workspace / "credentials.yaml")

    assert not hasattr(bundle, "credentials")
    assert credentials.credentials["openai"].value == "secret"
