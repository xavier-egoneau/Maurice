"""Conversational project planning flow for the dev skill."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from maurice.kernel.contracts import MauriceModel


PLAN_WIZARD_FILE = ".dev_plan_wizards.json"
CANCEL_WORDS = {"annule", "annuler", "stop", "cancel", "abandonne"}
NONE_WORDS = {"aucun", "aucune", "non", "none", "rien"}
YES_WORDS = {"oui", "ok", "go", "yes", "y", "valide", "validé", "valider"}


class PlanWizardState(MauriceModel):
    step: str = "expectations"
    project_path: str
    project_name: str
    data: dict[str, str] = Field(default_factory=dict)


class PlanWizardStore(MauriceModel):
    sessions: dict[str, PlanWizardState] = Field(default_factory=dict)



def handle_plan_wizard(
    *,
    store_path: Path,
    agent_id: str,
    session_id: str,
    text: str,
) -> str | None:
    store = _load_store(store_path)
    key = _key(agent_id, session_id)
    state = store.sessions.get(key)
    if state is None:
        return None

    normalized = _normalize(text)
    if normalized in CANCEL_WORDS:
        store.sessions.pop(key, None)
        _write_store(store_path, store)
        return "Creation du plan annulee."

    if state.step == "expectations":
        value = text.strip()
        if not value:
            return _expectations_question(state)
        state.data["expectations"] = value
        state.step = "audience"
        store.sessions[key] = state
        _write_store(store_path, store)
        return _audience_question(state)

    if state.step == "audience":
        state.data["audience"] = _none_to_default(text, "Usage personnel ou interne.")
        state.step = "constraints"
        store.sessions[key] = state
        _write_store(store_path, store)
        return _constraints_question(state)

    if state.step == "constraints":
        state.data["constraints"] = _none_to_default(text, "Aucune contrainte particuliere indiquee.")
        state.step = "done_definition"
        store.sessions[key] = state
        _write_store(store_path, store)
        return _done_definition_question(state)

    if state.step == "done_definition":
        state.data["done_definition"] = _none_to_default(text, "Le resultat fonctionne et peut etre teste simplement.")
        state.step = "confirm"
        store.sessions[key] = state
        _write_store(store_path, store)
        return _proposal_question(state)

    if state.step == "confirm":
        if normalized in YES_WORDS:
            project_path = Path(state.project_path)
            meta_path = project_path / ".maurice"
            meta_path.mkdir(parents=True, exist_ok=True)
            plan_path = meta_path / "PLAN.md"
            decisions_path = meta_path / "DECISIONS.md"
            plan_path.write_text(_plan_markdown(state), encoding="utf-8")
            decisions_path.write_text(_decisions_markdown(state), encoding="utf-8")
            store.sessions.pop(key, None)
            _write_store(store_path, store)
            return (
                f"Plan cree dans `{plan_path}`.\n"
                f"Decisions ecrites dans `{decisions_path}`.\n\n"
                "Prochaine etape utile : `/tasks` pour relire les taches, puis `/dev` pour avancer."
            )
        state.data["adjustments"] = text.strip()
        state.step = "adjust"
        store.sessions[key] = state
        _write_store(store_path, store)
        return (
            "Ok, qu'est-ce qu'on ajuste ? Donne-moi les changements attendus, "
            "puis je repropose un plan."
        )

    if state.step == "adjust":
        state.data["adjustments"] = text.strip()
        state.step = "confirm"
        store.sessions[key] = state
        _write_store(store_path, store)
        return _proposal_question(state)

    state.step = "expectations"
    store.sessions[key] = state
    _write_store(store_path, store)
    return _expectations_question(state)


def clear_plan_wizard(*, store_path: Path, agent_id: str, session_id: str) -> None:
    store = _load_store(store_path)
    store.sessions.pop(_key(agent_id, session_id), None)
    _write_store(store_path, store)


def _expectations_question(state: PlanWizardState) -> str:
    return (
        f"On cadre le plan du projet `{state.project_name}`.\n\n"
        "Pitch le projet ou la feature avec tes mots : qu'est-ce que tu veux obtenir ?"
    )


def _audience_question(state: PlanWizardState) -> str:
    return (
        "Bien recu.\n\n"
        "Pour qui ou pour quel usage principal est ce projet ? "
        "Tu peux repondre `aucun` si ce n'est pas important."
    )


def _constraints_question(state: PlanWizardState) -> str:
    return (
        "Quelles contraintes je dois prendre en compte ? "
        "Exemples : techno imposee, temps, securite, UX, budget, donnees. "
        "Tu peux repondre `aucun`."
    )


def _done_definition_question(state: PlanWizardState) -> str:
    return (
        "Derniere question : comment saura-t-on que c'est reussi ? "
        "Donne 1 a 3 criteres de validation."
    )


def _proposal_question(state: PlanWizardState) -> str:
    return (
        "Voici ma proposition avant ecriture :\n\n"
        + _critique_markdown(state)
        + "\n\n"
        + _approach_markdown(state)
        + "\n\n"
        + _tasks_markdown(state)
        + "\n\nTu valides ? Reponds `oui` pour ecrire `.maurice/DECISIONS.md` et `.maurice/PLAN.md`, "
        "ou indique ce qu'il faut ajuster."
    )


def _plan_markdown(state: PlanWizardState) -> str:
    data = state.data
    return (
        "# Plan\n\n"
        "## Objectif\n\n"
        f"{data.get('expectations', '').strip()}\n\n"
        "## Usage vise\n\n"
        f"{data.get('audience', '').strip()}\n\n"
        "## Contraintes\n\n"
        f"{data.get('constraints', '').strip()}\n\n"
        "## Definition de fini\n\n"
        f"{data.get('done_definition', '').strip()}\n\n"
        "## Critique\n\n"
        + _critique_markdown(state).removeprefix("## Critique\n\n")
        + "\n\n"
        "## Approche\n\n"
        + _approach_markdown(state).removeprefix("## Approche\n\n")
        + "\n\n"
        "## Taches\n\n"
        + _tasks_markdown(state).removeprefix("## Taches\n\n")
        + "\n"
    )


def _decisions_markdown(state: PlanWizardState) -> str:
    data = state.data
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        "# Decisions\n\n"
        f"- {today} - Objectif retenu : {data.get('expectations', '').strip()}\n"
        f"- {today} - Usage vise : {data.get('audience', '').strip()}\n"
        f"- {today} - Contraintes a respecter : {data.get('constraints', '').strip()}\n"
        f"- {today} - Approche initiale : avancer par taches cochees, avec tag `[ parallellisable ]` ou `[ non parallellisable ]`.\n"
    )


def _critique_markdown(state: PlanWizardState) -> str:
    data = state.data
    constraints = data.get("constraints", "").strip()
    adjustments = data.get("adjustments", "").strip()
    points = [
        "Le risque principal est de partir trop large : il faut garder un premier parcours testable.",
        "Les choix techniques doivent suivre le projet existant avant d'ajouter une nouvelle stack.",
        "Chaque tache doit produire un resultat verifiable, pas seulement du cadrage.",
    ]
    if constraints and "secur" in _normalize(constraints):
        points.append("La securite doit etre traitee tot : secrets, permissions, surfaces d'entree.")
    if adjustments:
        points.append(f"Ajustement utilisateur a integrer : {adjustments}")
    return "## Critique\n\n" + "\n".join(f"- {point}" for point in points)


def _approach_markdown(state: PlanWizardState) -> str:
    data = state.data
    constraints = data.get("constraints", "").strip()
    stack = (
        "Stack : conserver la stack existante si le dossier contient deja un projet ; sinon choisir la stack minimale adaptee au besoin."
    )
    if constraints:
        stack += f" Contraintes a verifier : {constraints}"
    return (
        "## Approche\n\n"
        "- Lire la structure actuelle avant de coder.\n"
        "- Formaliser les decisions qui engagent le projet dans `.maurice/DECISIONS.md`.\n"
        "- Avancer par petites taches ordonnees dans `.maurice/PLAN.md`.\n"
        f"- {stack}"
    )


def _tasks_markdown(state: PlanWizardState) -> str:
    return (
        "## Taches\n\n"
        "- [ ] Lire l'existant, identifier la stack et les conventions du projet. [ non parallellisable ]\n"
        "- [ ] Preciser le perimetre du premier parcours testable. [ non parallellisable ]\n"
        "- [ ] Mettre en place ou ajuster la structure minimale necessaire. [ non parallellisable ]\n"
        "- [ ] Implementer le parcours principal de bout en bout. [ non parallellisable ]\n"
        "- [ ] Ajouter les tests ou checks utiles autour du parcours. [ parallellisable ]\n"
        "- [ ] Relire l'UX, les messages, la documentation et le code mort. [ parallellisable ]\n"
    )


def _load_store(path: Path) -> PlanWizardStore:
    if not path.exists():
        return PlanWizardStore()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PlanWizardStore()
    if not isinstance(payload, dict):
        return PlanWizardStore()
    return PlanWizardStore.model_validate(payload)


def _write_store(path: Path, store: PlanWizardStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")


def _key(agent_id: str, session_id: str) -> str:
    return f"{agent_id}:{session_id}"


def _normalize(text: str) -> str:
    return text.strip().lower().strip("` ")


def _none_to_default(text: str, default: str) -> str:
    value = text.strip()
    return default if _normalize(value) in NONE_WORDS or not value else value
