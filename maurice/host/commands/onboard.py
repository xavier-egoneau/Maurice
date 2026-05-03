"""Auto-split from cli.py."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agent_wizard import clear_agent_creation_wizard, handle_agent_creation_wizard
from maurice.host.agents import archive_agent, create_agent, delete_agent, disable_agent, list_agents, update_agent
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME, ChatGPTAuthFlow, clear_chatgpt_auth,
    get_valid_chatgpt_access_token, load_chatgpt_auth, save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.command_registry import CommandRegistry, default_command_registry
from maurice.host.credentials import (
    CredentialRecord, CredentialsStore, credentials_path,
    ensure_workspace_credentials_migrated, load_workspace_credentials, write_workspace_credentials,
)
from maurice.host.dashboard import build_dashboard_snapshot
from maurice.host.delivery import (
    _schedule_reminder_callback, _deliver_reminder_result, _build_daily_digest,
    _deliver_daily_digest, _emit_daily_event, _cancel_job_callback,
    _latest_dream_report, _human_datetime,
)
from maurice.host.gateway import GatewayHttpServer, MessageRouter
from maurice.host.migration import inspect_jarvis_workspace, migrate_jarvis_workspace
from maurice.host.model_catalog import chatgpt_model_choices, format_bytes, ollama_model_choices
from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.output import (
    _yes_no, _status_marker, _short, _ansi_padding, _compact_text,
    _supports_color, _color, _print_title, _print_dim,
)
from maurice.host.paths import (
    agents_config_path, ensure_workspace_config_migrated, host_config_path,
    kernel_config_path, maurice_home, workspace_skills_config_path,
)
from maurice.host.runtime import (
    run_one_turn, _resolve_agent, _agent_system_prompt, _active_dev_project_path,
    _provider_for_config, _effective_model_config, _model_credential,
    _effective_model_label, _default_agent,
)
from maurice.host.secret_capture import capture_pending_secret
from maurice.host.self_update import (
    apply_runtime_proposal, list_runtime_proposals, run_proposal_tests, validate_runtime_proposal,
)
from maurice.host.service import check_install, inspect_service_status, read_service_logs
from maurice.host.telegram import (
    _credential_value, _telegram_channel_configured, _telegram_channel_configs,
    _telegram_channel_for_agent, _telegram_offset_path, _validate_telegram_first_message,
    _telegram_get_updates, _telegram_bot_username, _telegram_send_message,
    _telegram_send_chat_action, _telegram_api_json, _telegram_update_to_inbound,
    _int_list, _read_int_file, _write_int_file, _redact_secret,
    _telegram_sender_ids, _telegram_start_chat_action,
)
from maurice.host.workspace import ensure_workspace_content_migrated, initialize_workspace
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import (
    ConfigBundle,
    default_model_config,
    load_workspace_config,
    model_profile_id,
    model_profile_payload,
    read_yaml_file,
    write_yaml_file,
)
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider, ChatGPTCodexProvider, MockProvider,
    OllamaCompatibleProvider, OpenAICompatibleProvider, UnsupportedProvider,
)
from maurice.kernel.runs import RunApprovalStore, RunCoordinationStore, RunExecutor, RunStore
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.system_skills.reminders.tools import fire_reminder

PROVIDER_HELP = {
    "chatgpt": ("ChatGPT", "connexion via ton abonnement ChatGPT, sans cle API OpenAI"),
    "openai_api": ("API compatible OpenAI", "URL + cle API, pour OpenAI ou un provider compatible"),
    "ollama": ("Ollama", "modele local ou serveur Ollama"),
}
DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_WORKSPACE = Path.home() / "Documents" / "workspace_maurice"




def _onboarding_existing_values(workspace_root: Path) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    ensure_workspace_config_migrated(workspace)
    host_data = read_yaml_file(host_config_path(workspace))
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    agents_data = read_yaml_file(agents_config_path(workspace))
    skills_data = read_yaml_file(workspace_skills_config_path(workspace))

    kernel = kernel_data.get("kernel") if isinstance(kernel_data.get("kernel"), dict) else {}
    host = host_data.get("host") if isinstance(host_data.get("host"), dict) else {}
    gateway = host.get("gateway") if isinstance(host.get("gateway"), dict) else {}
    permissions = kernel.get("permissions") if isinstance(kernel.get("permissions"), dict) else {}
    model = kernel.get("model") if isinstance(kernel.get("model"), dict) else _raw_default_model(kernel)
    skills = skills_data.get("skills") if isinstance(skills_data.get("skills"), dict) else {}
    web_skill = skills.get("web") if isinstance(skills.get("web"), dict) else {}
    channels = host.get("channels") if isinstance(host.get("channels"), dict) else {}
    telegram = channels.get("telegram") if isinstance(channels.get("telegram"), dict) else {}
    provider = _onboarding_provider_choice(model)

    existing: dict[str, Any] = {
        "host_data": host_data,
        "kernel_data": kernel_data,
        "agents_data": agents_data,
        "skills_data": skills_data,
        "provider": provider,
    }
    if permissions.get("profile"):
        existing["profile"] = permissions["profile"]
    if gateway.get("port"):
        existing["gateway_port"] = gateway["port"]
    if web_skill.get("base_url"):
        existing["searxng_url"] = web_skill["base_url"]
    if model.get("name"):
        existing["model"] = model["name"]
    if model.get("base_url"):
        existing["base_url"] = model["base_url"]
    if model.get("credential"):
        existing["credential"] = model["credential"]
    if telegram:
        existing["telegram_config"] = telegram
        existing["telegram_credential"] = telegram.get("credential") or "telegram_bot"
        existing["telegram_agent"] = telegram.get("agent") or "main"
        existing["telegram_allowed_users"] = telegram.get("allowed_users") or []
        existing["telegram_allowed_chats"] = telegram.get("allowed_chats") or []
    return existing



def _onboarding_provider_choice(model: dict[str, Any]) -> str:
    provider = model.get("provider")
    protocol = model.get("protocol")
    if provider == "auth" and protocol == "chatgpt_codex":
        return "chatgpt"
    if provider == "api" and protocol == "openai_chat_completions":
        return "openai_api"
    if provider == "ollama" or (provider == "api" and protocol == "ollama_chat"):
        return "ollama"
    return "mock"


def _raw_default_model(kernel: dict[str, Any]) -> dict[str, Any]:
    models = kernel.get("models") if isinstance(kernel.get("models"), dict) else {}
    entries = models.get("entries") if isinstance(models.get("entries"), dict) else {}
    default_id = models.get("default")
    if isinstance(default_id, str) and isinstance(entries.get(default_id), dict):
        return entries[default_id]
    for entry in entries.values():
        if isinstance(entry, dict):
            return entry
    return {}



def _model_existing_values(model: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {"provider": _onboarding_provider_choice(model)}
    if model.get("name"):
        existing["model"] = model["name"]
    if model.get("base_url"):
        existing["base_url"] = model["base_url"]
    if model.get("credential"):
        existing["credential"] = model["credential"]
    return existing



def _ask_model_config(
    workspace: Path,
    *,
    existing: dict[str, object],
    provider: str | None = None,
) -> tuple[dict[str, object], str | None, str]:
    provider = provider or _ask_provider_choice(default=_real_provider_default(existing.get("provider")))
    credential_to_allow: str | None = None
    kernel_model: dict[str, object] = {
        "provider": "mock",
        "protocol": None,
        "name": "mock",
        "base_url": None,
        "credential": None,
    }
    if provider == "chatgpt":
        model = _ask_model_from_choices(
            "Modele ChatGPT",
            _chatgpt_model_choices(),
            default=str(existing.get("model") or "gpt-5"),
        )
        kernel_model = {
            "provider": "auth",
            "protocol": "chatgpt_codex",
            "name": model,
            "base_url": None,
            "credential": CHATGPT_CREDENTIAL_NAME,
        }
        credential_to_allow = CHATGPT_CREDENTIAL_NAME
    elif provider == "openai_api":
        credential_name = _ask("Credential name", default=str(existing.get("credential") or "openai"))
        model = _ask("Model name", default=str(existing.get("model") or "gpt-4o-mini"))
        base_url = _ask("API base URL", default=str(existing.get("base_url") or "https://api.openai.com/v1"))
        key_hint = "configured; press Enter to keep" if _credential_exists(workspace, credential_name) else "leave empty to configure later"
        api_key = _ask_api_key(f"API key ({key_hint})")
        kernel_model = {
            "provider": "api",
            "protocol": "openai_chat_completions",
            "name": model,
            "base_url": base_url,
            "credential": credential_name,
        }
        credential_to_allow = credential_name
        if api_key:
            _save_api_credential(workspace, credential_name, api_key, base_url)
    elif provider == "ollama":
        deployment = _ask_ollama_deployment(existing)
        credential_name = None
        api_key = ""
        if deployment == "cloud":
            base_url = _ask("Ollama Cloud URL", default=str(existing.get("base_url") or "https://ollama.com"))
            credential_name = _ask("Ollama credential name", default=str(existing.get("credential") or "ollama"))
            key_hint = "configured; press Enter to keep" if _credential_exists(workspace, credential_name) else "required for Ollama Cloud; leave empty to configure later"
            api_key = _ask_api_key(f"Ollama API key ({key_hint})")
            if api_key:
                _save_api_credential(workspace, credential_name, api_key, base_url)
            else:
                api_key = _credential_value(workspace, credential_name)
        else:
            base_url = _ask("Ollama URL auto-heberge", default=str(existing.get("base_url") or "http://localhost:11434"))
        model = _ask_model_from_choices(
            "Modele Ollama",
            _ollama_model_choices(base_url, api_key=api_key),
            default=str(existing.get("model") or "llama3.1"),
        )
        kernel_model = {
            "provider": "ollama",
            "protocol": "ollama_chat",
            "name": model,
            "base_url": base_url,
            "credential": credential_name,
        }
        credential_to_allow = credential_name
    return kernel_model, credential_to_allow, provider



def _onboard_agent(workspace_root: Path, *, agent_id: str) -> None:
    workspace = workspace_root.expanduser().resolve()
    bundle = load_workspace_config(workspace)
    print(f"Maurice agent onboarding: {workspace}")
    if not agent_id:
        agent_id = _ask("Agent id", default="")
    if not agent_id:
        raise SystemExit("Agent id is required.")
    profile = _ask_choice(
        "Permission profile",
        ["limited", "safe", "power"],
        default=bundle.kernel.permissions.profile,
    )
    skills = _ask_csv_strings(
        "Skills enabled",
        default=",".join(bundle.kernel.skills),
    )
    credentials = _ask_csv_strings(
        "Credentials allowed (comma-separated, empty = none)",
        default="",
    )
    channels = _ask_csv_strings(
        "Channels bound (comma-separated, empty = none)",
        default="",
    )
    make_default = _ask_yes_no("Make this the default agent", default=False)
    try:
        agent = create_agent(
            workspace,
            agent_id=agent_id,
            permission_profile=profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=make_default,
        )
    except PermissionError as exc:
        if not _ask_yes_no(f"{exc} Confirm permission elevation", default=False):
            raise SystemExit(str(exc)) from exc
        agent = create_agent(
            workspace,
            agent_id=agent_id,
            permission_profile=profile,
            skills=skills,
            credentials=credentials,
            channels=channels,
            make_default=make_default,
            confirmed_permission_elevation=True,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Agent onboarded: {agent.id} ({agent.permission_profile})")



def _onboard_agent_model(workspace_root: Path, *, agent_id: str) -> None:
    workspace = workspace_root.expanduser().resolve()
    bundle = load_workspace_config(workspace)
    if not agent_id:
        agent_id = _ask("Agent id", default="")
    if not agent_id:
        raise SystemExit("Agent id is required.")
    if agent_id not in bundle.agents.agents:
        raise SystemExit(f"Unknown agent: {agent_id}")
    agent = bundle.agents.agents[agent_id]
    existing = _model_existing_values(_agent_existing_model_config(bundle, agent))
    print(f"Maurice model onboarding: {agent_id}")
    print("")
    _print_dim("Appuie sur Entree pour conserver la valeur proposee.")
    kernel_model, credential_to_allow, provider = _ask_model_config(workspace, existing=existing)
    credentials = list(agent.credentials or [])
    if credential_to_allow and credential_to_allow not in credentials:
        credentials.append(credential_to_allow)
    if agent.default:
        _write_kernel_model(workspace, kernel_model)
        update_agent(workspace, agent_id=agent_id, credentials=credentials, model_chain=[])
    else:
        profile_id = _upsert_model_profile(workspace, kernel_model)
        update_agent(
            workspace,
            agent_id=agent_id,
            credentials=credentials,
            model_chain=[profile_id],
        )
    if provider == "chatgpt" and _ask_yes_no("Connect ChatGPT now?", default=False):
        _auth_login("chatgpt", workspace)
    print("")
    print(f"Agent model updated: {agent_id}")
    print(f"Next: maurice run --agent {agent_id} --message \"salut Maurice\"")



def _write_kernel_model(workspace: Path, kernel_model: dict[str, object]) -> None:
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel = kernel_data.setdefault("kernel", {})
    _register_model_profile(kernel, kernel_model, make_default=True)
    write_yaml_file(kernel_path, kernel_data)


def _upsert_model_profile(workspace: Path, model: dict[str, object]) -> str:
    kernel_path = kernel_config_path(workspace)
    kernel_data = read_yaml_file(kernel_path)
    kernel = kernel_data.setdefault("kernel", {})
    profile_id = _register_model_profile(kernel, model, make_default=False)
    write_yaml_file(kernel_path, kernel_data)
    return profile_id


def _register_model_profile(kernel: dict[str, Any], model: dict[str, object], *, make_default: bool) -> str:
    payload = model_profile_payload(dict(model))
    profile_id = model_profile_id(payload)
    models = kernel.setdefault("models", {})
    entries = models.setdefault("entries", {})
    entries[profile_id] = payload
    if make_default:
        models["default"] = profile_id
    return profile_id


def _agent_existing_model_config(bundle: ConfigBundle, agent: Any) -> dict[str, Any]:
    for model_id in agent.model_chain:
        profile = bundle.kernel.models.entries.get(model_id)
        if profile is not None:
            return profile.model_dump(mode="json")
    default_profile = bundle.kernel.models.entries.get(bundle.kernel.models.default)
    if default_profile is not None:
        return default_profile.model_dump(mode="json")
    return default_model_config(bundle)



def _onboard_interactive(workspace_root: Path, *, existing: dict[str, object] | None = None) -> None:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    existing = existing or {}
    print(f"Maurice workspace initialized: {workspace}")
    print("")
    _print_title("Maurice - Onboarding")
    _print_dim("Appuie sur Entree pour conserver la valeur proposee.")

    profile = _ask_choice(
        "Permission profile",
        ["limited", "safe", "power"],
        default=str(existing.get("profile") or bundle.kernel.permissions.profile),
    )
    provider = _ask_provider_choice(default=_real_provider_default(existing.get("provider")))
    gateway_port = _ask_int("Gateway port", default=int(existing.get("gateway_port") or bundle.host.gateway.port))
    search_config = _existing_search_config(existing)
    telegram_config = _ask_telegram_config(workspace, existing=existing)

    kernel_model, credential_to_allow, provider = _ask_model_config(
        workspace,
        existing=existing,
        provider=provider,
    )

    _write_onboarding_config(
        workspace,
        profile=profile,
        gateway_port=gateway_port,
        kernel_model=kernel_model,
        credential_to_allow=credential_to_allow,
        search_config=search_config,
        telegram_config=telegram_config,
        existing=existing,
    )

    if provider == "chatgpt" and _ask_yes_no("Connect ChatGPT now?", default=False):
        _auth_login("chatgpt", workspace)

    print("")
    print("Maurice onboarding complete.")
    print(f"Workspace: {workspace}")
    provider_label = PROVIDER_HELP[provider][0]
    print(f"Provider: {provider_label} ({kernel_model['provider']} {kernel_model.get('protocol') or ''})".strip())
    print(f"Gateway: http://127.0.0.1:{gateway_port}")
    print("")
    print("Next:")
    print(f"  maurice doctor --workspace {workspace}")
    print(f"  maurice run --workspace {workspace} --message \"salut Maurice\"")



def _write_onboarding_config(
    workspace: Path,
    *,
    profile: str,
    gateway_port: int,
    kernel_model: dict[str, object],
    credential_to_allow: str | None,
    search_config: dict[str, object] | None,
    telegram_config: dict[str, object] | None,
    existing: dict[str, object] | None = None,
) -> None:
    existing = existing or {}
    ensure_workspace_config_migrated(workspace)
    fresh_host_data = read_yaml_file(host_config_path(workspace))
    host_data = _existing_config(existing, "host_data") or fresh_host_data
    fresh_host = fresh_host_data.get("host") if isinstance(fresh_host_data.get("host"), dict) else {}
    host = host_data.setdefault("host", {})
    for key in ("runtime_root", "workspace_root", "skill_roots"):
        if key in fresh_host:
            host[key] = fresh_host[key]
    host_data.setdefault("host", {}).setdefault("gateway", {})["port"] = gateway_port
    channels = host_data.setdefault("host", {}).setdefault("channels", {})
    if telegram_config:
        channels["telegram"] = telegram_config
    else:
        channels.pop("telegram", None)
    write_yaml_file(host_config_path(workspace), host_data)

    kernel_data = _existing_config(existing, "kernel_data") or read_yaml_file(kernel_config_path(workspace))
    kernel = kernel_data.setdefault("kernel", {})
    _register_model_profile(kernel, kernel_model, make_default=True)
    kernel.setdefault("permissions", {})["profile"] = profile
    write_yaml_file(kernel_config_path(workspace), kernel_data)

    fresh_agents_data = read_yaml_file(agents_config_path(workspace))
    agents_data = _existing_config(existing, "agents_data") or fresh_agents_data
    main_agent = agents_data.setdefault("agents", {}).setdefault("main", {})
    fresh_main = (fresh_agents_data.get("agents") or {}).get("main") if isinstance(fresh_agents_data.get("agents"), dict) else {}
    if isinstance(fresh_main, dict):
        for key in ("workspace", "event_stream"):
            if key in fresh_main:
                main_agent[key] = fresh_main[key]
    main_agent["permission_profile"] = profile
    if credential_to_allow:
        credentials = list(main_agent.get("credentials") or [])
        if credential_to_allow not in credentials:
            credentials.append(credential_to_allow)
        main_agent["credentials"] = credentials
    channels = list(main_agent.get("channels") or [])
    if telegram_config and "telegram" not in channels:
        channels.append("telegram")
    if not telegram_config:
        channels = [channel for channel in channels if channel != "telegram"]
    main_agent["channels"] = channels
    write_yaml_file(agents_config_path(workspace), agents_data)

    skills_data = _existing_config(existing, "skills_data") or read_yaml_file(workspace_skills_config_path(workspace))
    web_config = skills_data.setdefault("skills", {}).setdefault("web", {})
    if search_config:
        web_config["search_provider"] = search_config["provider"]
        web_config["base_url"] = search_config["base_url"]
    else:
        web_config.pop("search_provider", None)
        web_config.pop("base_url", None)
    write_yaml_file(workspace_skills_config_path(workspace), skills_data)



def _existing_config(existing: dict[str, object], key: str) -> dict[str, Any]:
    value = existing.get(key)
    return value if isinstance(value, dict) else {}



def _existing_search_config(existing: dict[str, object]) -> dict[str, object] | None:
    searxng_url = existing.get("searxng_url")
    if isinstance(searxng_url, str) and searxng_url:
        return {"provider": "searxng", "base_url": searxng_url}
    return {"provider": "searxng", "base_url": DEFAULT_SEARXNG_URL}



def _ask_telegram_config(workspace: Path, *, existing: dict[str, object]) -> dict[str, object] | None:
    current = existing.get("telegram_config")
    default_enabled = isinstance(current, dict)
    print("")
    print(_color("Bot Telegram", "1"))
    print("Maurice peut pre-configurer ton bot Telegram maintenant.")
    print("Dans Telegram, parle a @BotFather, cree un bot avec /newbot, puis colle ici le token qu'il te donne.")
    print("Pour trouver ton id Telegram, tu peux envoyer /start a @userinfobot ou @RawDataBot.")
    print("Tu peux mettre plusieurs ids autorises, separes par des virgules.")
    _print_dim("Pour les groupes, on securise d'abord par l'id de l'utilisateur qui parle au bot.")
    if not _ask_yes_no("Configurer un bot Telegram", default=default_enabled):
        return None

    credential_name = str(existing.get("telegram_credential") or "telegram_bot")
    token_configured = _credential_exists(workspace, credential_name)
    token_hint = "deja configure, Entree pour garder" if token_configured else "coller le token BotFather, ou Entree pour le configurer plus tard"
    token = _ask_secret(f"Token du bot Telegram ({token_hint})", default="")
    if token:
        _save_token_credential(
            workspace,
            credential_name,
            token,
            provider="telegram_bot",
        )

    allowed_users = _ask_csv_ints(
        "Id(s) Telegram autorises (plusieurs ids separes par virgule)",
        default=_csv_default(existing.get("telegram_allowed_users")),
    )
    effective_token = token or _credential_value(workspace, credential_name)
    if effective_token and allowed_users and _ask_yes_no("Envoyer un premier message au bot pour valider la config maintenant", default=True):
        _validate_telegram_first_message(effective_token, allowed_users)
    return {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": credential_name,
        "allowed_users": allowed_users,
        "allowed_chats": [],
        "status": "configured_pending_adapter",
    }



def _csv_default(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return ",".join(str(item) for item in value)



def _ask_csv_ints(prompt: str, *, default: str) -> list[int]:
    while True:
        value = _ask(prompt, default=default)
        if not value.strip():
            return []
        try:
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        except ValueError:
            print("Entre des nombres separes par des virgules.")



def _ask_csv_strings(prompt: str, *, default: str) -> list[str]:
    value = _ask(prompt, default=default)
    return [item.strip() for item in value.split(",") if item.strip()]



def _save_api_credential(workspace: Path, name: str, api_key: str, base_url: str) -> None:
    store = load_workspace_credentials(workspace)
    store.credentials[name] = CredentialRecord(type="api_key", value=api_key, base_url=base_url)
    write_workspace_credentials(workspace, store)



def _save_token_credential(workspace: Path, name: str, token: str, *, provider: str) -> None:
    store = load_workspace_credentials(workspace)
    store.credentials[name] = CredentialRecord(type="token", value=token, provider=provider)
    write_workspace_credentials(workspace, store)



def _credential_exists(workspace: Path, name: str) -> bool:
    return name in load_workspace_credentials(workspace).credentials



def _ask(prompt: str, *, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default



def _ask_secret(prompt: str, *, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    if sys.stdin.isatty():
        value = getpass.getpass(f"{prompt}{suffix}: ").strip()
    else:
        value = input(f"{prompt}{suffix}: ").strip()
    return value or default



def _ask_api_key(prompt: str) -> str:
    _print_dim("La cle ne s'affiche pas pendant la saisie. Dans un terminal Linux, colle avec Ctrl+Shift+V ou clic droit > Coller.")
    while True:
        value = _ask_secret(prompt, default="").strip()
        if value == "^":
            print("Collage non pris en compte. Essaie Ctrl+Shift+V ou clic droit > Coller.")
            continue
        return value



def _ask_int(prompt: str, *, default: int) -> int:
    while True:
        value = _ask(prompt, default=str(default))
        try:
            return int(value)
        except ValueError:
            print("Please enter a number.")



def _ask_choice(prompt: str, choices: list[str], *, default: str) -> str:
    choice_text = "/".join(choices)
    while True:
        value = _ask(f"{prompt} ({choice_text})", default=default)
        if value in choices:
            return value
        print(f"Choose one of: {choice_text}")



def _ask_provider_choice(*, default: str) -> str:
    default = _real_provider_default(default)
    choices = list(PROVIDER_HELP)
    print("")
    print(_color("Provider LLM", "1"))
    for index, choice in enumerate(choices, start=1):
        label, description = PROVIDER_HELP[choice]
        marker = _color(choice, "36")
        print(f"  {index}. {marker} - {label}: {description}")
    print("")
    while True:
        value = _ask(f"Choix du provider (1-{len(choices)} ou identifiant)", default=default)
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        aliases = {
            "openai": "openai_api",
            "api": "openai_api",
        }
        value = aliases.get(value, value)
        if value in choices:
            return value
        print(f"Choisis un provider valide: {', '.join(choices)}")



def _ask_ollama_deployment(existing: dict[str, object]) -> str:
    base_url = str(existing.get("base_url") or "")
    credential = str(existing.get("credential") or "")
    default = "cloud" if credential or (base_url and not _is_local_ollama_url(base_url)) else "auto_heberge"
    print("")
    print(_color("Hebergement Ollama", "1"))
    print(f"  1. {_color('auto_heberge', '36')} - Ollama sur ta machine, ton reseau, ou ton serveur")
    print(f"  2. {_color('cloud', '36')} - Ollama Cloud ou endpoint distant avec cle API")
    print("")
    return _ask_choice("Mode Ollama", ["auto_heberge", "cloud"], default=default)



def _ask_model_from_choices(prompt: str, choices: list[tuple[str, str]], *, default: str) -> str:
    choices_by_id = {model_id: label for model_id, label in choices}
    if choices:
        print("")
        print(_color(prompt, "1"))
        for index, (model_id, label) in enumerate(choices, start=1):
            marker = _color(model_id, "36")
            suffix = f" - {label}" if label and label != model_id else ""
            print(f"  {index}. {marker}{suffix}")
        print("")
        while True:
            value = _ask(f"Choix du modele (1-{len(choices)} ou identifiant)", default=default)
            if value.isdigit():
                index = int(value)
                if 1 <= index <= len(choices):
                    return choices[index - 1][0]
                print(f"Choisis un numero entre 1 et {len(choices)}, ou entre un identifiant manuel.")
                continue
            if value in choices_by_id:
                return value
            return value
    return _ask(prompt, default=default)



def _chatgpt_model_choices() -> list[tuple[str, str]]:
    return chatgpt_model_choices()



def _ollama_model_choices(base_url: str, *, api_key: str = "") -> list[tuple[str, str]]:
    return ollama_model_choices(base_url, api_key=api_key)



def _is_local_ollama_url(base_url: str) -> bool:
    value = base_url.lower().strip().rstrip("/")
    return (
        value.startswith("http://localhost")
        or value.startswith("http://127.0.0.1")
        or value.startswith("http://0.0.0.0")
    )



def _format_bytes(value: object) -> str:
    return format_bytes(value)



def _real_provider_default(provider: object) -> str:
    value = str(provider or "").strip()
    if value in PROVIDER_HELP:
        return value
    return "chatgpt"



def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "o", "oui"}:
            return True
        if value in {"n", "no", "non"}:
            return False
        print("Please answer yes or no.")
