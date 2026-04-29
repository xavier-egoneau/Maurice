"""Dev command handlers."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from maurice.host.command_registry import CommandContext, CommandResult
from maurice.kernel.contracts import DreamInput
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.dev.planner import PLAN_WIZARD_FILE, start_plan_wizard


STATE_FILE = ".dev_state.json"
META_DIR = ".maurice"
GUIDANCE_FILES = ("AGENTS.md", "PLAN.md", "DECISIONS.md")


def projects(context: CommandContext) -> CommandResult:
    content = _content_root(context)
    project_dirs = sorted(path.name for path in content.iterdir() if path.is_dir() and not path.name.startswith("."))
    if not project_dirs:
        return _result(
            "Aucun projet pour cet agent.\n\n"
            "Cree un dossier dans son espace de contenu, ou lance `/project open monprojet` pour demarrer."
        )
    rows = "\n".join(f"{index}. `{name}`" for index, name in enumerate(project_dirs, start=1))
    active = _active_project(context)
    active_line = f"\n\nProjet actif : `{active}`." if active else ""
    return _result(f"Projets disponibles :\n\n{rows}{active_line}")


def project(context: CommandContext) -> CommandResult:
    args = _args(context)
    if not args:
        active = _active_project(context)
        if not active:
            return _result("Aucun projet actif. Utilise `/projects` puis `/project open <nom>`.")
        return _result(f"Projet actif : `{active}`.")
    if args.startswith("open "):
        name = _project_name(args.removeprefix("open ").strip())
        if not name:
            return _result("Donne un nom de projet simple : `/project open monprojet`.")
        path = _content_root(context) / name
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        _write_state(context, {"active_project": name})
        _ensure_project_files(path)
        status = "cree et ouvert" if created else "ouvert"
        return _result(f"Projet `{name}` {status}.\n\nDossier : `{path}`")
    return _result("Commande projet attendue : `/project open <nom>`.")


def plan(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    args = _args(context)
    if args in {"show", "affiche", "afficher", "read", "lis"}:
        plan_path = _guidance_path(path, "PLAN.md")
        if not plan_path.exists():
            _ensure_project_files(path)
        content = _read_excerpt(plan_path)
        return _result(f"Plan du projet `{path.name}` :\n\n```markdown\n{content}\n```")
    plan_path = _guidance_path(path, "PLAN.md")
    _ensure_project_files(path)
    text = start_plan_wizard(
        store_path=_plan_wizard_store_path(context),
        project_path=path,
        agent_id=context.agent_id,
        session_id=context.session_id,
        initial_expectations=args,
    )
    return _result(
        f"{text}\n\n"
        f"Le plan sera ecrit dans `{plan_path}`. "
        "Pour seulement le relire : `/plan show`."
    )


def tasks(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    plan_path = _guidance_path(path, "PLAN.md")
    if not plan_path.exists():
        _ensure_project_files(path)
    open_tasks = _open_tasks(plan_path)
    if not open_tasks:
        return _result("Aucune tache ouverte trouvee dans `PLAN.md`.")
    rows = "\n".join(f"{index}. {task}" for index, task in enumerate(open_tasks, start=1))
    return _result(f"Taches ouvertes :\n\n{rows}")


def decision(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    decisions_path = _guidance_path(path, "DECISIONS.md")
    text = _args(context)
    if text:
        _ensure_project_files(path)
        date = datetime.now().strftime("%Y-%m-%d")
        with decisions_path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n- {date} - {text}\n")
        return _result("Decision ajoutee dans `DECISIONS.md`.")
    if not decisions_path.exists():
        _ensure_project_files(path)
    return _result(f"Decisions du projet `{path.name}` :\n\n```markdown\n{_read_excerpt(decisions_path)}\n```")


def dev(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    plan_path = _guidance_path(path, "PLAN.md")
    if not plan_path.exists():
        _ensure_project_files(path)
    open_tasks = _open_tasks(plan_path)
    if not open_tasks:
        return _result("Je ne vois pas de prochaine tache claire dans `PLAN.md`.")
    return _result(
        "Je lance le mode dev autonome sur le plan actif.",
        metadata={
            "command": "/dev",
            "agent_prompt": _dev_execution_prompt(path, open_tasks),
            "agent_limits": {"max_tool_iterations": 80, "allow_text_tool_calls": True},
            "autonomy": {
                "requires_activity": True,
                "continue_without_activity": True,
                "max_continuations": 120,
                "max_seconds": 3600,
                "continue_prompt": (
                    "Continue le mode dev. Tu ne dois pas seulement annoncer la prochaine action : "
                    "avance concretement dans le projet actif, utilise les capacites disponibles, "
                    "puis resume ce qui a vraiment change. Si tu es bloque, donne le blocage precis."
                ),
                "no_activity_text": (
                    "Je n'arrive pas a coder avec le modele actuel : il annonce des actions mais "
                    "n'utilise aucune capacite pour lire ou modifier les fichiers. Il faut passer sur "
                    "un modele/provider capable d'utiliser les outils, ou traiter cette phase depuis la CLI."
                ),
            },
        },
    )


def check(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    meta = _meta_root(path)
    files = [".gitignore", *GUIDANCE_FILES, "dreams.md"]
    states = [f"- `.maurice/{name}` : {'ok' if (meta / name).exists() else 'manquant'}" for name in files]
    return _result("Verification projet :\n\n" + "\n".join(states))


def review(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    _ensure_project_files(path)
    plan_path = _guidance_path(path, "PLAN.md")
    decisions_path = _guidance_path(path, "DECISIONS.md")
    open_tasks = _open_tasks(plan_path)
    done_tasks = _done_tasks(plan_path)
    meta = _meta_root(path)
    missing = [name for name in GUIDANCE_FILES if not (meta / name).exists()]
    git_status = _git_status(path)

    lines = [
        f"Review du projet `{path.name}`",
        "",
        "## Cadrage",
        "",
        f"- `.maurice/AGENTS.md` : {'manquant' if 'AGENTS.md' in missing else 'ok'}",
        f"- `.maurice/PLAN.md` : {'manquant' if 'PLAN.md' in missing else 'ok'}",
        f"- `.maurice/DECISIONS.md` : {'manquant' if 'DECISIONS.md' in missing else 'ok'}",
        f"- Taches ouvertes : {len(open_tasks)}",
        f"- Taches terminees : {len(done_tasks)}",
    ]
    if open_tasks:
        lines.extend(["", "## Prochaines taches", ""])
        lines.extend(f"{index}. {task}" for index, task in enumerate(open_tasks[:5], start=1))
    if decisions_path.exists():
        decisions = _recent_decisions(decisions_path)
        if decisions:
            lines.extend(["", "## Decisions recentes", ""])
            lines.extend(f"- {decision}" for decision in decisions)
    lines.extend(["", "## Etat Git", ""])
    lines.extend(git_status)
    lines.extend(
        [
            "",
            "## Avis",
            "",
            _review_advice(open_tasks=open_tasks, git_status=git_status),
        ]
    )
    return _result("\n".join(lines))


def commit(context: CommandContext) -> CommandResult:
    path = _active_project_path(context)
    if path is None:
        return _no_project()
    return _result(
        f"Projet actif : `{path.name}`.\n\n"
        "Preparation de commit a venir avec l'integration git du skill dev."
    )


def _content_root(context: CommandContext) -> Path:
    agent_workspace_for = context.callbacks.get("agent_workspace_for")
    if callable(agent_workspace_for):
        agent_workspace = agent_workspace_for(context.agent_id)
    else:
        agent_workspace = context.callbacks.get("agent_workspace")
    if agent_workspace is None:
        workspace = Path(context.callbacks.get("workspace") or ".").expanduser().resolve()
        agent_workspace = workspace / "agents" / context.agent_id
    content = Path(agent_workspace).expanduser().resolve() / "content"
    content.mkdir(parents=True, exist_ok=True)
    return content


def _state_path(context: CommandContext) -> Path:
    return _content_root(context).parent / STATE_FILE


def _plan_wizard_store_path(context: CommandContext) -> Path:
    return _content_root(context).parent / PLAN_WIZARD_FILE


def _read_state(context: CommandContext) -> dict[str, str]:
    path = _state_path(context)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(context: CommandContext, data: dict[str, str]) -> None:
    path = _state_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _active_project(context: CommandContext) -> str:
    value = _read_state(context).get("active_project")
    return value if isinstance(value, str) else ""


def _active_project_path(context: CommandContext) -> Path | None:
    active = _active_project(context)
    if not active:
        return None
    path = _content_root(context) / active
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_project_files(path: Path) -> None:
    meta = _meta_root(path)
    meta.mkdir(parents=True, exist_ok=True)
    files = {
        ".gitignore": "*\n",
        "AGENTS.md": (
            "# Agent Rules\n\n"
            "- Work in the project folder unless the user explicitly names another location.\n"
            "- Treat this `.maurice/` directory as Maurice's local project memory.\n"
            "- Keep user-facing code and files outside `.maurice/`.\n"
            "- Ask before destructive changes, secret handling, dependency installs, or permission escalation.\n"
            "- After each task, verify the change, inspect impacts, and remove dead code when relevant.\n"
        ),
        "PLAN.md": (
            "# Plan\n\n"
            "## Taches\n\n"
            "- [ ] Definir la prochaine tache concrete. [ non parallellisable ]\n"
        ),
        "DECISIONS.md": "# Decisions\n\n",
        "dreams.md": (
            "# Dev Dream Signals\n\n"
            "- Review `.maurice/PLAN.md` for stale open tasks, blocked work, or tasks that can be parallelized.\n"
            "- Review `.maurice/DECISIONS.md` for architectural drift or decisions that need follow-up.\n"
        ),
    }
    for name, content in files.items():
        target = meta / name
        if not target.exists():
            legacy = path / name
            if name in GUIDANCE_FILES and legacy.exists():
                target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
                legacy.unlink()
            else:
                target.write_text(content, encoding="utf-8")


def _meta_root(project_path: Path) -> Path:
    return project_path / META_DIR


def _guidance_path(project_path: Path, name: str) -> Path:
    return _meta_root(project_path) / name


def _open_tasks(plan_path: Path) -> list[str]:
    if not plan_path.exists():
        return []
    tasks = []
    for line in plan_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            tasks.append(stripped.removeprefix("- [ ]").strip())
    return tasks


def _done_tasks(plan_path: Path) -> list[str]:
    if not plan_path.exists():
        return []
    tasks = []
    for line in plan_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            tasks.append(stripped[5:].strip())
    return tasks


def _is_parallelizable_task(task: str) -> bool:
    normalized = task.lower()
    return "[ paralle" in normalized and "non paralle" not in normalized


def _recent_decisions(decisions_path: Path, *, limit: int = 5) -> list[str]:
    if not decisions_path.exists():
        return []
    decisions = [
        line.strip().removeprefix("- ").strip()
        for line in decisions_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ")
    ]
    return decisions[-limit:]


def _git_status(path: Path) -> list[str]:
    if not _inside_git_repo(path):
        return ["- Pas de depot Git detecte pour ce projet."]
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["- Etat Git indisponible."]
    if result.returncode != 0:
        return ["- Etat Git indisponible."]
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return ["- Aucun changement Git detecte."]
    return ["```text", *rows[:80], "```"]


def _inside_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _review_advice(*, open_tasks: list[str], git_status: list[str]) -> str:
    if open_tasks:
        return f"Prochaine action conseillee : traiter `{open_tasks[0]}` puis relancer `/check`."
    if any("Aucun changement Git" in line for line in git_status):
        return "Le cadrage est present et je ne vois pas de changement Git. Cree ou choisis une prochaine tache dans `PLAN.md`."
    return "Des changements existent. Relis-les, lance les tests utiles, puis utilise `/commit` quand c'est pret."


def _read_excerpt(path: Path, limit: int = 2400) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if len(content) <= limit:
        return content
    return content[: limit - 3].rstrip() + "..."


def _args(context: CommandContext) -> str:
    parts = context.message_text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _project_name(value: str) -> str:
    normalized = value.strip().strip("`").replace(" ", "_")
    if not re.match(r"^[A-Za-z0-9_.-]+$", normalized):
        return ""
    if normalized in {".", ".."}:
        return ""
    return normalized


def _no_project() -> CommandResult:
    return _result("Aucun projet actif. Utilise `/projects` puis `/project open <nom>`.")


def _dev_execution_prompt(project_path: Path, open_tasks: list[str]) -> str:
    meta = _meta_root(project_path)
    plan_path = meta / "PLAN.md"
    decisions_path = meta / "DECISIONS.md"
    agents_path = meta / "AGENTS.md"
    dreams_path = meta / "dreams.md"
    task_lines = "\n".join(f"- [ ] {task}" for task in open_tasks)
    return (
        "Commande interne `/dev` : execute le plan de developpement en autonomie maximale.\n\n"
        f"Projet actif : {project_path}\n"
        f"Memoire Maurice : {meta}\n"
        f"Regles locales : {agents_path}\n"
        f"Decisions : {decisions_path}\n"
        f"Plan : {plan_path}\n"
        f"Dream signals : {dreams_path}\n\n"
        "Objectif : avancer dans l'ordre de `.maurice/PLAN.md` jusqu'a finir le plan, rencontrer un blocage, "
        "ou avoir besoin d'un test/choix utilisateur.\n\n"
        "Taches ouvertes actuelles :\n"
        f"{task_lines}\n\n"
        "Regles d'execution :\n"
        "- Tu es en mode developpement : avance concretement, pas seulement en commentaire.\n"
        "- Utilise les outils disponibles quand ils sont utiles pour lire, ecrire, verifier ou inspecter le projet.\n"
        "- Commence par les taches `[ non parallellisable ]`, dans l'ordre.\n"
        "- Pour les taches `[ parallellisable ]`, si un outil de spawn/worker est disponible, delegue avec un contexte autonome et continue; sinon execute sequentiellement.\n"
        "- Chaque worker doit recevoir le contexte minimal complet : projet, fichiers pertinents, objectifs, contraintes, sortie attendue.\n"
        "- Apres chaque tache : verifie le code, cherche les impacts, nettoie le code mort, puis coche la tache dans `.maurice/PLAN.md`.\n"
        "- Note toute decision structurante dans `.maurice/DECISIONS.md`.\n"
        "- Mets a jour `.maurice/dreams.md` si tu decouvres des signaux utiles au dreaming.\n"
        "- Stoppe et demande a l'utilisateur uniquement si une information manque, une autorisation est necessaire, ou un test manuel est indispensable.\n\n"
        "Contexte AGENTS.md :\n"
        f"```markdown\n{_read_excerpt(agents_path, limit=1800)}\n```\n\n"
        "Contexte DECISIONS.md :\n"
        f"```markdown\n{_read_excerpt(decisions_path, limit=1800)}\n```\n\n"
        "Contexte PLAN.md :\n"
        f"```markdown\n{_read_excerpt(plan_path, limit=5000)}\n```\n"
    )


def _result(text: str, *, metadata: dict[str, object] | None = None) -> CommandResult:
    return CommandResult(text=text, format="markdown", metadata=metadata or {})


def build_dream_input(context: PermissionContext, *, limit: int = 12) -> DreamInput:
    now = datetime.now(UTC)
    content_root = Path(context.variables()["$agent_content"])
    signals = []
    if content_root.exists():
        for project in sorted(path for path in content_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            meta = _meta_root(project)
            plan_path = meta / "PLAN.md"
            decisions_path = meta / "DECISIONS.md"
            if not plan_path.exists() and not decisions_path.exists():
                continue
            open_tasks = _open_tasks(plan_path)
            done_tasks = _done_tasks(plan_path)
            recent_decisions = _recent_decisions(decisions_path, limit=3)
            summary_parts = [
                f"Project {project.name}: {len(open_tasks)} open task(s), {len(done_tasks)} done task(s)."
            ]
            if open_tasks:
                summary_parts.append(f"Next: {open_tasks[0]}")
            if recent_decisions:
                summary_parts.append(f"Recent decisions: {'; '.join(recent_decisions)}")
            signals.append(
                {
                    "id": f"sig_dev_{project.name}",
                    "type": "dev_project_review",
                    "summary": " ".join(summary_parts),
                    "data": {
                        "project": project.name,
                        "plan_path": str(plan_path),
                        "decisions_path": str(decisions_path),
                        "open_tasks": open_tasks[:5],
                        "done_count": len(done_tasks),
                        "recent_decisions": recent_decisions,
                    },
                }
            )
            if len(signals) >= limit:
                break
    return DreamInput(
        skill="dev",
        trust="local_mutable",
        freshness={"generated_at": now, "expires_at": None},
        signals=signals,
        limits=["Includes project `.maurice/PLAN.md` and `.maurice/DECISIONS.md` summaries only."],
    )
