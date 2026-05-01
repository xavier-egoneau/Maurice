"""Interactive REPL for Maurice CLI mode."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from maurice.host.client import MauriceClient
from maurice.host.project import ensure_maurice_dir, resolve_project_root


_THEME = Theme({
    "prompt":         "bold white",
    "dim":            "dim",
    "tool.running":   "dim cyan",
    "tool.ok":        "dim green",
    "tool.err":       "dim red",
    "approval":       "yellow",
    "approval.label": "bold yellow",
    "error":          "bold red",
    "header":         "bold dim white",
    "logo":           "bold bright_white",
    "info.key":       "dim",
    "info.val":       "white",
    "cmd.name":       "bold cyan",
    "cmd.desc":       "dim",
})

_console = Console(theme=_THEME, highlight=False)



_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "filesystem.read":   ("📖", "Lecture"),
    "filesystem.write":  ("✏️ ", "Écriture"),
    "filesystem.list":   ("📂", "Liste"),
    "filesystem.mkdir":  ("📁", "Création dossier"),
    "filesystem.move":   ("📦", "Déplacement"),
    "memory.remember":   ("🧠", "Mémorisation"),
    "memory.search":     ("🔍", "Recherche mémoire"),
    "memory.forget":     ("🗑️ ", "Oubli"),
    "web.fetch":         ("🌐", "Fetch"),
    "web.search":        ("🔎", "Recherche web"),
    "host.status":       ("📊", "État host"),
}


def _tool_label(tool_name: str, arguments: dict) -> str:
    icon, verb = _TOOL_LABELS.get(tool_name, ("🔧", tool_name))
    detail = (
        arguments.get("path")
        or arguments.get("url")
        or arguments.get("query")
        or arguments.get("command", "")
    )
    if detail:
        short = os.path.basename(str(detail)) if "/" in str(detail) else str(detail)
        short = short[:60]
        return f"{icon} {verb}  {short}"
    return f"{icon} {verb}…"


def _approval_prompt(tool: str, arguments: dict, permission_class: str, reason: str) -> bool:
    args_str = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:3])
    _console.print()
    _console.print(f"  [approval.label]Permission requise[/] [approval]{tool}({args_str})[/]")
    _console.print(f"  [dim]{permission_class}  ·  {reason}[/]")
    try:
        answer = _console.input("  [approval]Autoriser ?[/] [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes", "o", "oui"}


_ASCII = """\
 ███╗   ███╗ █████╗ ██╗   ██╗██████╗ ██╗ ██████╗███████╗
 ████╗ ████║██╔══██╗██║   ██║██╔══██╗██║██╔════╝██╔════╝
 ██╔████╔██║███████║██║   ██║██████╔╝██║██║     █████╗
 ██║╚██╔╝██║██╔══██║██║   ██║██╔══██╗██║██║     ██╔══╝
 ██║ ╚═╝ ██║██║  ██║╚██████╔╝██║  ██║██║╚██████╗███████╗
 ╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝╚══════╝"""

_TIPS = [
    ("/plan",   "cadrer le projet et créer PLAN.md"),
    ("/tasks",  "afficher les tâches ouvertes"),
    ("/dev",    "exécuter le plan en autonomie"),
    ("/commit", "préparer un commit"),
    ("/check",  "vérifier l'état du projet"),
    ("/help",   "toutes les commandes"),
]


def _welcome(project_root: Path, session_id: str) -> None:
    from maurice import __version__
    from maurice.host.project import global_config_path
    import yaml

    try:
        cfg = yaml.safe_load(global_config_path().read_text()) or {}
        p = cfg.get("provider") or {}
        ptype = p.get("type", "mock")
        model = p.get("model", "")
        profile = cfg.get("permission_profile", "limited")
        provider_label = f"{ptype} · {model}" if model else ptype
    except Exception:
        provider_label = "mock"
        profile = "limited"
        __version__ = "?"

    # --- bottom: info (left) + tips (right) ---
    info = Table.grid(padding=(0, 1))
    info.add_column(style="dim",      no_wrap=True)
    info.add_column(style="info.val", no_wrap=True)
    info.add_row("provider", provider_label)
    info.add_row("profil",   profile)
    info.add_row("projet",   project_root.name)
    info.add_row("session",  session_id)

    tips = Table.grid(padding=(0, 2))
    tips.add_column(style="cmd.name", no_wrap=True)
    tips.add_column(style="cmd.desc")
    for name, desc in _TIPS:
        tips.add_row(name, desc)

    bottom = Table.grid(expand=True, padding=(0, 4))
    bottom.add_column(ratio=4)
    bottom.add_column(ratio=6)
    bottom.add_row(info, tips)

    # --- bottom grid: info (left) + tips (right) ---
    right_body = Table.grid()
    right_body.add_row(Text("Pour démarrer", style="bold"))
    right_body.add_row(Text(""))
    right_body.add_row(tips)
    right_body.add_row(Text(""))
    right_body.add_row(Text("Activité récente", style="bold"))
    right_body.add_row(Text(""))
    right_body.add_row(Text("  Aucune activité récente", style="dim"))

    bottom = Table.grid(expand=True, padding=(0, 3))
    bottom.add_column(ratio=4)
    bottom.add_column(ratio=6)
    bottom.add_row(info, right_body)

    # --- single outer panel ---
    body = Table.grid(padding=(0, 0))
    body.add_row(Text(_ASCII, style="bold", no_wrap=True, overflow="fold"))
    body.add_row(Text(""))
    body.add_row(bottom)

    _console.print()
    _console.print(Panel(body, border_style="dim", padding=(1, 2)))
    _console.print("[dim]  ? pour les raccourcis  ·  /exit ou Ctrl-D pour quitter[/]\n")


def run_repl(project_root: Path, *, session_id: str = "default") -> None:
    ensure_maurice_dir(project_root)
    client = MauriceClient(project_root)
    client.ensure_running()
    client.connect()

    _welcome(project_root, session_id)

    try:
        while True:
            try:
                message = _console.input("[prompt]>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                _console.print()
                break

            if not message:
                continue
            if message in {"/exit", "/quit", "/q"}:
                break

            _stream_turn(client, message, session_id)

    finally:
        client.close()


_SPINNER_WORDS = [
    "Réflexion", "Analyse", "Traitement", "Inspection",
    "Exploration", "Synthèse", "Recherche",
]
_spinner_idx = 0


def _next_spinner_word() -> str:
    global _spinner_idx
    word = _SPINNER_WORDS[_spinner_idx % len(_SPINNER_WORDS)]
    _spinner_idx += 1
    return word


_CONTEXT_WINDOW = 128_000  # default; could be read from config


def _print_context_bar(input_tokens: int, output_tokens: int) -> None:
    if not input_tokens and not output_tokens:
        return
    total = input_tokens + output_tokens
    pct = total / _CONTEXT_WINDOW
    bar_width = 20
    filled = round(pct * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    color = "green" if pct < 0.5 else "yellow" if pct < 0.8 else "red"
    _console.print(
        f"\n  [dim]context[/dim]  [{color}]{bar}[/{color}]"
        f"  [dim]{total:,} / {_CONTEXT_WINDOW:,} tokens  ({pct:.0%})[/dim]"
    )


def _stream_turn(client: MauriceClient, message: str, session_id: str) -> None:
    text_buf = ""
    ctx_tokens = (0, 0)
    first_event = True
    status = Status(
        f"[dim]{_next_spinner_word()}…[/dim]",
        spinner="dots",
        spinner_style="color(208)",
        console=_console,
    )
    status.start()

    try:
        for event in client.run_turn(
            message,
            session_id=session_id,
            approval_callback=_approval_prompt,
        ):
            if first_event:
                status.stop()
                first_event = False

            etype = event.get("type")

            if etype == "text_delta":
                delta = event.get("delta", "")
                if not text_buf and delta:
                    _console.print()  # blank line before first text
                print(delta, end="", flush=True)
                text_buf += delta

            elif etype == "tool_started":
                tool = event.get("tool", "")
                args = event.get("arguments", {})
                label = _tool_label(tool, args)
                _console.print(f"\n  [tool.running]{label}[/]", end="")

            elif etype == "tool_result":
                ok = event.get("ok", False)
                summary = event.get("summary", "")
                error = event.get("error")
                style = "tool.ok" if ok else "tool.err"
                icon = "✓" if ok else "✗"
                code = f" [{error}]" if error else ""
                short = summary.splitlines()[0] if summary else ""
                _console.print(f"\r  [{style}]{icon} {short}{code}[/]")

            elif etype == "error":
                _console.print(f"\n[error]  {event.get('message', '')}[/]")

            elif etype == "done":
                ctx_tokens = (event.get("input_tokens", 0), event.get("output_tokens", 0))
                break

    except OSError:
        status.stop()
        _console.print("\n[dim]  Connexion perdue — reconnexion…[/]")
        try:
            client.close()
            client.ensure_running()
            client.connect()
            _console.print("[dim]  Reconnecté. Renvoie ton message.[/]")
        except Exception as exc2:
            _console.print(f"[error]  Reconnexion échouée : {exc2}[/]")
        return

    if text_buf:
        # Re-render final response as markdown (replaces raw streamed text)
        lines = text_buf.count("\n") + 1
        print(f"\033[{lines}A\033[J", end="")
        _console.print(Markdown(text_buf))
    _console.print()
    _print_context_bar(*ctx_tokens)


def launch(cwd: Path | None = None, *, session_id: str = "default") -> int:
    from maurice.host.setup import needs_setup, run_setup
    if needs_setup():
        try:
            run_setup()
        except (EOFError, KeyboardInterrupt):
            print("\nAnnulé.", file=sys.stderr)
            return 1

    cwd = (cwd or Path.cwd()).resolve()
    project_root = resolve_project_root(cwd, confirm=True)
    if project_root is None:
        print("Annulé.", file=sys.stderr)
        return 1
    run_repl(project_root, session_id=session_id)
    return 0
