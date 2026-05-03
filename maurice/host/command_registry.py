"""Channel-neutral command registry for gateway commands."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
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
        return command.handler(context)

    def help_text(self, *, title: str = "Commandes Maurice", scope: str | None = None) -> str:
        unique: dict[str, RuntimeCommand] = {}
        for command in self._commands.values():
            if command.available_for(scope):
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
        RuntimeCommand(
            name="/setup",
            description="configurer Maurice ou passer en assistant de bureau",
            owner="system",
            handler=_setup_handler,
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
        text = registry.help_text(scope=scope_value)
    else:
        text = default_command_registry().help_text(scope=scope_value)
    return CommandResult(text=text, metadata={"command": "/help"})


def _new_handler(context: CommandContext) -> CommandResult:
    reset_session = context.callbacks.get("reset_session")
    if reset_session is not None:
        reset_session(context.agent_id, context.session_id)
    return CommandResult(
        text="Session reinitialisee. On repart proprement.",
        metadata={"command": "/new"},
    )


def _stop_handler(context: CommandContext) -> CommandResult:
    cancel_turn = context.callbacks.get("cancel_turn")
    cancelled = bool(cancel_turn(context.agent_id, context.session_id)) if cancel_turn is not None else False
    text = (
        "Annulation demandee pour la reponse en cours."
        if cancelled
        else "Aucune reponse en cours a annuler dans cette session."
    )
    return CommandResult(
        text=text,
        metadata={"command": "/stop", "cancelled": cancelled},
    )


def _compact_handler(context: CommandContext) -> CommandResult:
    compact_session = context.callbacks.get("compact_session")
    if compact_session is None:
        return CommandResult(
            text="Compaction indisponible sur cette surface.",
            metadata={"command": "/compact"},
        )
    text = compact_session(context.agent_id, context.session_id)
    return CommandResult(text=str(text), metadata={"command": "/compact"})


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
