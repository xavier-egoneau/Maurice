"""Human-facing labels for tool approval prompts and activity display."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


TOOL_ACTION_LABELS: dict[str, str] = {
    "filesystem.list": "lister des fichiers",
    "filesystem.read": "lire un fichier",
    "filesystem.write": "modifier un fichier",
    "filesystem.mkdir": "créer un dossier",
    "filesystem.move": "déplacer ou renommer un fichier",
    "web.fetch": "ouvrir une page web",
    "web.search": "faire une recherche web",
    "reminders.create": "créer un rappel",
    "reminders.list": "lister les rappels",
    "reminders.cancel": "annuler un rappel",
    "vision.inspect": "inspecter une image",
    "vision.analyze": "analyser une image",
    "dreaming.run": "lancer une revue planifiee",
    "skills.create": "créer une compétence",
    "skills.list": "lister les compétences",
    "skills.reload": "recharger les compétences",
    "self_update.propose": "préparer une mise à jour de Maurice",
    "host.service_status": "consulter l'état du service Maurice",
    "host.events_tail": "lire les derniers événements Maurice",
    "host.credentials_list": "lister les profils d'authentification",
    "host.credentials": "lister les profils d'authentification",
    "host.request_secret": "enregistrer un secret",
    "host.agent_list": "lister les agents",
    "host.agent_create": "créer un agent",
    "host.agent_update": "modifier un agent",
    "host.agent_delete": "supprimer un agent",
    "host.telegram_bind": "connecter Telegram à un agent",
    "explore.tree": "explorer l'arborescence du projet",
    "explore.grep": "chercher dans les fichiers du projet",
    "explore.summary": "résumer le projet",
}


def tool_action_label(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Return a short user-facing action label for approval UX."""
    label = TOOL_ACTION_LABELS.get(tool_name)
    if label is None:
        label = _fallback_label(tool_name)
    arguments = arguments or {}
    if tool_name in {"host.agent_create", "host.agent_update", "host.agent_delete", "host.telegram_bind"}:
        agent_id = arguments.get("agent_id")
        if agent_id:
            return f"{label} `{agent_id}`"
    if tool_name in {"filesystem.write", "filesystem.read", "filesystem.move", "filesystem.mkdir"}:
        path = arguments.get("path") or arguments.get("source") or arguments.get("destination")
        if path:
            return f"{label} `{path}`"
    return label


def _fallback_label(tool_name: str) -> str:
    name = tool_name.rsplit(".", 1)[-1].replace("_", " ").strip()
    return name or "effectuer cette action"


# Short display labels — Claude Code style (Read, Write, Bash, ...)
TOOL_SHORT_LABELS: dict[str, str] = {
    "filesystem.read":     "Read",
    "filesystem.write":    "Write",
    "filesystem.edit":     "Edit",
    "filesystem.list":     "List",
    "filesystem.mkdir":    "Mkdir",
    "filesystem.move":     "Move",
    "shell.exec":          "Bash",
    "dev.bash":            "Bash",
    "web.fetch":           "Fetch",
    "web.search":          "WebSearch",
    "memory.remember":     "Remember",
    "memory.search":       "MemSearch",
    "memory.get":          "Memory",
    "explore.tree":        "Tree",
    "explore.grep":        "Grep",
    "explore.summary":     "Summary",
    "reminders.create":    "Reminder",
    "reminders.list":      "Reminders",
    "reminders.cancel":    "CancelReminder",
    "vision.inspect":      "Vision",
    "vision.analyze":      "Vision",
    "skills.create":       "NewSkill",
    "skills.list":         "Skills",
    "skills.reload":       "ReloadSkills",
    "dreaming.run":        "Dream",
    "self_update.propose": "Update",
    "host.service_status": "Status",
    "host.events_tail":    "Events",
    "host.credentials_list": "Credentials",
    "host.credentials":    "Credentials",
    "host.request_secret": "Secret",
    "host.agent_list":     "Agents",
    "host.agent_create":   "NewAgent",
    "host.agent_update":   "EditAgent",
    "host.agent_delete":   "DeleteAgent",
    "host.telegram_bind":  "Telegram",
}


def tool_short_label(tool_name: str) -> str:
    """Return a short display label: Read, Write, Bash, WebSearch, ..."""
    label = TOOL_SHORT_LABELS.get(tool_name)
    if label:
        return label
    action = tool_name.rsplit(".", 1)[-1].replace("_", "")
    return action.title() if action else tool_name


def tool_target(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return the primary target of a tool call (file, URL, command...)."""
    path = (
        arguments.get("path")
        or arguments.get("source_path")
        or arguments.get("target_path")
        or arguments.get("source")
        or arguments.get("destination")
    )
    url = arguments.get("url") or arguments.get("base_url")
    command = arguments.get("command")
    query = arguments.get("query")

    if path:
        parts = Path(str(path)).parts
        raw = "/".join(parts[-2:]) if len(parts) >= 2 else str(path)
        return raw[:60]
    if url:
        return (urlparse(str(url)).netloc or str(url))[:50]
    if command:
        return str(command)[:60]
    if query:
        return f'"{str(query)[:40]}"'
    return ""
