from __future__ import annotations

import pytest

from maurice.host.credentials import credentials_path, load_workspace_credentials
from maurice.host.paths import agents_config_path, host_config_path, kernel_config_path
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import load_workspace_config


def test_initialize_workspace_creates_expected_shape(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()

    created = initialize_workspace(workspace, runtime, permission_profile="limited")

    assert created == workspace.resolve()
    for relative in (
        "agents/main",
        "agents/main/content",
        "agents/main/memory",
        "agents/main/dreams",
        "agents/main/reminders",
        "skills",
        "sessions",
    ):
        assert (workspace / relative).is_dir()
    assert not (workspace / "content").exists()
    assert not (workspace / "memory").exists()
    assert (workspace / "skills.yaml").is_file()
    assert not (workspace / "config").exists()
    assert host_config_path(workspace).is_file()
    assert kernel_config_path(workspace).is_file()
    assert agents_config_path(workspace).is_file()

    bundle = load_workspace_config(workspace)
    assert bundle.host.runtime_root == str(runtime.resolve())
    assert bundle.host.workspace_root == str(workspace.resolve())
    assert bundle.kernel.permissions.profile == "limited"
    assert bundle.agents.agents["main"].permission_profile == "limited"
    assert credentials_path().is_file()
    assert credentials_path().parent.is_dir()
    assert not (workspace / "credentials.yaml").exists()


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
    credentials = load_workspace_credentials(workspace)

    assert not hasattr(bundle, "credentials")
    assert credentials.credentials["openai"].value == "secret"
    assert credentials_path().is_file()
    assert not (workspace / "credentials.yaml").exists()


def test_openai_chatgpt_configs_upgrade_legacy_context_window(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    (workspace / "config").mkdir(exist_ok=True)
    (workspace / "config" / "kernel.yaml").write_text(
        """
kernel:
  model:
    provider: auth
    protocol: chatgpt_codex
    name: gpt-5
    credential: chatgpt
  sessions:
    context_window_tokens: 100000
""",
        encoding="utf-8",
    )

    bundle = load_workspace_config(workspace)

    assert bundle.kernel.sessions.context_window_tokens == 250_000
