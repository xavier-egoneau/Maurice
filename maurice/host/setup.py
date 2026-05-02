"""First-run setup wizard — writes ~/.maurice/config.yaml."""

from __future__ import annotations

import sys
import os
from pathlib import Path

from maurice.host.project import global_config_path
from maurice.host.model_catalog import chatgpt_model_choices, ollama_model_choices
from maurice.kernel.contracts import ProviderChunk, ProviderChunkType, ProviderStatus


_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def _default_global_workspace() -> Path:
    return Path.home() / "Documents" / "workspace_maurice"


def _color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _color() else text


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    display = f"{prompt} [{default}] " if default else f"{prompt} "
    try:
        if secret:
            import getpass
            answer = getpass.getpass(_c(_BOLD, display))
        else:
            answer = input(_c(_BOLD, display)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise
    return answer.strip() if answer.strip() else default


def _choose(
    prompt: str,
    choices: list[tuple[str, str]],
    default: str,
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    print()
    print(_c(_BOLD, prompt))
    for index, (key, label) in enumerate(choices, start=1):
        marker = _c(_GREEN, "•") if key == default else " "
        print(f"  {marker} {index}. {_c(_BOLD, key):<14} {_c(_DIM, label)}")
    print()
    keys = [k for k, _ in choices]
    while True:
        answer = _ask(f"Choix (1-{len(choices)} ou identifiant)", default).lower()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(choices):
                return choices[index - 1][0]
            print(_c(_YELLOW, f"  → numéro attendu entre 1 et {len(choices)}"))
            continue
        if aliases:
            answer = aliases.get(answer, answer)
        if answer in keys:
            return answer
        print(_c(_YELLOW, f"  → options valides : 1-{len(choices)} ou {', '.join(keys)}"))


def _ask_model_from_choices(prompt: str, choices: list[tuple[str, str]], *, default: str) -> str:
    if not choices:
        return _ask(prompt, default)
    print()
    print(_c(_BOLD, prompt))
    for index, (model_id, label) in enumerate(choices, start=1):
        suffix = f" — {label}" if label and label != model_id else ""
        print(f"  {index:>2}. {_c(_BOLD, model_id)}{_c(_DIM, suffix)}")
    print()
    ids = {model_id for model_id, _label in choices}
    while True:
        answer = _ask(f"Choix du modèle (1-{len(choices)} ou identifiant)", default)
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(choices):
                return choices[index - 1][0]
            print(_c(_YELLOW, f"  → numéro attendu entre 1 et {len(choices)}"))
            continue
        if answer in ids:
            return answer
        return answer


# ---------------------------------------------------------------------------
# provider-specific flows

def _setup_anthropic() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  API Anthropic — génère une clé sur console.anthropic.com\n"))
    model = _ask("  Modèle", "claude-opus-4-5")
    api_key = _ask("  Clé API (sk-ant-...)", secret=True)
    if not api_key:
        raise ValueError("Clé API requise pour Anthropic.")
    config_lines = [
        "provider:",
        "  type: api",
        "  protocol: anthropic",
        f"  model: {model}",
        "  credential: anthropic",
    ]
    creds = {"anthropic": api_key}
    return config_lines, creds


def _setup_openai_api() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  API compatible OpenAI — OpenAI, Mistral, Groq, Together, etc.\n"))
    base_url = _ask("  Base URL", "https://api.openai.com/v1")
    model = _ask("  Modèle", "gpt-4o")
    api_key = _ask("  Clé API", secret=True)
    if not api_key:
        raise ValueError("Clé API requise.")
    config_lines = [
        "provider:",
        "  type: openai",
        f"  model: {model}",
        f"  base_url: {base_url}",
        "  credential: openai",
    ]
    creds = {"openai": api_key}
    return config_lines, creds


def _setup_ollama_local() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  Ollama local ou auto-hebergé — aucune clé requise.\n"))
    base_url = _ask("  URL Ollama", "http://localhost:11434")
    model = _ask_model_from_choices(
        "  Modèle Ollama",
        ollama_model_choices(base_url),
        default="llama3.1",
    )
    config_lines = [
        "provider:",
        "  type: ollama",
        f"  model: {model}",
        f"  base_url: {base_url}",
    ]
    return config_lines, {}


def _setup_ollama_api() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  Ollama Cloud ou endpoint distant — clé API requise.\n"))
    base_url = _ask("  URL Ollama API", "https://ollama.com")
    api_key = _ask("  Clé API Ollama", secret=True)
    if not api_key:
        raise ValueError("Clé API requise pour Ollama API.")
    model = _ask_model_from_choices(
        "  Modèle Ollama",
        ollama_model_choices(base_url, api_key=api_key),
        default="llama3.1",
    )
    config_lines = [
        "provider:",
        "  type: ollama",
        "  protocol: ollama_chat",
        f"  model: {model}",
        f"  base_url: {base_url}",
        "  credential: ollama",
    ]
    return config_lines, {"ollama": api_key}


def _setup_openai_auth(existing_provider: dict[str, object] | None = None) -> tuple[list[str], dict[str, str]]:
    """OAuth PKCE flow — opens the browser, waits for the callback."""
    from maurice.host.auth import ChatGPTAuthFlow
    from maurice.host.credentials import load_credentials, write_credentials, CredentialRecord, credentials_path
    import time
    from datetime import UTC, datetime

    existing_provider = existing_provider or {}
    print(_c(_DIM, "\n  OpenAI / ChatGPT — connexion navigateur via ton abonnement ChatGPT.\n"))

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_credentials(path)
    existing = store.credentials.get("chatgpt")
    if existing and (existing.value or getattr(existing, "refresh_token", "")):
        answer = _choose(
            "Auth ChatGPT déjà configurée.",
            [
                ("keep", "garder l'auth existante"),
                ("reauth", "refaire la connexion navigateur"),
            ],
            default="keep",
            aliases={"garder": "keep", "refaire": "reauth", "reconnecter": "reauth"},
        )
    else:
        answer = "reauth"

    if answer.strip().lower() == "reauth":
        print(_c(_CYAN, "\n  Ouverture du navigateur pour l'authentification…"))
        print(_c(_DIM,  "  Connecte-toi puis reviens ici (timeout 5 min).\n"))

        try:
            token_data = ChatGPTAuthFlow().run()
        except TimeoutError:
            raise RuntimeError("Authentification ChatGPT expirée. Relance le wizard.")

        # Write token directly to ~/.maurice/credentials.yaml (bypass workspace migration)
        store.credentials["chatgpt"] = CredentialRecord(
            type="token",
            value=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            expires=float(token_data.get("expires", time.time())),
            provider="chatgpt_codex",
            obtained_at=token_data.get("obtained_at", datetime.now(UTC).isoformat()),
        )
        write_credentials(path, store)
        print(_c(_GREEN, "  ✓ Token ChatGPT enregistré dans ~/.maurice/credentials.yaml"))
    else:
        print(_c(_GREEN, "  ✓ Auth ChatGPT existante conservée"))

    model = _ask_model_from_choices(
        "  Modèle ChatGPT",
        chatgpt_model_choices(),
        default=str(existing_provider.get("model") or "gpt-5"),
    )

    config_lines = [
        "provider:",
        "  type: auth",
        "  protocol: chatgpt_codex",
        f"  model: {model}",
        "  credential: chatgpt",
    ]
    return config_lines, {}   # credentials already written by save_chatgpt_auth


def _setup_mock() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  Mock — réponses simulées, aucune connexion requise.\n"))
    return ["provider:", "  type: mock"], {}


def _ask_usage_mode(default: str = "local") -> str:
    return _choose(
        "Avec quel niveau de contexte veux-tu démarrer ?",
        [
            ("local", "dossier courant — type assistant de code, `cd projet && maurice`"),
            ("global", "assistant de bureau — permanent, mémoire centrale"),
        ],
        default=default if default in {"local", "global"} else "local",
    )


def _ask_global_workspace(default: str | None = None) -> Path:
    value = _ask("  Workspace global", default or str(_default_global_workspace()))
    return Path(value).expanduser().resolve()


def _provider_choices() -> list[tuple[str, str]]:
    choices = [
        ("openai_api",   "OpenAI-compatible  — API key"),
        ("openai_auth",  "OpenAI / ChatGPT  — connexion navigateur, sans clé API"),
        ("ollama_local", "Ollama  — local ou auto-hebergé, sans clé"),
        ("ollama_api",   "Ollama  — cloud ou endpoint distant, clé API"),
        ("anthropic_api", "Anthropic  — API key"),
    ]
    if os.environ.get("MAURICE_SETUP_SHOW_MOCK") == "1":
        choices.append(("mock", "Mock  — tests/dev uniquement"))
    return choices


def _provider_aliases() -> dict[str, str]:
    return {
        "openai": "openai_api",
        "api": "openai_api",
        "chatgpt": "openai_auth",
        "auth": "openai_auth",
        "ollama": "ollama_local",
        "ollama_cloud": "ollama_api",
        "anthropic": "anthropic_api",
    }


def _provider_default_from_config(config: dict[str, object]) -> str:
    provider = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    kind = provider.get("type") if isinstance(provider, dict) else None
    protocol = provider.get("protocol") if isinstance(provider, dict) else None
    if kind == "auth" and protocol == "chatgpt_codex":
        return "openai_auth"
    if kind == "openai":
        return "openai_api"
    if kind == "ollama":
        return "ollama_api" if provider.get("credential") else "ollama_local"
    if kind in {"api", "anthropic"} and protocol == "anthropic":
        return "anthropic_api"
    if kind == "mock":
        return "mock"
    return "openai_auth"


def _existing_setup_config() -> dict[str, object]:
    from maurice.kernel.config import read_yaml_file

    return read_yaml_file(global_config_path())


# ---------------------------------------------------------------------------
# main wizard

def run_setup() -> bool:
    """Interactive first-run wizard. Returns True if config was written."""
    cfg_path = global_config_path()
    existing_config = _existing_setup_config()
    existing_usage = existing_config.get("usage") if isinstance(existing_config.get("usage"), dict) else {}
    existing_provider = existing_config.get("provider") if isinstance(existing_config.get("provider"), dict) else {}
    existing_profile = str(existing_config.get("permission_profile") or "limited")

    print()
    print(_c(_BOLD, "  Bienvenue dans Maurice"))
    print(_c(_DIM,  "  Configuration initiale — moins d'une minute.\n"))

    usage_mode = _ask_usage_mode(default=str(existing_usage.get("mode") or "local"))
    global_workspace = (
        _ask_global_workspace(str(existing_usage.get("workspace") or ""))
        if usage_mode == "global"
        else None
    )
    provider_choices = _provider_choices()
    provider_default = _provider_default_from_config(existing_config)
    if provider_default not in {key for key, _label in provider_choices}:
        provider_default = "openai_auth"

    provider = _choose(
        "Quel provider LLM ?",
        provider_choices,
        default=provider_default,
        aliases=_provider_aliases(),
    )

    try:
        if provider == "anthropic_api":
            config_lines, creds = _setup_anthropic()
        elif provider == "openai_api":
            config_lines, creds = _setup_openai_api()
        elif provider == "openai_auth":
            config_lines, creds = _setup_openai_auth(
                existing_provider if isinstance(existing_provider, dict) else {}
            )
        elif provider == "ollama_local":
            config_lines, creds = _setup_ollama_local()
        elif provider == "ollama_api":
            config_lines, creds = _setup_ollama_api()
        else:
            config_lines, creds = _setup_mock()
    except (ValueError, RuntimeError) as exc:
        print(_c(_RED, f"\n  ✗ {exc}"), file=sys.stderr)
        return False

    profile = _choose(
        "Profil de permissions ?",
        [
            ("limited", "lecture/écriture projet, shell sur demande  ← recommandé"),
            ("safe",    "lecture libre, tout le reste sur demande"),
            ("power",   "accès étendu, réseau libre"),
        ],
        default=existing_profile if existing_profile in {"limited", "safe", "power"} else "limited",
    )
    config_lines += ["", f"permission_profile: {profile}"]
    config_lines += ["", "usage:", f"  mode: {usage_mode}"]
    if global_workspace is not None:
        config_lines.append(f"  workspace: {global_workspace}")

    # Write ~/.maurice/config.yaml
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    # Write credentials if any (non-chatgpt providers)
    if creds:
        _write_credentials(creds)

    ok, error = _test_provider_connection()
    if not ok:
        print(_c(_RED, f"\n  ✗ Test provider échoué : {error}"), file=sys.stderr)
        print(_c(_DIM, "  Corrige la configuration puis relance `maurice`."), file=sys.stderr)
        return False

    if global_workspace is not None:
        _initialize_global_workspace(global_workspace, profile)
        _configure_global_workspace_provider(global_workspace, config_lines, profile)

    print()
    print(_c(_GREEN, f"  ✓ Config écrite dans {cfg_path}"))
    print(_c(_GREEN, "  ✓ Provider vérifié"))
    if usage_mode == "local":
        print(_c(_DIM, "  Contexte dossier : place-toi dans un dossier puis lance `maurice`."))
        print(_c(_DIM, "  Tu pourras passer à l'assistant de bureau avec `maurice setup`."))
    else:
        print(_c(_GREEN, f"  ✓ Workspace assistant initialisé dans {global_workspace}"))
        print(_c(_DIM, "  Assistant de bureau : lance `maurice start`."))
    print(_c(_DIM,   "  Modifiable à tout moment avec un éditeur de texte.\n"))
    return True


def _write_credentials(creds: dict[str, str]) -> None:
    from maurice.host.credentials import load_credentials, write_credentials, CredentialRecord, credentials_path

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_credentials(path)
    for name, value in creds.items():
        store.credentials[name] = CredentialRecord(type="api_key", value=value)
    write_credentials(path, store)


def _test_provider_connection() -> tuple[bool, str]:
    """Run a tiny provider call using the just-written global config."""
    try:
        from maurice.host.server import _build_provider
        import yaml

        cfg = yaml.safe_load(global_config_path().read_text(encoding="utf-8")) or {}
        provider = _build_provider(cfg, "test")
        chunks = provider.stream(
            messages=[{"role": "user", "content": "test", "metadata": {}}],
            model=str((cfg.get("provider") or {}).get("model") or "mock"),
            tools=[],
            system="You are Maurice. Reply briefly for a setup connectivity check.",
            limits={"max_tokens": 1},
        )
        for chunk in chunks:
            chunk = ProviderChunk.model_validate(chunk)
            if chunk.type == ProviderChunkType.STATUS and chunk.status == ProviderStatus.FAILED:
                message = chunk.error.message if chunk.error is not None else "provider failed"
                return False, message
            if chunk.type in {ProviderChunkType.TEXT_DELTA, ProviderChunkType.USAGE}:
                continue
            if chunk.type == ProviderChunkType.STATUS and chunk.status == ProviderStatus.COMPLETED:
                return True, ""
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _initialize_global_workspace(workspace: Path, permission_profile: str) -> Path:
    from maurice.host.workspace import initialize_workspace

    runtime_root = Path(__file__).resolve().parents[2]
    return initialize_workspace(
        workspace,
        runtime_root,
        permission_profile=permission_profile,  # type: ignore[arg-type]
    )


def _configure_global_workspace_provider(workspace: Path, config_lines: list[str], permission_profile: str) -> None:
    from maurice.host.paths import agents_config_path, kernel_config_path
    from maurice.kernel.config import read_yaml_file, write_yaml_file
    import yaml

    config = yaml.safe_load("\n".join(config_lines) + "\n") or {}
    provider = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    if not isinstance(provider, dict):
        return

    kernel_model = _kernel_model_from_provider(provider)
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    kernel_data.setdefault("kernel", {})["model"] = kernel_model
    kernel_data["kernel"].setdefault("permissions", {})["profile"] = permission_profile
    write_yaml_file(kernel_config_path(workspace), kernel_data)

    credential = kernel_model.get("credential")
    if credential:
        agents_data = read_yaml_file(agents_config_path(workspace))
        main_agent = agents_data.setdefault("agents", {}).setdefault("main", {})
        credentials = list(main_agent.get("credentials") or [])
        if credential not in credentials:
            credentials.append(str(credential))
        main_agent["credentials"] = credentials
        main_agent["permission_profile"] = permission_profile
        write_yaml_file(agents_config_path(workspace), agents_data)


def _kernel_model_from_provider(provider: dict[str, object]) -> dict[str, object]:
    kind = str(provider.get("type") or "mock")
    if kind == "anthropic":
        kind = "api"
    model_name = str(provider.get("model") or ("mock" if kind == "mock" else ""))
    return {
        "provider": kind,
        "protocol": provider.get("protocol"),
        "name": model_name,
        "base_url": provider.get("base_url"),
        "credential": provider.get("credential"),
    }


def needs_setup() -> bool:
    return not global_config_path().exists()
