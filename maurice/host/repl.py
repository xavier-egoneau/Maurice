"""Interactive REPL for Maurice CLI mode."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from maurice.host.client import MauriceClient
from maurice.host.context_meter import context_bar, context_summary, context_usage
from maurice.host.project import ensure_maurice_dir, resolve_project_root, sessions_dir
from maurice.kernel.session import SessionRecord, SessionStore
from maurice.kernel.tool_labels import tool_short_label, tool_target


_THEME = Theme({
    "prompt":         "bold white",
    "dim":            "dim",
    "tool.bullet":    "bold dim",
    "tool.verb":      "bold",
    "tool.target":    "dim",
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



def _tool_verb_target(tool_name: str, arguments: dict) -> tuple[str, str]:
    return tool_short_label(tool_name), tool_target(tool_name, arguments)


def _approval_prompt(tool: str, arguments: dict, permission_class: str, reason: str) -> bool | str:
    args_str = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:3])
    _console.print()
    _console.print(f"  [approval.label]Permission requise[/] [approval]{tool}({args_str})[/]")
    _console.print(f"  [dim]{permission_class}  ·  {reason}[/]")
    try:
        answer = _console.input("  [approval]Autoriser ?[/] [y/N/session] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer in {"session", "s"}:
        return "session"
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
    ("/setup",  "configurer ou passer en assistant de bureau"),
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
    current_session = session_id

    _welcome(project_root, current_session)

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
            if message == "/sessions":
                _print_sessions(project_root, current_session)
                continue
            if message == "/session" or message.startswith("/session "):
                switched = _switch_session(project_root, message, current_session)
                if switched:
                    current_session = switched
                continue

            _stream_turn(client, message, current_session)

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


def _print_context_bar(input_tokens: int, output_tokens: int) -> None:
    usage = context_usage(input_tokens, output_tokens)
    if usage is None:
        return
    bar = context_bar(usage, available_width=_console.width or 80)
    color = {"low": "green", "medium": "yellow", "high": "red"}[usage["level"]]
    _console.print(
        f"\n  [dim]context[/dim]  [{color}]{bar}[/{color}]"
        f"  [dim]{context_summary(usage).removeprefix('context ')}[/dim]"
    )


def _stream_turn(client: MauriceClient, message: str, session_id: str) -> None:
    text_buf = ""
    ctx_tokens = (0, 0)
    first_event = True
    last_kind: str | None = None  # "text" | "tool"
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
                    _console.print()
                print(delta, end="", flush=True)
                text_buf += delta
                last_kind = "text"

            elif etype == "tool_started":
                tool = event.get("tool", "")
                args = event.get("arguments", {})
                verb, target = _tool_verb_target(tool, args)
                if last_kind == "text":
                    _console.print()
                if target:
                    _console.print(f"\n  [tool.bullet]●[/] [tool.verb]{verb}[/]  [tool.target]{target}[/]")
                else:
                    _console.print(f"\n  [tool.bullet]●[/] [tool.verb]{verb}[/]")
                last_kind = "tool"

            elif etype == "tool_result":
                ok = event.get("ok", False)
                summary = event.get("summary", "")
                error = event.get("error")
                style = "tool.ok" if ok else "tool.err"
                short = summary.splitlines()[0][:80] if summary else ""
                code = f"  [{error}]" if error and not ok else ""
                if short:
                    _console.print(f"    [{style}]{short}{code}[/]")

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

    _console.print()
    _print_context_bar(*ctx_tokens)


def _session_store(project_root: Path) -> SessionStore:
    return SessionStore(sessions_dir(project_root))


def _print_sessions(project_root: Path, current_session: str) -> None:
    rows = _session_rows(_session_store(project_root).list("main"), current_session)
    if not rows:
        _console.print("[dim]Aucune session enregistrée pour ce projet.[/]")
        return
    table = Table("Session", "Messages", "Tours", "Mise à jour", show_header=True)
    for session_id, messages, turns, updated_at in rows:
        style = "bold" if session_id == current_session else ""
        table.add_row(session_id, str(messages), str(turns), updated_at, style=style)
    _console.print(table)


def _session_rows(
    sessions: list[SessionRecord],
    current_session: str,
) -> list[tuple[str, int, int, str]]:
    rows = [
        (
            session.id,
            len(session.messages),
            len(session.turns),
            session.updated_at.strftime("%Y-%m-%d %H:%M"),
        )
        for session in sessions
    ]
    if current_session and all(row[0] != current_session for row in rows):
        rows.insert(0, (current_session, 0, 0, "nouvelle"))
    return rows


def _switch_session(project_root: Path, message: str, current_session: str) -> str | None:
    parts = message.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        _console.print(f"[dim]Session courante :[/] {current_session}")
        _console.print("[dim]Usage : /session <nom>[/]")
        return None
    session_id = parts[1].strip()
    if not _valid_session_id(session_id):
        _console.print("[error]Nom de session invalide.[/]")
        return None
    store = _session_store(project_root)
    try:
        store.load("main", session_id)
        created = False
    except FileNotFoundError:
        store.create("main", session_id=session_id)
        created = True
    status = "créée" if created else "ouverte"
    _console.print(f"[dim]Session `{session_id}` {status}.[/]")
    return session_id


def _valid_session_id(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and "\\" not in session_id


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
