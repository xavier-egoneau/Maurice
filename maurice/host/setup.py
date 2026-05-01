"""First-run setup wizard — writes ~/.maurice/config.yaml."""

from __future__ import annotations

import sys
from pathlib import Path

from maurice.host.project import global_config_path


_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


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


def _choose(prompt: str, choices: list[tuple[str, str]], default: str) -> str:
    print()
    print(_c(_BOLD, prompt))
    for key, label in choices:
        marker = _c(_GREEN, "•") if key == default else " "
        print(f"  {marker} {_c(_BOLD, key):<14} {_c(_DIM, label)}")
    print()
    keys = [k for k, _ in choices]
    while True:
        answer = _ask(f"Choix ({'/'.join(keys)})", default).lower()
        if answer in keys:
            return answer
        print(_c(_YELLOW, f"  → options valides : {', '.join(keys)}"))


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


def _setup_openai() -> tuple[list[str], dict[str, str]]:
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


def _setup_ollama() -> tuple[list[str], dict[str, str]]:
    print(_c(_DIM, "\n  Ollama — aucune clé requise, modèle local ou distant.\n"))
    base_url = _ask("  URL Ollama", "http://localhost:11434")
    model = _ask("  Modèle", "llama3")
    config_lines = [
        "provider:",
        "  type: ollama",
        f"  model: {model}",
        f"  base_url: {base_url}",
    ]
    return config_lines, {}


def _setup_chatgpt() -> tuple[list[str], dict[str, str]]:
    """OAuth PKCE flow — opens the browser, waits for the callback."""
    from maurice.host.auth import ChatGPTAuthFlow
    from maurice.host.credentials import load_credentials, write_credentials, CredentialRecord, credentials_path
    import time
    from datetime import UTC, datetime

    print(_c(_DIM, "\n  ChatGPT — connexion via ton abonnement ChatGPT (sans clé API).\n"))
    model = _ask("  Modèle Codex", "gpt-4o")

    print(_c(_CYAN, "\n  Ouverture du navigateur pour l'authentification…"))
    print(_c(_DIM,  "  Connecte-toi puis reviens ici (timeout 5 min).\n"))

    try:
        token_data = ChatGPTAuthFlow().run()
    except TimeoutError:
        raise RuntimeError("Authentification ChatGPT expirée. Relance le wizard.")

    # Write token directly to ~/.maurice/credentials.yaml (bypass workspace migration)
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_credentials(path)
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


# ---------------------------------------------------------------------------
# main wizard

def run_setup() -> bool:
    """Interactive first-run wizard. Returns True if config was written."""
    cfg_path = global_config_path()

    print()
    print(_c(_BOLD, "  Bienvenue dans Maurice"))
    print(_c(_DIM,  "  Configuration initiale — moins d'une minute.\n"))

    provider = _choose(
        "Quel provider LLM ?",
        [
            ("anthropic", "API Anthropic  — clé API"),
            ("openai",    "API OpenAI-compatible  — clé API"),
            ("chatgpt",   "ChatGPT  — connexion navigateur, sans clé API"),
            ("ollama",    "Ollama  — modèle local, sans clé"),
            ("mock",      "Mock  — pour tester"),
        ],
        default="anthropic",
    )

    try:
        if provider == "anthropic":
            config_lines, creds = _setup_anthropic()
        elif provider == "openai":
            config_lines, creds = _setup_openai()
        elif provider == "chatgpt":
            config_lines, creds = _setup_chatgpt()
        elif provider == "ollama":
            config_lines, creds = _setup_ollama()
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
        default="limited",
    )
    config_lines += ["", f"permission_profile: {profile}"]

    # Write ~/.maurice/config.yaml
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    # Write credentials if any (non-chatgpt providers)
    if creds:
        _write_credentials(creds)

    print()
    print(_c(_GREEN, f"  ✓ Config écrite dans {cfg_path}"))
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


def needs_setup() -> bool:
    return not global_config_path().exists()
