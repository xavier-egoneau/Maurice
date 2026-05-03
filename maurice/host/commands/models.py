"""Model profile management commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maurice.host.agents import update_agent
from maurice.host.paths import kernel_config_path
from maurice.kernel.config import (
    load_workspace_config,
    model_profile_id,
    model_profile_payload,
    read_yaml_file,
    write_yaml_file,
)


def _models_list(workspace_root: Path) -> None:
    bundle = load_workspace_config(workspace_root)
    entries = bundle.kernel.models.entries
    if not entries:
        print("No model profiles.")
        return
    for profile_id, model in sorted(entries.items()):
        marker = "default" if profile_id == bundle.kernel.models.default else "-"
        capabilities = ",".join(model.capabilities)
        print(
            f"{profile_id} {marker} provider={model.provider} model={model.name} "
            f"tier={model.tier or '-'} privacy={model.privacy} capabilities={capabilities or '-'}"
        )


def _models_add(
    workspace_root: Path,
    *,
    profile_id: str | None,
    provider: str,
    protocol: str | None,
    name: str,
    base_url: str | None,
    credential: str | None,
    tier: str | None,
    capabilities: list[str] | None,
    privacy: str | None,
    make_default: bool,
) -> None:
    model = {
        "provider": provider,
        "protocol": protocol,
        "name": name,
        "base_url": base_url,
        "credential": credential,
        "tier": tier,
        "capabilities": capabilities or [],
        "privacy": privacy,
    }
    model_id = _write_model_profile(workspace_root, model, profile_id=profile_id, make_default=make_default)
    print(f"Model profile added: {model_id}")


def _models_default(workspace_root: Path, *, profile_id: str) -> None:
    bundle = load_workspace_config(workspace_root)
    if profile_id not in bundle.kernel.models.entries:
        raise SystemExit(f"Unknown model profile: {profile_id}")
    model = bundle.kernel.models.entries[profile_id].model_dump(mode="json")
    _write_model_profile(workspace_root, model, profile_id=profile_id, make_default=True)
    print(f"Default model profile: {profile_id}")


def _models_assign(workspace_root: Path, *, agent_id: str, model_chain: list[str]) -> None:
    bundle = load_workspace_config(workspace_root)
    missing = [profile_id for profile_id in model_chain if profile_id not in bundle.kernel.models.entries]
    if missing:
        raise SystemExit(f"Unknown model profile(s): {', '.join(missing)}")
    try:
        agent = update_agent(
            workspace_root,
            agent_id=agent_id,
            model_chain=model_chain,
        )
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent model chain updated: {agent.id} -> {', '.join(model_chain)}")


def _write_model_profile(
    workspace_root: Path,
    model: dict[str, Any],
    *,
    profile_id: str | None,
    make_default: bool,
) -> str:
    workspace = Path(workspace_root).expanduser().resolve()
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel = kernel_data.setdefault("kernel", {})
    payload = model_profile_payload(model)
    model_id = profile_id or model_profile_id(payload)
    models = kernel.setdefault("models", {})
    entries = models.setdefault("entries", {})
    entries[model_id] = payload
    if make_default:
        models["default"] = model_id
    write_yaml_file(kernel_path, kernel_data)
    return model_id
