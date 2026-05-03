from __future__ import annotations

from maurice.host.paths import agents_config_path, kernel_config_path, maurice_home, workspace_key
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file


def test_model_schema_migration_moves_inline_models_without_touching_credentials(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    kernel_data["kernel"]["model"] = {
        "provider": "auth",
        "protocol": "chatgpt_codex",
        "name": "gpt-5",
        "base_url": None,
        "credential": "chatgpt",
    }
    write_yaml_file(kernel_config_path(workspace), kernel_data)
    agents_data = read_yaml_file(agents_config_path(workspace))
    agents_data["agents"]["main"]["credentials"] = ["chatgpt"]
    agents_data["agents"]["main"]["model"] = {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "gemma4",
        "base_url": "http://localhost:11434",
        "credential": None,
    }
    write_yaml_file(agents_config_path(workspace), agents_data)

    bundle = load_workspace_config(workspace)
    migrated_kernel = read_yaml_file(kernel_config_path(workspace))["kernel"]
    migrated_agent = read_yaml_file(agents_config_path(workspace))["agents"]["main"]

    assert "model" not in migrated_kernel
    assert "model" not in migrated_agent
    assert migrated_kernel["models"]["default"] == "auth_gpt_5"
    assert migrated_kernel["models"]["entries"]["auth_gpt_5"]["credential"] == "chatgpt"
    assert migrated_agent["model_chain"] == ["ollama_gemma4"]
    assert migrated_agent["credentials"] == ["chatgpt"]
    assert bundle.agents.agents["main"].model_chain == ["ollama_gemma4"]


def test_config_load_recovers_agents_from_orphaned_state_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "workspace_maurice"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    (workspace / "agents" / "num2").mkdir(parents=True)

    orphan_config = maurice_home() / "workspaces" / f"{workspace_key(workspace)}-old" / "config"
    write_yaml_file(
        orphan_config / "host.yaml",
        {
            "host": {
                "runtime_root": str(runtime),
                "workspace_root": str(workspace.resolve()),
                "channels": {
                    "telegram_num2": {
                        "adapter": "telegram",
                        "enabled": True,
                        "agent": "num2",
                        "credential": "telegram_num2",
                    }
                },
            }
        },
    )
    write_yaml_file(
        orphan_config / "kernel.yaml",
        {
            "kernel": {
                "models": {
                    "default": "auth_gpt_5",
                    "entries": {
                        "ollama_minimax": {
                            "provider": "ollama",
                            "protocol": "ollama_chat",
                            "name": "minimax-m2.7",
                            "base_url": "https://ollama.com",
                            "credential": "ollama",
                        }
                    },
                }
            }
        },
    )
    write_yaml_file(
        orphan_config / "agents.yaml",
        {
            "agents": {
                "num2": {
                    "id": "num2",
                    "workspace": str(workspace / "agents" / "num2"),
                    "skills": ["filesystem", "memory"],
                    "credentials": ["ollama"],
                    "permission_profile": "limited",
                    "status": "active",
                    "default": False,
                    "channels": ["telegram"],
                    "model": {
                        "provider": "ollama",
                        "protocol": "ollama_chat",
                        "name": "minimax-m2.7",
                        "base_url": "https://ollama.com",
                        "credential": "ollama",
                    },
                    "event_stream": str(workspace / "agents" / "num2" / "events.jsonl"),
                }
            }
        },
    )

    bundle = load_workspace_config(workspace)
    agents = read_yaml_file(agents_config_path(workspace))["agents"]
    kernel = read_yaml_file(kernel_config_path(workspace))["kernel"]

    assert "num2" in bundle.agents.agents
    assert "model" not in agents["num2"]
    assert agents["num2"]["model_chain"] == ["ollama_minimax_m2_7"]
    assert kernel["models"]["entries"]["ollama_minimax"]["credential"] == "ollama"
    assert kernel["models"]["entries"]["ollama_minimax_m2_7"]["credential"] == "ollama"
    assert bundle.host.channels["telegram_num2"]["agent"] == "num2"
