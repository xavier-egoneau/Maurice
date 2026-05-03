"""Deterministic conversational flows for host-managed agent setup."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import Field

from maurice.host.agents import create_agent, list_agents, update_agent
from maurice.host.credentials import load_workspace_credentials
from maurice.host.model_catalog import chatgpt_model_choices, ollama_model_choices
from maurice.host.paths import host_config_path
from maurice.host.secret_capture import request_secret_capture
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.contracts import MauriceModel, SkillManifest


DEFAULT_SKILL_DESCRIPTIONS: dict[str, str] = {
    "filesystem": "lire et écrire des fichiers dans son espace",
    "memory": "retenir des informations utiles",
    "web": "chercher et lire des informations web",
    "explore": "explorer un projet, son arbre et son contenu",
    "reminders": "créer et suivre des rappels",
    "vision": "analyser des images",
    "dreaming": "consolider la mémoire et agir avec proactivité",
    "skills": "créer de nouvelles compétences",
    "host": "diagnostiquer et configurer Maurice",
    "self_update": "signaler des bugs Maurice et préparer des améliorations",
    "dev": "piloter un projet de développement",
}

PERMISSIONS = {
    "safe": "lecture seule et très prudent",
    "limited": "peut lire/écrire dans son espace et utiliser ses compétences",
    "power": "accès étendu, réservé aux agents techniques",
}

START_MARKERS = (
    "/add_agent",
    "nouvel agent",
    "nouveau agent",
    "crée un agent",
    "creer un agent",
    "créer un agent",
    "ajoute un agent",
)
TELEGRAM_EDIT_MARKERS = (
    "modifie le bot",
    "modifie bot",
    "modifions le bot",
    "modifions bot",
    "modifier le bot",
    "modifier bot",
    "modifier telegram",
    "modifie telegram",
    "modifions telegram",
    "changer le bot",
    "changer telegram",
    "configurer telegram",
    "configure telegram",
)
CANCEL_WORDS = {"annule", "annuler", "stop", "cancel", "abandonne"}
YES_WORDS = {"oui", "ok", "go", "vas-y", "vasy", "yes", "y"}
NO_WORDS = {"non", "no", "n"}
DEFAULT_WORDS = {"defaut", "défaut", "par defaut", "par défaut", "default", "aucun", "non"}
KEEP_WORDS = {
    "garder",
    "garde",
    "on garde",
    "on garde ce qui etait",
    "on garde ce qui était",
    "inchangé",
    "inchange",
    "pareil",
}
SKILL_WORDS = {"skill", "skills", "competence", "competences", "compétence", "compétences", "capacite", "capacites", "capacité", "capacités"}


class AgentCreationState(MauriceModel):
    step: str = "agent_id"
    data: dict[str, Any] = Field(default_factory=dict)


class AgentWizardStore(MauriceModel):
    sessions: dict[str, AgentCreationState] = Field(default_factory=dict)


def handle_agent_creation_wizard(
    workspace_root: str | Path,
    *,
    agent_id: str,
    session_id: str,
    text: str,
) -> str | None:
    workspace = Path(workspace_root).expanduser().resolve()
    store = _load_store(workspace)
    state = store.sessions.get(_key(agent_id, session_id))
    normalized = _normalize(text)

    if state is None:
        if _starts_agent_creation(normalized):
            state = AgentCreationState()
            store.sessions[_key(agent_id, session_id)] = state
            _write_store(workspace, store)
            return "Je vais t'aider à créer un nouvel agent.\n\n1. Quel nom unique veux-tu lui donner ? Exemple : `assistant_devoirs`."
        if _starts_agent_edit(normalized):
            state = _new_agent_edit_state(
                workspace,
                requested_agent_id=_extract_agent_reference(workspace, normalized),
            )
            store.sessions[_key(agent_id, session_id)] = state
            _write_store(workspace, store)
            return _agent_edit_intro_question(state.data)
        if _starts_telegram_edit(normalized):
            state = _new_telegram_edit_state(
                workspace,
                requested_agent_id=_extract_agent_reference(workspace, normalized),
            )
            store.sessions[_key(agent_id, session_id)] = state
            _write_store(workspace, store)
            return _telegram_edit_agent_question(state.data, workspace)
        return None

    if state.step.startswith("agent_edit_"):
        response = _advance_agent_edit(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            state=state,
            text=text,
        )
        if response.clear:
            _clear(workspace, agent_id, session_id)
        else:
            store.sessions[_key(agent_id, session_id)] = state
            _write_store(workspace, store)
        return response.text

    if state.step.startswith("edit_"):
        response = _advance_telegram_edit(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            state=state,
            text=text,
        )
        if response.clear:
            _clear(workspace, agent_id, session_id)
        else:
            store.sessions[_key(agent_id, session_id)] = state
            _write_store(workspace, store)
        return response.text

    if normalized in CANCEL_WORDS:
        _clear(workspace, agent_id, session_id)
        return "Creation d'agent annulee."

    response = _advance(workspace, agent_id=agent_id, session_id=session_id, state=state, text=text)
    if response.clear:
        _clear(workspace, agent_id, session_id)
    else:
        store.sessions[_key(agent_id, session_id)] = state
        _write_store(workspace, store)
    return response.text


def clear_agent_creation_wizard(
    workspace_root: str | Path,
    *,
    agent_id: str,
    session_id: str,
) -> None:
    _clear(Path(workspace_root).expanduser().resolve(), agent_id, session_id)


class _Response(MauriceModel):
    text: str
    clear: bool = False


def _advance_agent_edit(
    workspace: Path,
    *,
    agent_id: str,
    session_id: str,
    state: AgentCreationState,
    text: str,
) -> _Response:
    normalized = _normalize(text)
    data = state.data
    if normalized in CANCEL_WORDS:
        return _Response(text="Modification d'agent annulee.", clear=True)
    if (
        state.step != "agent_edit_skills_value"
        and data.get("agent")
        and _wants_change_skills(normalized)
    ):
        state.step = "agent_edit_skills_value"
        return _Response(text=_edit_skills_question(workspace, list(data.get("skills") or [])))

    if state.step == "agent_edit_agent_value":
        target = _sanitize_agent_id(text)
        if target not in load_workspace_config(workspace).agents.agents:
            return _Response(text=_agent_edit_agent_value_question(workspace))
        state.data = _agent_edit_data_for(workspace, target)
        state.step = "agent_edit_permission_change"
        return _Response(text=_agent_edit_permission_question(state.data))

    if state.step == "agent_edit_permission_change":
        if normalized in YES_WORDS:
            state.step = "agent_edit_permission_value"
            return _Response(text=_permission_question())
        if normalized in NO_WORDS or _wants_keep_current(normalized):
            state.step = "agent_edit_skills_change"
            return _Response(text=_agent_edit_skills_change_question(data))
        return _Response(text=_agent_edit_permission_question(data))

    if state.step == "agent_edit_permission_value":
        permission = normalized.strip("` ")
        if permission not in PERMISSIONS:
            return _Response(text=_permission_question())
        data["permission"] = permission
        state.step = "agent_edit_skills_change"
        return _Response(text=_agent_edit_skills_change_question(data))

    if state.step == "agent_edit_skills_change":
        if normalized in YES_WORDS:
            state.step = "agent_edit_skills_value"
            return _Response(text=_edit_skills_question(workspace, list(data.get("skills") or [])))
        if normalized in NO_WORDS or _wants_keep_current(normalized):
            state.step = "agent_edit_model_change"
            return _Response(text=_agent_edit_model_change_question(data, workspace))
        return _Response(text=_agent_edit_skills_change_question(data))

    if state.step == "agent_edit_skills_value":
        parsed = _parse_skills(text, list(data.get("skills") or []), workspace)
        if parsed is None:
            return _Response(text=_edit_skills_question(workspace, list(data.get("skills") or [])))
        data["skills"] = parsed
        state.step = "agent_edit_model_change"
        return _Response(text=_agent_edit_model_change_question(data, workspace))

    if state.step == "agent_edit_model_change":
        if normalized in YES_WORDS:
            model_source, model_choices = _model_choices_for_workspace(
                workspace,
                agent_id=str(data.get("agent")),
            )
            data["model_source"] = model_source
            data["model_choices"] = [{"id": model_id, "label": label} for model_id, label in model_choices]
            state.step = "agent_edit_model_value"
            return _Response(text=_model_question(model_source, model_choices))
        if normalized in NO_WORDS:
            state.step = "agent_edit_telegram_change"
            return _Response(text=_agent_edit_telegram_change_question(data, workspace))
        return _Response(text=_agent_edit_model_change_question(data, workspace))

    if state.step == "agent_edit_model_value":
        model_source = data.get("model_source") if isinstance(data.get("model_source"), dict) else {}
        model_choices = [
            (str(item.get("id")), str(item.get("label") or item.get("id")))
            for item in data.get("model_choices", [])
            if isinstance(item, dict) and item.get("id")
        ]
        selected_model = _parse_model_choice(text, model_choices)
        if normalized in DEFAULT_WORDS or selected_model == "__default__":
            data["model"] = None
            data["clear_model"] = True
        elif selected_model:
            model = dict(model_source)
            model["name"] = selected_model
            data["model"] = model
            data["clear_model"] = False
        elif model_choices and normalized.isdigit():
            return _Response(text=_model_question(model_source, model_choices))
        else:
            model = dict(model_source)
            model["name"] = text.strip()
            data["model"] = model
            data["clear_model"] = False
        state.step = "agent_edit_telegram_change"
        return _Response(text=_agent_edit_telegram_change_question(data, workspace))

    if state.step == "agent_edit_telegram_change":
        if normalized in YES_WORDS:
            data["telegram_changed"] = True
            data["credential"] = str(data.get("credential") or _telegram_credential_for_agent(str(data["agent"])))
            state.step = "agent_edit_token_change"
            return _Response(text=_telegram_edit_token_question(data, workspace))
        if normalized in NO_WORDS:
            state.step = "agent_edit_confirm"
            return _Response(text=_agent_edit_summary(data, workspace))
        return _Response(text=_agent_edit_telegram_change_question(data, workspace))

    if state.step == "agent_edit_token_change":
        if normalized in YES_WORDS:
            credential = str(data.get("credential") or _telegram_credential_for_agent(str(data["agent"])))
            data["credential"] = credential
            request_secret_capture(
                workspace,
                agent_id=agent_id,
                session_id=session_id,
                credential=credential,
                provider="telegram_bot",
                secret_type="token",
                prompt="Nouveau token BotFather pour ce bot Telegram.",
            )
            state.step = "agent_edit_token_capture"
            return _Response(
                text=(
                    f"Envoie-moi maintenant le token BotFather. "
                    f"Je le stockerai sous `{credential}`, hors du workspace."
                )
            )
        if normalized in NO_WORDS:
            state.step = "agent_edit_users_change"
            return _Response(text=_telegram_edit_users_question(data))
        return _Response(text=_telegram_edit_token_question(data, workspace))

    if state.step == "agent_edit_token_capture":
        credential = str(data.get("credential") or _telegram_credential_for_agent(str(data["agent"])))
        if credential not in load_workspace_credentials(workspace).credentials:
            request_secret_capture(
                workspace,
                agent_id=agent_id,
                session_id=session_id,
                credential=credential,
                provider="telegram_bot",
                secret_type="token",
                prompt="Nouveau token BotFather pour ce bot Telegram.",
            )
            return _Response(text="Je n'ai pas encore reçu le token. Envoie le token BotFather maintenant.")
        state.step = "agent_edit_users_change"
        return _Response(text=_telegram_edit_users_question(data))

    if state.step == "agent_edit_users_change":
        if normalized in YES_WORDS:
            state.step = "agent_edit_users_value"
            return _Response(text="Quels IDs Telegram autoriser ? Sépare-les par des virgules, ou réponds `aucun`.")
        if normalized in NO_WORDS:
            state.step = "agent_edit_chats_change"
            return _Response(text=_telegram_edit_chats_question(data))
        return _Response(text=_telegram_edit_users_question(data))

    if state.step == "agent_edit_users_value":
        users = _parse_telegram_ids(text)
        if users is None:
            return _Response(text="Je n'ai pas reconnu les IDs. Exemple : `123456789, 987654321`.")
        data["allowed_users"] = users
        state.step = "agent_edit_chats_change"
        return _Response(text=f"IDs utilisateurs enregistres : {_csv_ints(users)}.\n\n" + _telegram_edit_chats_question(data))

    if state.step == "agent_edit_chats_change":
        if normalized in YES_WORDS:
            state.step = "agent_edit_chats_value"
            return _Response(text="Quels IDs de groupes/chats autoriser ? Sépare-les par des virgules, ou réponds `aucun`.")
        if normalized in NO_WORDS:
            state.step = "agent_edit_confirm"
            return _Response(text=_agent_edit_summary(data, workspace))
        return _Response(text=_telegram_edit_chats_question(data))

    if state.step == "agent_edit_chats_value":
        chats = _parse_telegram_ids(text)
        if chats is None:
            return _Response(text="Je n'ai pas reconnu les IDs de chats. Exemple : `-1001234567890`, ou `aucun`.")
        data["allowed_chats"] = chats
        state.step = "agent_edit_confirm"
        return _Response(text=f"IDs groupes/chats enregistres : {_csv_ints(chats)}.\n\n" + _agent_edit_summary(data, workspace))

    if state.step == "agent_edit_confirm":
        if normalized in NO_WORDS:
            return _Response(text="Modification d'agent annulee.", clear=True)
        if normalized not in YES_WORDS:
            return _Response(text=_agent_edit_summary(data, workspace))
        _apply_agent_edit(workspace, data)
        return _Response(text=f"Agent `{data.get('agent')}` mis a jour.", clear=True)

    state.step = "agent_edit_permission_change"
    return _Response(text=_agent_edit_permission_question(data))


def _advance_telegram_edit(
    workspace: Path,
    *,
    agent_id: str,
    session_id: str,
    state: AgentCreationState,
    text: str,
) -> _Response:
    normalized = _normalize(text)
    data = state.data
    if normalized in CANCEL_WORDS:
        return _Response(text="Modification Telegram annulee.", clear=True)

    if state.step == "edit_agent_change":
        if normalized in YES_WORDS:
            state.step = "edit_agent_value"
            return _Response(text=_telegram_edit_agent_value_question(workspace))
        if normalized in NO_WORDS:
            state.step = "edit_token_change"
            return _Response(text=_telegram_edit_token_question(data, workspace))
        return _Response(text=_telegram_edit_agent_question(data, workspace))

    if state.step == "edit_agent_value":
        target = _sanitize_agent_id(text)
        if target not in load_workspace_config(workspace).agents.agents:
            return _Response(text=_telegram_edit_agent_value_question(workspace))
        data["agent"] = target
        data["credential"] = _telegram_credential_for_agent(target)
        state.step = "edit_token_change"
        return _Response(text=_telegram_edit_token_question(data, workspace))

    if state.step == "edit_token_change":
        if normalized in YES_WORDS:
            credential = str(data.get("credential") or _telegram_credential_for_agent(str(data["agent"])))
            data["credential"] = credential
            request_secret_capture(
                workspace,
                agent_id=agent_id,
                session_id=session_id,
                credential=credential,
                provider="telegram_bot",
                secret_type="token",
                prompt="Nouveau token BotFather pour ce bot Telegram.",
            )
            state.step = "edit_token_capture"
            return _Response(
                text=(
                    f"Envoie-moi maintenant le token BotFather. "
                    f"Je le stockerai sous `{credential}`, hors du workspace."
                )
            )
        if normalized in NO_WORDS:
            state.step = "edit_users_change"
            return _Response(text=_telegram_edit_users_question(data))
        return _Response(text=_telegram_edit_token_question(data, workspace))

    if state.step == "edit_token_capture":
        credential = str(data.get("credential") or _telegram_credential_for_agent(str(data["agent"])))
        if credential not in load_workspace_credentials(workspace).credentials:
            request_secret_capture(
                workspace,
                agent_id=agent_id,
                session_id=session_id,
                credential=credential,
                provider="telegram_bot",
                secret_type="token",
                prompt="Nouveau token BotFather pour ce bot Telegram.",
            )
            return _Response(text="Je n'ai pas encore reçu le token. Envoie le token BotFather maintenant.")
        state.step = "edit_users_change"
        return _Response(text=_telegram_edit_users_question(data))

    if state.step == "edit_users_change":
        if normalized in YES_WORDS:
            state.step = "edit_users_value"
            return _Response(text="Quels IDs Telegram autoriser ? Sépare-les par des virgules, ou réponds `aucun`.")
        if normalized in NO_WORDS:
            state.step = "edit_chats_change"
            return _Response(text=_telegram_edit_chats_question(data))
        return _Response(text=_telegram_edit_users_question(data))

    if state.step == "edit_users_value":
        users = _parse_telegram_ids(text)
        if users is None:
            return _Response(text="Je n'ai pas reconnu les IDs. Exemple : `123456789, 987654321`.")
        data["allowed_users"] = users
        state.step = "edit_chats_change"
        return _Response(text=f"IDs utilisateurs enregistres : {_csv_ints(users)}.\n\n" + _telegram_edit_chats_question(data))

    if state.step == "edit_chats_change":
        if normalized in YES_WORDS:
            state.step = "edit_chats_value"
            return _Response(text="Quels IDs de groupes/chats autoriser ? Sépare-les par des virgules, ou réponds `aucun`.")
        if normalized in NO_WORDS:
            state.step = "edit_confirm"
            return _Response(text=_telegram_edit_summary(data, workspace))
        return _Response(text=_telegram_edit_chats_question(data))

    if state.step == "edit_chats_value":
        chats = _parse_telegram_ids(text)
        if chats is None:
            return _Response(text="Je n'ai pas reconnu les IDs de chats. Exemple : `-1001234567890`, ou `aucun`.")
        data["allowed_chats"] = chats
        state.step = "edit_confirm"
        return _Response(text=f"IDs groupes/chats enregistres : {_csv_ints(chats)}.\n\n" + _telegram_edit_summary(data, workspace))

    if state.step == "edit_confirm":
        if normalized in NO_WORDS:
            return _Response(text="Modification Telegram annulee.", clear=True)
        if normalized not in YES_WORDS:
            return _Response(text=_telegram_edit_summary(data, workspace))
        _apply_telegram_edit(workspace, data)
        return _Response(
            text=(
                "Configuration Telegram mise a jour.\n\n"
                "Redemarre Maurice pour basculer sur ce bot : `maurice restart`."
            ),
            clear=True,
        )

    state.step = "edit_agent_change"
    return _Response(text=_telegram_edit_agent_question(data, workspace))


def _advance(
    workspace: Path,
    *,
    agent_id: str,
    session_id: str,
    state: AgentCreationState,
    text: str,
) -> _Response:
    normalized = _normalize(text)
    data = state.data

    if state.step == "agent_id":
        technical_id = _sanitize_agent_id(text)
        if not technical_id:
            return _Response(text="J'ai besoin d'un identifiant technique simple. Exemple : `assistant_devoirs`.")
        data["id"] = technical_id
        suffix = ""
        if technical_id != text.strip():
            suffix = f"\n\nJe vais utiliser l'identifiant technique `{technical_id}`."
        state.step = "role"
        return _Response(text=f"Parfait.{suffix}\n\n2. Quelle sera sa mission principale ?")

    if state.step == "role":
        role = text.strip()
        if len(role) < 3:
            return _Response(text="Decris sa mission en une courte phrase, par exemple : `organisation et aide aux devoirs`.")
        data["role"] = role
        state.step = "permission"
        return _Response(text=_permission_question())

    if state.step == "permission":
        permission = normalized.strip("` ")
        if permission not in PERMISSIONS:
            return _Response(text="Je n'ai pas reconnu ce niveau.\n\n" + _permission_question())
        data["permission"] = permission
        data["suggested_skills"] = _suggest_skills(str(data.get("role", "")))
        state.step = "skills"
        return _Response(text=_skills_question(workspace, data["suggested_skills"]))

    if state.step == "skills":
        parsed = _parse_skills(text, data.get("suggested_skills") or [], workspace)
        if parsed is None:
            return _Response(text=_skills_question(workspace, data.get("suggested_skills") or []))
        data["skills"] = parsed
        state.step = "model"
        model_source, model_choices = _model_choices_for_workspace(workspace)
        data["model_source"] = model_source
        data["model_choices"] = [{"id": model_id, "label": label} for model_id, label in model_choices]
        return _Response(text=_model_question(model_source, model_choices))

    if state.step == "model":
        model_source = data.get("model_source") if isinstance(data.get("model_source"), dict) else {}
        model_choices = [
            (str(item.get("id")), str(item.get("label") or item.get("id")))
            for item in data.get("model_choices", [])
            if isinstance(item, dict) and item.get("id")
        ]
        selected_model = _parse_model_choice(text, model_choices)
        if normalized in DEFAULT_WORDS or selected_model == "__default__":
            data["model"] = None
        elif selected_model:
            model = dict(model_source)
            model["name"] = selected_model
            data["model"] = model
        elif model_choices and normalized.isdigit():
            return _Response(text=_model_question(model_source, model_choices))
        else:
            model = dict(model_source)
            model["name"] = text.strip()
            data["model"] = model
        state.step = "telegram"
        return _Response(text=_telegram_question())

    if state.step == "telegram":
        if normalized in {"aucun", "non", "no", "none", "pas maintenant"}:
            data["telegram"] = False
            data["telegram_allowed_users"] = []
            state.step = "confirm"
            return _Response(text=_summary_question(data))
        if normalized not in {"telegram", "tg"}:
            return _Response(text=_telegram_question())
        data["telegram"] = True
        credential = f"telegram_bot_{data.get('id')}"
        data["telegram_credential"] = credential
        credentials = load_workspace_credentials(workspace).credentials
        if credential in credentials:
            state.step = "telegram_users"
            return _Response(text=_telegram_users_question(workspace))
        request_secret_capture(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            credential=credential,
            provider="telegram_bot",
            secret_type="token",
            prompt="Token BotFather pour le nouveau bot Telegram.",
        )
        state.step = "telegram_token"
        return _Response(
            text=(
                "D'accord, on connecte Telegram avec un bot dedie a cet agent.\n\n"
                "Envoie-moi maintenant le token BotFather de ce nouveau bot. "
                "Je vais le stocker hors du workspace et il ne sera pas transmis au modele."
            )
        )

    if state.step == "telegram_token":
        credential = str(data.get("telegram_credential") or "telegram_bot")
        credentials = load_workspace_credentials(workspace).credentials
        if credential not in credentials:
            request_secret_capture(
                workspace,
                agent_id=agent_id,
                session_id=session_id,
                credential=credential,
                provider="telegram_bot",
                secret_type="token",
                prompt="Token BotFather pour le bot Telegram.",
            )
            return _Response(text="Je n'ai pas encore le token Telegram. Envoie le token BotFather maintenant.")
        state.step = "telegram_users"
        # The current message can already be the IDs answer.
        return _advance(workspace, agent_id=agent_id, session_id=session_id, state=state, text=text)

    if state.step == "telegram_users":
        parsed_ids = _parse_telegram_ids(text)
        if parsed_ids is None:
            current = _current_telegram_users(workspace)
            if normalized in YES_WORDS and current:
                parsed_ids = current
            else:
                return _Response(text=_telegram_users_question(workspace))
        data["telegram_allowed_users"] = parsed_ids
        state.step = "confirm"
        return _Response(text=_summary_question(data))

    if state.step == "confirm":
        if normalized in NO_WORDS:
            return _Response(text="Creation d'agent annulee.", clear=True)
        if normalized not in YES_WORDS:
            return _Response(text=_summary_question(data))
        try:
            created = create_agent(
                workspace,
                agent_id=str(data["id"]),
                permission_profile=str(data["permission"]),
                skills=list(data.get("skills") or []),
                credentials=_credentials_for_agent(data),
                channels=["telegram"] if data.get("telegram") else [],
                model=data.get("model"),
                confirmed_permission_elevation=True,
            )
            if data.get("telegram"):
                _bind_telegram(
                    workspace,
                    agent_id=created.id,
                    credential=str(data.get("telegram_credential") or "telegram_bot"),
                    allowed_users=list(data.get("telegram_allowed_users") or []),
                )
        except Exception as exc:
            return _Response(text=f"Je n'ai pas pu creer l'agent : {exc}", clear=True)
        return _Response(
            text=(
                f"Agent `{created.id}` cree.\n\n"
                f"Pour lui parler depuis la console : `maurice run --agent {created.id} --message \"salut\"`."
            ),
            clear=True,
        )

    state.step = "agent_id"
    return _Response(text="On reprend proprement.\n\n1. Quel nom unique veux-tu donner a cet agent ?")


def _permission_question() -> str:
    return (
        "3. Quel niveau de permission veux-tu lui donner ?\n\n"
        "- `safe` : lecture seule et tres prudent\n"
        "- `limited` : peut lire/ecrire dans son espace et utiliser ses competences (recommande)\n"
        "- `power` : acces etendu, a reserver aux agents techniques\n\n"
        "Reponds `safe`, `limited` ou `power`."
    )


def _skills_question(workspace: Path, suggested: list[str]) -> str:
    available_skills = _available_skills(workspace)
    options = _skill_options_text(available_skills)
    suggested_available = [name for name in suggested if name in available_skills]
    suggestion = ", ".join(suggested_available) if suggested_available else "aucun"
    return (
        "4. Quelles competences veux-tu lui donner ?\n\n"
        f"{options}\n\n"
        f"Pour cette mission, je propose : `{suggestion}`.\n"
        "Reponds par des numeros, une liste de noms, `tous`, ou `ok` pour garder cette proposition."
    )


def _edit_skills_question(workspace: Path, current: list[str]) -> str:
    available_skills = _available_skills(workspace)
    current_available = [name for name in current if name in available_skills]
    current_label = ", ".join(current_available) if current_available else "aucune"
    options = _skill_options_text(available_skills, selected=current_available)
    return (
        f"Competences actuelles : `{current_label}`.\n\n"
        "Choisis les competences a activer :\n\n"
        f"{options}\n\n"
        "Reponds par des numeros (`1,2,4`), des noms, `tous`, `aucune`, "
        "ou `ok` pour garder les competences actuelles."
    )


def _skill_options_text(available_skills: dict[str, str], *, selected: list[str] | None = None) -> str:
    selected_set = set(selected or [])
    rows = []
    for index, (name, description) in enumerate(available_skills.items(), start=1):
        suffix = " (actuelle)" if name in selected_set else ""
        rows.append(f"{index}. `{name}`{suffix} : {description}")
    return "\n".join(rows)


def _model_question(model_source: dict[str, Any], choices: list[tuple[str, str]]) -> str:
    current = str(model_source.get("name") or "defaut")
    provider = str(model_source.get("provider") or "provider actuel")
    if not choices:
        return (
            "5. Quel modele veux-tu utiliser ?\n\n"
            f"- `defaut` : garder le modele actuel (`{current}`)\n"
            "- ou donne directement un identifiant de modele\n\n"
            f"Je n'ai pas trouve de liste automatique pour le provider `{provider}`."
        )
    rows = [f"0. `defaut` : garder le modele actuel (`{current}`)"]
    rows.extend(
        f"{index}. `{model_id}`" + (f" : {label}" if label and label != model_id else "")
        for index, (model_id, label) in enumerate(choices, start=1)
    )
    return (
        "5. Quel modele veux-tu utiliser ?\n\n"
        + "\n".join(rows)
        + "\n\nReponds par un numero, `defaut`, ou un identifiant de modele."
    )


def _telegram_question() -> str:
    return (
        "6. Tu veux connecter Telegram a cet agent ?\n\n"
        "- `aucun` : pas de Telegram\n"
        "- `telegram` : connecter un nouveau bot Telegram dedie a cet agent\n\n"
        "Reponds `aucun` ou `telegram`."
    )


def _telegram_users_question(workspace: Path) -> str:
    current = _current_telegram_users(workspace)
    current_line = ""
    if current:
        current_line = f"\n\nIDs deja autorises : `{', '.join(str(item) for item in current)}`. Reponds `ok` pour les garder."
    return (
        "Quels IDs Telegram peuvent utiliser ce bot ?\n\n"
        "Tu peux en mettre plusieurs, separes par des virgules. "
        "Si tu ne connais pas ton ID, envoie `/start` a @userinfobot ou @RawDataBot dans Telegram."
        f"{current_line}"
    )


def _summary_question(data: dict[str, Any]) -> str:
    skills = ", ".join(data.get("skills") or []) or "aucune"
    model = data.get("model")
    model_text = model.get("name") if isinstance(model, dict) else "defaut"
    telegram = "non"
    if data.get("telegram"):
        users = ", ".join(str(item) for item in data.get("telegram_allowed_users") or []) or "aucun ID"
        telegram = f"oui, bot `{data.get('telegram_credential') or 'telegram_bot'}`, IDs autorises : {users}"
    return (
        "Resume avant creation :\n\n"
        f"- Agent : `{data.get('id')}`\n"
        f"- Mission : {data.get('role')}\n"
        f"- Permissions : `{data.get('permission')}`\n"
        f"- Competences : {skills}\n"
        f"- Modele : {model_text}\n"
        f"- Telegram : {telegram}\n\n"
        "Je cree cet agent ? Reponds `oui` ou `non`."
    )


def _starts_agent_creation(normalized: str) -> bool:
    return any(marker in normalized for marker in START_MARKERS)


def _starts_agent_edit(normalized: str) -> bool:
    return _command_name(normalized) == "/edit_agent"


def _starts_telegram_edit(normalized: str) -> bool:
    if any(marker in normalized for marker in TELEGRAM_EDIT_MARKERS):
        return True
    return bool(
        re.search(r"\b(modifie|modifions|modifier|change|changer|configure|configurer)\b.*\b(bot|telegram)\b", normalized)
        or re.search(r"\b(bot|telegram)\b.*\b(modifie|modifions|modifier|change|changer|configure|configurer)\b", normalized)
    )


def _wants_keep_current(normalized: str) -> bool:
    if normalized in KEEP_WORDS:
        return True
    return "garde" in normalized and any(word in normalized for word in ("actuel", "actuelle", "etait", "était", "config"))


def _wants_change_skills(normalized: str) -> bool:
    if not any(word in normalized for word in SKILL_WORDS):
        return False
    return any(
        word in normalized
        for word in (
            "change",
            "changer",
            "modifie",
            "modifier",
            "edite",
            "editer",
            "éditer",
            "activer",
            "desactiver",
            "désactiver",
            "ajouter",
            "retirer",
        )
    )


def _command_name(normalized: str) -> str:
    first = normalized.split(maxsplit=1)[0] if normalized.split(maxsplit=1) else ""
    return first.split("@", 1)[0]


def _new_agent_edit_state(
    workspace: Path,
    *,
    requested_agent_id: str | None = None,
) -> AgentCreationState:
    if requested_agent_id is None:
        return AgentCreationState(step="agent_edit_agent_value", data={})
    return AgentCreationState(
        step="agent_edit_permission_change",
        data=_agent_edit_data_for(workspace, requested_agent_id),
    )


def _agent_edit_data_for(workspace: Path, agent_id: str) -> dict[str, Any]:
    bundle = load_workspace_config(workspace)
    agent = bundle.agents.agents[agent_id]
    telegram = _telegram_channel_for_agent(bundle.host.channels, agent_id)
    agent_telegram = isinstance(telegram, dict)
    model = agent.model if agent.model is not None else None
    return {
        "agent": agent.id,
        "permission": agent.permission_profile,
        "skills": list(agent.skills),
        "credentials": list(agent.credentials),
        "model": model,
        "clear_model": False,
        "telegram_enabled": agent_telegram,
        "credential": (
            str(telegram.get("credential") or _telegram_credential_for_agent(agent_id))
            if agent_telegram
            else _telegram_credential_for_agent(agent_id)
        ),
        "allowed_users": _int_values(telegram.get("allowed_users") if agent_telegram else []),
        "allowed_chats": _int_values(telegram.get("allowed_chats") if agent_telegram else []),
    }


def _agent_edit_intro_question(data: dict[str, Any]) -> str:
    if not data.get("agent"):
        return "Quel agent veux-tu modifier ? Donne son identifiant."
    return (
        f"Agent concerne : `{data.get('agent')}`.\n"
        "L'identifiant reste fixe pour ne pas casser ses dossiers.\n\n"
        + _agent_edit_permission_question(data)
    )


def _agent_edit_agent_value_question(workspace: Path) -> str:
    agents = ", ".join(sorted(load_workspace_config(workspace).agents.agents))
    return f"Quel agent veux-tu modifier ? Agents disponibles : {agents}."


def _agent_edit_permission_question(data: dict[str, Any]) -> str:
    return (
        f"Permissions actuelles : `{data.get('permission')}`.\n\n"
        "Changer les permissions ? Reponds `oui` ou `non`."
    )


def _agent_edit_skills_change_question(data: dict[str, Any]) -> str:
    skills = ", ".join(data.get("skills") or []) or "aucune"
    return (
        f"Competences actuelles : {skills}.\n\n"
        "Changer les competences ? Reponds `oui`, `non`, ou dis directement `changer les skills`."
    )


def _agent_edit_model_change_question(data: dict[str, Any], workspace: Path) -> str:
    label = _agent_edit_model_label(data, workspace)
    return f"Modele actuel : {label}.\n\nChanger le modele ? Reponds `oui` ou `non`."


def _agent_edit_telegram_change_question(data: dict[str, Any], workspace: Path) -> str:
    label = _agent_edit_telegram_label(data, workspace)
    return f"Telegram actuel : {label}.\n\nChanger la configuration Telegram ? Reponds `oui` ou `non`."


def _agent_edit_summary(data: dict[str, Any], workspace: Path) -> str:
    skills = ", ".join(data.get("skills") or []) or "aucune"
    return (
        "Resume avant modification :\n\n"
        f"- Agent : `{data.get('agent')}`\n"
        f"- Permissions : `{data.get('permission')}`\n"
        f"- Competences : {skills}\n"
        f"- Modele : {_agent_edit_model_label(data, workspace)}\n"
        f"- Telegram : {_agent_edit_telegram_label(data, workspace)}\n\n"
        "Appliquer ces modifications ? Reponds `oui` ou `non`."
    )


def _apply_agent_edit(workspace: Path, data: dict[str, Any]) -> None:
    agent_id = str(data["agent"])
    current = load_workspace_config(workspace).agents.agents[agent_id]
    credentials = list(dict.fromkeys([*current.credentials, *_credentials_for_agent(data)]))
    update_agent(
        workspace,
        agent_id=agent_id,
        permission_profile=str(data["permission"]),
        skills=list(data.get("skills") or []),
        credentials=credentials,
        model=data.get("model") if not data.get("clear_model") else None,
        clear_model=bool(data.get("clear_model")),
        confirmed_permission_elevation=True,
    )
    if data.get("telegram_changed"):
        _apply_telegram_edit(workspace, data)


def _agent_edit_model_label(data: dict[str, Any], workspace: Path) -> str:
    model = data.get("model")
    if isinstance(model, dict):
        return str(model.get("name") or model.get("provider") or "configure")
    if data.get("clear_model"):
        return "defaut"
    agent_id = data.get("agent")
    if isinstance(agent_id, str):
        bundle = load_workspace_config(workspace)
        agent = bundle.agents.agents.get(agent_id)
        if agent is not None and agent.model:
            return str(agent.model.get("name") or agent.model.get("provider") or "configure")
        return str(bundle.kernel.model.name)
    return "defaut"


def _agent_edit_telegram_label(data: dict[str, Any], workspace: Path) -> str:
    agent_id = str(data.get("agent") or "")
    credential = str(data.get("credential") or _telegram_credential_for_agent(agent_id))
    token = "configure" if credential in load_workspace_credentials(workspace).credentials else "manquant"
    if data.get("telegram_changed"):
        return (
            f"oui, token `{credential}` ({token}), "
            f"utilisateurs {_csv_ints(data.get('allowed_users') or [])}, "
            f"groupes {_csv_ints(data.get('allowed_chats') or [])}"
        )
    if not data.get("telegram_enabled"):
        return "non"
    return (
        f"oui, token `{credential}` ({token}), "
        f"utilisateurs {_csv_ints(data.get('allowed_users') or [])}, "
        f"groupes {_csv_ints(data.get('allowed_chats') or [])}"
    )


def _new_telegram_edit_state(
    workspace: Path,
    *,
    requested_agent_id: str | None = None,
) -> AgentCreationState:
    bundle = load_workspace_config(workspace)
    target = requested_agent_id or _default_telegram_edit_agent(workspace)
    telegram = _telegram_channel_for_agent(bundle.host.channels, target) or {}
    credential = str(telegram.get("credential") or _telegram_credential_for_agent(target))
    return AgentCreationState(
        step="edit_agent_change",
        data={
            "agent": target,
            "credential": credential,
            "allowed_users": _int_values(telegram.get("allowed_users") if isinstance(telegram, dict) else []),
            "allowed_chats": _int_values(telegram.get("allowed_chats") if isinstance(telegram, dict) else []),
        },
    )


def _extract_agent_reference(workspace: Path, normalized: str) -> str | None:
    agents = load_workspace_config(workspace).agents.agents
    for agent_id in sorted(agents, key=len, reverse=True):
        variants = {
            _normalize(agent_id),
            _normalize(agent_id.replace("_", " ")),
            _normalize(agent_id.replace("_", "-")),
        }
        if any(re.search(rf"(^|\W){re.escape(variant)}($|\W)", normalized) for variant in variants):
            return agent_id
    return None


def _default_telegram_edit_agent(workspace: Path) -> str:
    bundle = load_workspace_config(workspace)
    for channel in _telegram_channels(bundle.host.channels).values():
        configured = channel.get("agent")
        if isinstance(configured, str) and configured in bundle.agents.agents and configured != "main":
            return configured
    for candidate in bundle.agents.agents.values():
        if candidate.id != "main" and "telegram" in candidate.channels:
            return candidate.id
    for channel in _telegram_channels(bundle.host.channels).values():
        configured = channel.get("agent")
        if isinstance(configured, str) and configured in bundle.agents.agents:
            return configured
    return "main"


def _telegram_edit_agent_question(data: dict[str, Any], workspace: Path) -> str:
    agents = ", ".join(sorted(load_workspace_config(workspace).agents.agents))
    return (
        f"Agent concerne : `{data.get('agent')}`.\n"
        f"Agents disponibles : {agents}.\n\n"
        "Changer l'agent lie a ce bot ? Reponds `oui` ou `non`."
    )


def _telegram_edit_agent_value_question(workspace: Path) -> str:
    agents = ", ".join(sorted(load_workspace_config(workspace).agents.agents))
    return f"Quel agent doit utiliser ce bot Telegram ? Agents disponibles : {agents}."


def _telegram_edit_token_question(data: dict[str, Any], workspace: Path) -> str:
    credential = str(data.get("credential") or _telegram_credential_for_agent(str(data.get("agent") or "main")))
    configured = credential in load_workspace_credentials(workspace).credentials
    state = "configure" if configured else "manquant"
    return (
        f"Token du bot : credential `{credential}` ({state}).\n\n"
        "Changer ou renseigner le token ? Reponds `oui` ou `non`."
    )


def _telegram_edit_users_question(data: dict[str, Any]) -> str:
    users = _csv_ints(data.get("allowed_users") or [])
    return f"IDs utilisateurs autorises : {users}.\n\nChanger cette liste ? Reponds `oui` ou `non`."


def _telegram_edit_chats_question(data: dict[str, Any]) -> str:
    chats = _csv_ints(data.get("allowed_chats") or [])
    return f"IDs groupes/chats autorises : {chats}.\n\nChanger cette liste ? Reponds `oui` ou `non`."


def _telegram_edit_summary(data: dict[str, Any], workspace: Path) -> str:
    credential = str(data.get("credential") or _telegram_credential_for_agent(str(data.get("agent") or "main")))
    token_state = "configure" if credential in load_workspace_credentials(workspace).credentials else "manquant"
    return (
        "Resume de la modification Telegram :\n\n"
        f"- Agent : `{data.get('agent')}`\n"
        f"- Token : `{credential}` ({token_state})\n"
        f"- IDs utilisateurs : {_csv_ints(data.get('allowed_users') or [])}\n"
        f"- IDs groupes/chats : {_csv_ints(data.get('allowed_chats') or [])}\n\n"
        "Appliquer cette configuration ? Reponds `oui` ou `non`."
    )


def _apply_telegram_edit(workspace: Path, data: dict[str, Any]) -> None:
    agent_id = str(data.get("agent") or "main")
    credential = str(data.get("credential") or _telegram_credential_for_agent(agent_id))
    host_path = host_config_path(workspace)
    host_data = read_yaml_file(host_path)
    host = host_data.setdefault("host", {})
    channels = host.setdefault("channels", {})
    token_exists = credential in load_workspace_credentials(workspace).credentials
    channels[_telegram_channel_key(agent_id)] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": agent_id,
        "credential": credential,
        "allowed_users": _int_values(data.get("allowed_users") or []),
        "allowed_chats": _int_values(data.get("allowed_chats") or []),
        "status": "configured_pending_restart" if token_exists else "missing_credential",
    }
    write_yaml_file(host_path, host_data)
    for agent in list_agents(workspace):
        if agent.id == agent_id and "telegram" not in agent.channels:
            update_agent(workspace, agent_id=agent.id, channels=[*agent.channels, "telegram"])


def _telegram_credential_for_agent(agent_id: str) -> str:
    return "telegram_bot" if agent_id == "main" else f"telegram_bot_{agent_id}"


def _telegram_channel_key(agent_id: str) -> str:
    return "telegram" if agent_id == "main" else f"telegram_{agent_id}"


def _telegram_channels(channels: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: config
        for name, config in channels.items()
        if isinstance(config, dict)
        and (name == "telegram" or name.startswith("telegram_") or config.get("adapter") == "telegram")
        and config.get("adapter", "telegram") == "telegram"
    }


def _telegram_channel_for_agent(channels: dict[str, Any], agent_id: str) -> dict[str, Any] | None:
    direct = channels.get(_telegram_channel_key(agent_id))
    if isinstance(direct, dict):
        return direct
    for channel in _telegram_channels(channels).values():
        if channel.get("agent") == agent_id:
            return channel
    return None


def _csv_ints(values: object) -> str:
    items = _int_values(values)
    return ", ".join(str(item) for item in items) if items else "aucun"


def _int_values(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for item in values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _sanitize_agent_id(value: str) -> str:
    candidate = _normalize(value).replace("-", "_")
    candidate = re.sub(r"[^a-z0-9_]+", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if not candidate:
        return ""
    if not candidate[0].isalpha():
        candidate = f"agent_{candidate}"
    return candidate


def _suggest_skills(role: str) -> list[str]:
    normalized = _normalize(role)
    if any(word in normalized for word in ("devoir", "organisation", "generaliste", "polyvalent")):
        return ["filesystem", "memory", "web", "reminders"]
    return ["filesystem", "memory"]


def _available_skills(workspace: Path) -> dict[str, str]:
    discovered: dict[str, str] = dict(DEFAULT_SKILL_DESCRIPTIONS)
    roots: list[Path] = [_system_skills_root()]
    try:
        bundle = load_workspace_config(workspace)
        roots.extend(Path(root.path).expanduser() for root in bundle.host.skill_roots)
    except Exception:
        pass

    for root in roots:
        if not root.exists():
            continue
        for manifest_path in sorted(root.glob("*/skill.yaml")):
            try:
                manifest = SkillManifest.model_validate(read_yaml_file(manifest_path))
            except Exception:
                continue
            if "global" not in manifest.available_in:
                continue
            discovered.setdefault(
                manifest.name,
                DEFAULT_SKILL_DESCRIPTIONS.get(
                    manifest.name,
                    _compact_skill_description(manifest.description),
                ),
            )

    ordered: dict[str, str] = {}
    for name in DEFAULT_SKILL_DESCRIPTIONS:
        if name in discovered:
            ordered[name] = discovered[name]
    for name in sorted(set(discovered) - set(ordered)):
        ordered[name] = discovered[name]
    return ordered


def _system_skills_root() -> Path:
    return Path(__file__).resolve().parents[1] / "system_skills"


def _compact_skill_description(description: str) -> str:
    compacted = " ".join(str(description or "").split())
    if len(compacted) <= 90:
        return compacted or "competence disponible"
    return compacted[:87].rstrip() + "..."


def _parse_skills(value: str, suggested: list[str], workspace: Path) -> list[str] | None:
    available_skills = _available_skills(workspace)
    normalized = _normalize(value).strip("` ")
    if normalized in YES_WORDS or normalized in {"recommande", "recommandé", "recommandes"}:
        return [name for name in suggested if name in available_skills]
    if normalized in {"tous", "tout", "all"}:
        return list(available_skills)
    if normalized in {"aucun", "aucune", "none", "non"}:
        return []
    parts = [
        part.strip(" `.-")
        for part in re.split(r"[,;\n]+|\s+", normalized)
        if part.strip(" `.-")
    ]
    if not parts:
        return None
    skill_names = list(available_skills)
    selected: list[str] = []
    unknown: list[str] = []
    for part in parts:
        if part.isdigit():
            index = int(part)
            if 1 <= index <= len(skill_names):
                selected.append(skill_names[index - 1])
            else:
                unknown.append(part)
            continue
        if part in available_skills:
            selected.append(part)
        else:
            unknown.append(part)
    if unknown:
        return None
    return list(dict.fromkeys(selected))


def _parse_model_choice(value: str, choices: list[tuple[str, str]]) -> str | None:
    normalized = _normalize(value).strip("` ")
    if normalized in DEFAULT_WORDS or normalized == "0":
        return "__default__"
    if normalized.isdigit():
        index = int(normalized)
        if 1 <= index <= len(choices):
            return choices[index - 1][0]
        return None
    choice_ids = {model_id for model_id, _label in choices}
    if value.strip() in choice_ids:
        return value.strip()
    return None


def _model_choices_for_workspace(
    workspace: Path,
    *,
    agent_id: str | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    bundle = load_workspace_config(workspace)
    agent = bundle.agents.agents.get(agent_id) if agent_id else None
    model = dict(agent.model) if agent is not None and agent.model else bundle.kernel.model.model_dump(mode="json")
    provider = model.get("provider")
    protocol = model.get("protocol")
    if provider == "auth" and protocol == "chatgpt_codex":
        return model, chatgpt_model_choices()
    if provider == "ollama" or protocol == "ollama_chat":
        credential = str(model.get("credential") or "")
        api_key = ""
        if credential:
            credentials = load_workspace_credentials(workspace).credentials
            record = credentials.get(credential)
            api_key = record.value if record is not None else ""
        base_url = str(model.get("base_url") or "http://localhost:11434")
        return model, ollama_model_choices(base_url, api_key=api_key)
    return model, []


def _credentials_for_agent(data: dict[str, Any]) -> list[str]:
    model = data.get("model") if isinstance(data.get("model"), dict) else data.get("model_source")
    credentials: list[str] = []
    if isinstance(model, dict):
        credential = model.get("credential")
        if isinstance(credential, str) and credential.strip():
            credentials.append(credential.strip())
    return list(dict.fromkeys(credentials))


def _parse_telegram_ids(value: str) -> list[int] | None:
    normalized = _normalize(value).strip("` ")
    if normalized in {"aucun", "aucune", "none", "non"}:
        return []
    raw_parts = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
    if not raw_parts:
        return None
    ids: list[int] = []
    for part in raw_parts:
        try:
            ids.append(int(part))
        except ValueError:
            return None
    return list(dict.fromkeys(ids))


def _current_telegram_users(workspace: Path) -> list[int]:
    try:
        data = read_yaml_file(host_config_path(workspace))
    except Exception:
        return []
    channels = ((data.get("host") or {}).get("channels") or {})
    telegram = channels.get("telegram") or {}
    users = telegram.get("allowed_users") if isinstance(telegram, dict) else []
    if not isinstance(users, list):
        return []
    return [int(item) for item in users if isinstance(item, int)]


def _bind_telegram(workspace: Path, *, agent_id: str, credential: str, allowed_users: list[int]) -> None:
    host_path = host_config_path(workspace)
    data = read_yaml_file(host_path)
    host = data.setdefault("host", {})
    channels = host.setdefault("channels", {})
    channel_key = _telegram_channel_key(agent_id)
    previous = channels.get(channel_key) if isinstance(channels.get(channel_key), dict) else {}
    allowed_chats = previous.get("allowed_chats") if isinstance(previous, dict) else []
    channels[channel_key] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": agent_id,
        "credential": credential,
        "allowed_users": allowed_users,
        "allowed_chats": allowed_chats if isinstance(allowed_chats, list) else [],
        "status": "configured_pending_adapter",
    }
    write_yaml_file(host_path, data)
    for agent in list_agents(workspace):
        if agent.id == agent_id:
            if "telegram" not in agent.channels:
                update_agent(workspace, agent_id=agent.id, channels=[*agent.channels, "telegram"])


def _normalize(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _key(agent_id: str, session_id: str) -> str:
    return f"{agent_id}:{session_id}"


def _store_path(workspace: Path) -> Path:
    return workspace / "agents" / ".agent_creation_wizards.json"


def _load_store(workspace: Path) -> AgentWizardStore:
    path = _store_path(workspace)
    if not path.exists():
        return AgentWizardStore()
    try:
        return AgentWizardStore.model_validate(read_yaml_file(path))
    except Exception:
        return AgentWizardStore()


def _write_store(workspace: Path, store: AgentWizardStore) -> None:
    path = _store_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml_file(path, store.model_dump(mode="json"))
    path.chmod(0o600)


def _clear(workspace: Path, agent_id: str, session_id: str) -> None:
    store = _load_store(workspace)
    store.sessions.pop(_key(agent_id, session_id), None)
    _write_store(workspace, store)
