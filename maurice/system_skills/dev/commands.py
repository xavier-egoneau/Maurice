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
from maurice.system_skills.dev.planner import PLAN_WIZARD_FILE


STATE_FILE = ".dev_state.json"
META_DIR = ".maurice"
GUIDANCE_FILES = ("AGENTS.md", "PLAN.md", "DECISIONS.md")


def projects(context: CommandContext) -> CommandResult:
    # CLI mode: the current directory IS the project
    if context.callbacks.get("project_root"):
        project_root = Path(context.callbacks["project_root"]).resolve()
        return _result(f"Projet actif : `{project_root.name}`\n\n`{project_root}`")
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
    _ensure_project_files(path)
    meta = _meta_root(path)
    plan_path = meta / "PLAN.md"
    decisions_path = meta / "DECISIONS.md"
    pitch = args.strip()
    return _result(
        f"Je cadre le projet `{path.name}`...",
        metadata={
            "command": "/plan",
            "agent_prompt": _plan_agent_prompt(path, pitch, plan_path, decisions_path),
            "agent_limits": {"max_tool_iterations": 8},
        },
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
    if not _inside_git_repo(path):
        return _result(
            f"Le projet `{path.name}` n'est pas dans un depot Git.\n\n"
            "Initialise un depot avec `git init` dans le dossier projet."
        )
    status = _git_short_status(path)
    if not status:
        return _result(f"Rien a commiter dans `{path.name}`. Le depot est propre.")
    diff_stat = _git_diff_stat(path)
    plan_path = _guidance_path(path, "PLAN.md")
    done_tasks = _done_tasks(plan_path) if plan_path.exists() else []
    context_block = "\n".join([
        f"Projet : {path.name}",
        f"Racine Git : {path}",
        "",
        "Fichiers modifies :",
        status,
        "",
        f"Stats : {diff_stat}" if diff_stat else "",
    ]).strip()
    tasks_block = ""
    if done_tasks:
        tasks_block = "\nTaches recemment cochees dans PLAN.md :\n" + "\n".join(f"- {t}" for t in done_tasks[-5:])
    return _result(
        f"Preparation du commit pour `{path.name}`.",
        metadata={
            "command": "/commit",
            "agent_prompt": (
                "Commande interne `/commit` : prepare et execute un commit Git pour le projet actif.\n\n"
                f"{context_block}{tasks_block}\n\n"
                "Instructions :\n"
                "1. Redige un message de commit concis en anglais (max 72 chars pour la premiere ligne).\n"
                "   Format : `type: sujet court` ou `type(scope): sujet court`.\n"
                "   Types acceptes : feat, fix, refactor, docs, test, chore.\n"
                "   La description doit exprimer le POURQUOI, pas seulement le quoi.\n"
                "2. Utilise `shell.exec` pour executer :\n"
                f"   `git -C {path} add -A`\n"
                f"   `git -C {path} commit -m \"<ton message>\"`\n"
                "3. Reporte le resultat du commit (hash, fichiers, message utilise).\n"
                "4. Si le commit echoue (hooks, rien a commiter, erreur), explique pourquoi et propose la correction.\n\n"
                "Ne coche pas les taches dans PLAN.md — elles sont deja cochees si elles figurent dans la liste ci-dessus."
            ),
            "agent_limits": {"max_tool_iterations": 6},
        },
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
    # CLI mode: project_root callback points directly to the working directory
    project_root = context.callbacks.get("project_root")
    if project_root:
        path = Path(project_root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
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


def _git_short_status(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--short"],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_diff_stat(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "diff", "--cached", "--stat", "HEAD"],
            check=False, capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            result = subprocess.run(
                ["git", "-C", str(path), "diff", "--stat"],
                check=False, capture_output=True, text=True, timeout=3,
            )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = result.stdout.strip().splitlines()
    return lines[-1].strip() if lines else ""


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


def _plan_agent_prompt(
    project_path: Path,
    pitch: str,
    plan_path: Path,
    decisions_path: Path,
) -> str:
    existing_plan = _read_excerpt(plan_path, limit=1200) if plan_path.exists() else ""
    existing_decisions = _read_excerpt(decisions_path, limit=600) if decisions_path.exists() else ""
    pitch_block = f'Pitch utilisateur : "{pitch}"' if pitch else "Aucun pitch fourni — demande a l'utilisateur ce qu'il veut faire."
    existing_block = ""
    if existing_plan:
        existing_block = f"\nPlan existant :\n```markdown\n{existing_plan}\n```\n"
    if existing_decisions:
        existing_block += f"\nDecisions existantes :\n```markdown\n{existing_decisions}\n```\n"

    return (
        "Commande interne `/plan` : cadre le projet et ecris le plan de developpement.\n\n"
        f"Projet : {project_path.name}\n"
        f"Dossier : {project_path}\n"
        f"Fichier plan : {plan_path}\n"
        f"Fichier decisions : {decisions_path}\n"
        f"{pitch_block}\n"
        f"{existing_block}\n"
        "Ta mission en 4 etapes :\n\n"
        "1. **Critique** : identifie les risques, les zones floues, les choix techniques implicites.\n"
        "   Sois precis — pas de critique generique. Adapte-toi au pitch et au contexte du projet.\n\n"
        "2. **Approche** : propose une approche concrete et une stack technique adaptee.\n"
        "   Si le projet a deja une stack, lis les fichiers existants avant de proposer.\n"
        "   Justifie brievement les choix techniques importants.\n\n"
        "3. **Plan** : propose des taches ordonnees logiquement.\n"
        "   Chaque tache doit :\n"
        "   - etre concrete et produire un resultat verifiable\n"
        "   - etre marquee `[ parallellisable ]` si elle peut se faire en parallele d'une autre\n"
        "   - etre marquee `[ non parallellisable ]` si elle bloque les suivantes\n"
        "   Format : `- [ ] Description de la tache. [ parallellisable ]`\n\n"
        "4. **Validation** : presente ta critique, ton approche et tes taches a l'utilisateur.\n"
        "   Demande : 'Tu valides ? Reponds `oui` pour ecrire les fichiers ou indique ce qu'il faut ajuster.'\n"
        "   Si l'utilisateur valide :\n"
        f"   - Ecris `{plan_path}` avec la structure : # Plan > ## Objectif > ## Approche > ## Taches\n"
        f"   - Ecris `{decisions_path}` avec la structure : # Decisions > une ligne par decision cle\n"
        "   Si l'utilisateur demande des ajustements, integre-les et represente.\n\n"
        "Ne cree pas les fichiers avant la validation explicite de l'utilisateur."
    )


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
