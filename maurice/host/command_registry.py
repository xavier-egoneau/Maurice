"""Channel-neutral command registry for gateway commands."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
import json
from pathlib import Path
import re
from typing import Any, Literal

from maurice.kernel.skills import SkillRegistry


@dataclass(frozen=True)
class CommandResult:
    text: str
    format: str = "markdown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandContext:
    message_text: str
    channel: str
    peer_id: str
    agent_id: str
    session_id: str
    correlation_id: str
    callbacks: dict[str, Any] = field(default_factory=dict)


CommandHandler = Callable[[CommandContext], CommandResult]
CommandScope = Literal["local", "global"]


@dataclass(frozen=True)
class RuntimeCommand:
    name: str
    description: str
    owner: str = "system"
    handler: CommandHandler | None = None
    renderer: str = "markdown"
    aliases: tuple[str, ...] = ()
    available_in: tuple[CommandScope, ...] = ("local", "global")
    project_required: bool = False

    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def available_for(self, scope: str | None) -> bool:
        return scope not in {"local", "global"} or scope in self.available_in


class CommandRegistry:
    def __init__(self, commands: list[RuntimeCommand] | None = None) -> None:
        self._commands: dict[str, RuntimeCommand] = {}
        if commands:
            for command in commands:
                self.register(command)

    def register(self, command: RuntimeCommand) -> None:
        for name in command.all_names():
            if name in self._commands:
                raise ValueError(f"Command already registered: {name}")
        for name in command.all_names():
            self._commands[name] = command

    def command_for_text(self, text: str) -> RuntimeCommand | None:
        command_name = command_name_from_text(text)
        if not command_name:
            return None
        return self._commands.get(command_name)

    def dispatch(self, context: CommandContext) -> CommandResult | None:
        command = self.command_for_text(context.message_text)
        if command is None or command.handler is None:
            return None
        if command.project_required and not _context_has_active_project(context):
            return _missing_project_result(command)
        return command.handler(context)

    def help_text(
        self,
        *,
        title: str = "Commandes Maurice",
        scope: str | None = None,
        agent_id: str | None = None,
        has_active_project: bool = True,
    ) -> str:
        unique: dict[str, RuntimeCommand] = {}
        for command in self._commands.values():
            if _command_visible(
                command,
                scope=scope,
                agent_id=agent_id,
                has_active_project=has_active_project,
            ):
                unique[command.name] = command
        grouped: dict[str, list[RuntimeCommand]] = defaultdict(list)
        for command in unique.values():
            grouped[command.owner].append(command)

        lines = [f"{title} :"]
        owner_order = ["system", "host"]
        for owner in [*owner_order, *sorted(set(grouped) - set(owner_order))]:
            commands = grouped.get(owner)
            if not commands:
                continue
            if len(grouped) > 1:
                lines.extend(["", _owner_title(owner)])
            for command in sorted(commands, key=lambda item: item.name):
                lines.append(f"{command.name} - {command.description}")
        return "\n".join(lines)

    def telegram_bot_commands(
        self,
        *,
        scope: str | None = None,
        agent_id: str | None = None,
        has_active_project: bool = True,
    ) -> list[dict[str, str]]:
        unique: dict[str, RuntimeCommand] = {}
        for command in self._commands.values():
            if _command_visible(
                command,
                scope=scope,
                agent_id=agent_id,
                has_active_project=has_active_project,
            ):
                unique[command.name] = command
        commands = []
        for command in sorted(unique.values(), key=lambda item: item.name):
            name = command.name.removeprefix("/")
            if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
                continue
            commands.append(
                {
                    "command": name,
                    "description": command.description[:256],
                }
            )
        return commands[:100]

    @classmethod
    def from_skill_registry(
        cls,
        registry: SkillRegistry,
        *,
        include_core: bool = True,
    ) -> "CommandRegistry":
        command_registry = cls(core_commands() if include_core else [])
        for command in {command.name: command for command in registry.commands.values()}.values():
            runtime = RuntimeCommand(
                name=command.name,
                description=command.description,
                owner=command.owner_skill,
                handler=_handler_from_path(command.handler),
                renderer=command.renderer,
                aliases=tuple(command.aliases),
                available_in=tuple(command.available_in),
                project_required=command.project_required,
            )
            if all(command_registry.command_for_text(name) is None for name in runtime.all_names()):
                command_registry.register(runtime)
        return command_registry


def core_commands() -> list[RuntimeCommand]:
    return [
        RuntimeCommand(
            name="/help",
            description="afficher cette aide",
            owner="system",
            handler=_help_handler,
            aliases=("/start",),
        ),
        RuntimeCommand(
            name="/new",
            description="repartir sur une session propre",
            owner="system",
            handler=_new_handler,
            aliases=("/reset",),
        ),
        RuntimeCommand(
            name="/stop",
            description="annuler la reponse en cours dans cette session",
            owner="system",
            handler=_stop_handler,
            aliases=("/cancel",),
        ),
        RuntimeCommand(
            name="/compact",
            description="compacter la session courante",
            owner="system",
            handler=_compact_handler,
        ),
        RuntimeCommand(
            name="/model",
            description="afficher le modele de l'agent courant",
            owner="system",
            handler=_model_handler,
        ),
    ]


def default_command_registry() -> CommandRegistry:
    registry = CommandRegistry(core_commands())
    registry.register(
        RuntimeCommand(
            name="/add_agent",
            description="creer un nouvel agent",
            owner="host",
            available_in=("global",),
        )
    )
    registry.register(
        RuntimeCommand(
            name="/edit_agent",
            description="modifier un agent (`/edit_agent <agent>`)",
            owner="host",
            available_in=("global",),
        )
    )
    return registry


def _command_visible_for_agent(command: RuntimeCommand, agent_id: str | None) -> bool:
    if agent_id in {None, "", "main"}:
        return True
    if command.name in {"/add_agent", "/edit_agent"}:
        return False
    return True


def _command_visible(
    command: RuntimeCommand,
    *,
    scope: str | None,
    agent_id: str | None,
    has_active_project: bool,
) -> bool:
    return (
        command.available_for(scope)
        and _command_visible_for_agent(command, agent_id)
        and (not command.project_required or has_active_project)
    )


def _context_has_active_project(context: CommandContext) -> bool:
    callbacks = context.callbacks
    if callbacks.get("active_project_path") or callbacks.get("project_root"):
        return True
    has_active_project = callbacks.get("has_active_project")
    if callable(has_active_project):
        return bool(has_active_project(context.agent_id, context.session_id))
    if has_active_project is not None:
        return bool(has_active_project)
    return _dev_state_has_active_project(context)


def _dev_state_has_active_project(context: CommandContext) -> bool:
    for path in _dev_state_candidates(context):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        active_path = data.get("active_project_path")
        if isinstance(active_path, str) and active_path.strip():
            return True
        active_name = data.get("active_project")
        if isinstance(active_name, str) and active_name.strip():
            return True
    return False


def _dev_state_candidates(context: CommandContext) -> list[Path]:
    callbacks = context.callbacks
    candidates: list[Path] = []
    content_root = callbacks.get("content_root")
    if content_root:
        candidates.append(Path(content_root).expanduser().resolve().parent / ".dev_state.json")
    agent_workspace = callbacks.get("agent_workspace")
    if agent_workspace:
        candidates.append(Path(agent_workspace).expanduser().resolve() / ".dev_state.json")
    else:
        workspace = callbacks.get("workspace")
        if workspace:
            candidates.append(
                Path(workspace).expanduser().resolve()
                / "agents"
                / context.agent_id
                / ".dev_state.json"
            )
    return candidates


def _missing_project_result(command: RuntimeCommand) -> CommandResult:
    return CommandResult(
        text=(
            f"La commande `{command.name}` demande un projet actif.\n\n"
            "Ouvre d'abord un projet avec `/project <nom-ou-chemin>`, ou lance Maurice depuis le dossier du projet."
        ),
        metadata={"command": command.name, "blocked": "missing_active_project"},
    )


def command_name_from_text(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ""
    first = stripped.split(maxsplit=1)[0]
    return first.split("@", 1)[0]


def _help_handler(context: CommandContext) -> CommandResult:
    registry = context.callbacks.get("command_registry")
    scope = context.callbacks.get("scope")
    scope_value = str(scope) if scope is not None else None
    if isinstance(registry, CommandRegistry):
        text = registry.help_text(
            scope=scope_value,
            agent_id=context.agent_id,
            has_active_project=_context_has_active_project(context),
        )
    else:
        text = default_command_registry().help_text(
            scope=scope_value,
            agent_id=context.agent_id,
            has_active_project=_context_has_active_project(context),
        )
    return CommandResult(text=text, metadata={"command": "/help"})


def _new_handler(context: CommandContext) -> CommandResult:
    cleared = _clear_conversation_state(context)
    reset_session = context.callbacks.get("reset_session")
    if reset_session is not None:
        reset_session(context.agent_id, context.session_id)
    text = "Session reinitialisee. On repart proprement."
    if cleared:
        text += "\n\nFlux en cours annule : " + ", ".join(cleared) + "."
    return CommandResult(
        text=text,
        metadata={"command": "/new", "cleared_state": cleared, "clears_history": True},
    )


def _stop_handler(context: CommandContext) -> CommandResult:
    cancel_turn = context.callbacks.get("cancel_turn")
    cancelled = bool(cancel_turn(context.agent_id, context.session_id)) if cancel_turn is not None else False
    cleared = _clear_conversation_state(context)
    parts = []
    if cancelled:
        parts.append("Annulation demandee pour la reponse en cours.")
    if cleared:
        parts.append("Flux en cours annule : " + ", ".join(cleared) + ".")
    if not parts:
        parts.append("Aucune reponse ni flux en cours a annuler dans cette session.")
    return CommandResult(
        text="\n\n".join(parts),
        metadata={"command": "/stop", "cancelled": cancelled, "cleared_state": cleared},
    )


def _clear_conversation_state(context: CommandContext) -> list[str]:
    callback = context.callbacks.get("clear_conversation_state")
    if callback is None:
        return []
    cleared = callback(context.agent_id, context.session_id)
    if isinstance(cleared, list):
        return [str(item) for item in cleared if str(item)]
    if isinstance(cleared, tuple):
        return [str(item) for item in cleared if str(item)]
    if isinstance(cleared, str) and cleared:
        return [cleared]
    return []


def _compact_handler(context: CommandContext) -> CommandResult:
    compact_session = context.callbacks.get("compact_session")
    if compact_session is None:
        return CommandResult(
            text="Compaction indisponible sur cette surface.",
            metadata={"command": "/compact", "preserve_view": True},
        )
    text = compact_session(context.agent_id, context.session_id)
    return CommandResult(text=str(text), metadata={"command": "/compact", "preserve_view": True})


def _model_handler(context: CommandContext) -> CommandResult:
    model_summary = context.callbacks.get("model_summary")
    if model_summary is None:
        return CommandResult(
            text="Modele courant non disponible sur cette surface.",
            metadata={"command": "/model"},
        )
    text = model_summary(context.agent_id)
    return CommandResult(text=str(text), metadata={"command": "/model"})


def _setup_handler(context: CommandContext) -> CommandResult:
    scope = str(context.callbacks.get("scope") or "")
    if scope == "global":
        text = (
            "Maurice est deja ouvert au niveau assistant de bureau pour cette surface.\n\n"
            "Pour reconfigurer le niveau de contexte, le provider, les permissions ou le workspace, lance :\n\n"
            "```bash\nmaurice setup\n```"
        )
    else:
        text = (
            "Tu es dans un contexte dossier. Pour configurer Maurice ou passer en assistant de bureau, lance :\n\n"
            "```bash\nmaurice setup\n```\n\n"
            "Le wizard proposera le contexte dossier ou le contexte global. En global, Maurice "
            "choisira un workspace, une memoire centrale et pourra rester disponible avec `maurice start`."
        )
    return CommandResult(text=text, metadata={"command": "/setup"})


def _owner_title(owner: str) -> str:
    if owner == "system":
        return "Systeme"
    if owner == "host":
        return "Agents"
    if owner == "dev":
        return "Dev"
    return owner


def _handler_from_path(path: str) -> CommandHandler | None:
    if not path:
        return None
    module_name, _, function_name = path.rpartition(".")
    if not module_name or not function_name:
        return None
    try:
        function = getattr(import_module(module_name), function_name)
    except (ImportError, AttributeError):
        return None

    def handler(context: CommandContext) -> CommandResult:
        result = function(context)
        if isinstance(result, CommandResult):
            return result
        return CommandResult(text=str(result))

    return handler
