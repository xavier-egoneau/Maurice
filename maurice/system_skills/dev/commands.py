"""Dev command handlers."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from maurice.host.command_registry import CommandContext, CommandResult
from maurice.host.project_registry import (
    known_project_by_name,
    list_known_projects,
    list_machine_projects,
    record_seen_project,
)
from maurice.kernel.contracts import DreamInput
from maurice.kernel.permissions import PermissionContext


STATE_FILE = ".dev_state.json"
META_DIR = ".maurice"
GUIDANCE_FILES = ("AGENTS.md", "PLAN.md", "DECISIONS.md")
DREAM_PROJECT_FILES = ("AGENTS.md", "PLAN.md", "DECISIONS.md", "dreams.md")


def projects(context: CommandContext) -> CommandResult:
    active_path = _explicit_active_project_path(context)
    if active_path is not None:
        _remember_active_project(context, active_path)
    content = _content_root(context)
    project_dirs = sorted(path.name for path in content.iterdir() if path.is_dir() and not path.name.startswith("."))
    known_projects = _known_project_rows(context)
    if active_path is None and not project_dirs and not known_projects:
        return _result(
            "Aucun projet pour cet agent.\n\n"
            "Cree un dossier dans son espace de contenu, ou lance `/project open monprojet` pour demarrer."
        )
    sections = []
    if active_path is not None:
        sections.append(f"Projet actif : `{active_path.name}`\n\n`{active_path}`")
    if project_dirs:
        rows = "\n".join(f"{index}. `{name}`" for index, name in enumerate(project_dirs, start=1))
        sections.append(f"Projets disponibles :\n\n{rows}")
    if known_projects:
        sections.append("Projets deja vus :\n\n" + "\n".join(known_projects))
    active = _active_project(context)
    active_line = f"\n\nProjet actif : `{active}`." if active and active_path is None else ""
    return _result("\n\n".join(sections) + active_line)


def project(context: CommandContext) -> CommandResult:
    args = _args(context)
    active_path = _explicit_active_project_path(context)
    if not args:
        if active_path is not None:
            return _result(f"Projet actif : `{active_path.name}`.\n\n`{active_path}`")
        active = _active_project(context)
        if not active:
            return _result("Aucun projet actif. Utilise `/projects` puis `/project open <nom>`.")
        return _result(f"Projet actif : `{active}`.")
    if active_path is not None:
        return _result(
            f"Le contexte courant est deja centre sur `{active_path.name}`.\n\n"
            f"Dossier : `{active_path}`"
        )
    if args.startswith("open "):
        name = _project_name(args.removeprefix("open ").strip())
        if not name:
            return _result("Donne un nom de projet simple : `/project open monprojet`.")
        known_path = known_project_by_name(_agent_workspace(context), name)
        content_project = _content_root(context) / name
        if known_path is not None and not content_project.exists():
            known_path.mkdir(parents=True, exist_ok=True)
            _write_state(context, {"active_project_path": str(known_path)})
            _ensure_project_files(known_path)
            return _result(f"Projet `{name}` ouvert.\n\nDossier : `{known_path}`")
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
    pitch = args.strip()
    plan_path = _guidance_path(path, "PLAN.md")
    decisions_path = _guidance_path(path, "DECISIONS.md")
    return _result(
        f"Je lance le cadrage du plan du projet `{path.name}` avec le modele.",
        metadata={
            "command": "/plan",
            "agent_prompt": _plan_agent_prompt(path, pitch, plan_path, decisions_path),
            "agent_limits": {"max_tool_iterations": 20, "allow_text_tool_calls": True},
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
    focus = _args(context).strip()
    if focus:
        _replace_plan_for_focus(plan_path, focus)
    open_tasks = _open_tasks(plan_path)
    if not open_tasks:
        return _result("Je ne vois pas de prochaine tache claire dans `PLAN.md`.")
    merge_target_branch = _remember_dev_merge_target(context, path, open_tasks, focus=focus)
    return _result(
        "Je lance le mode dev autonome sur le plan actif.",
        metadata={
            "command": "/dev",
            "agent_prompt": _dev_execution_prompt(
                path,
                open_tasks,
                focus=focus,
                merge_target_branch=merge_target_branch,
            ),
            "agent_limits": {"max_tool_iterations": 80, "allow_text_tool_calls": True},
            "autonomy": {
                "requires_activity": True,
                "continue_without_activity": True,
                "max_continuations": 120,
                "max_seconds": 3600,
                "continue_prompt": (
                    "Continue le mode dev. Tu ne dois pas seulement annoncer la prochaine action : "
                    "avance concretement dans le projet actif, utilise les capacites disponibles, "
                    "traite les taches ouvertes comme des demandes utilisateur prioritaires, "
                    "puis resume en 3-5 lignes maximum ce qui a vraiment change. "
                    "Si tu es bloque, donne seulement le blocage precis et la prochaine decision attendue."
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
    current_branch = _git_current_branch(path)
    suggested_branch = _dev_branch_name(path, done_tasks[-1] if done_tasks else status)
    merge_target_branch = _merge_target_branch(context, current_branch)
    context_block = "\n".join([
        f"Projet : {path.name}",
        f"Racine Git : {path}",
        "",
        "Fichiers modifies :",
        status,
        "",
        f"Branche actuelle : {current_branch or 'inconnue'}",
        f"Branche de travail Maurice suggeree : {suggested_branch}",
        f"Branche cible du merge apres approbation : {merge_target_branch or 'branche de depart inconnue'}",
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
                "2. Avant de commiter, verifie la branche courante. Si tu es sur `main`, `master`, "
                "`develop` ou une branche utilisateur non-Maurice, cree ou bascule d'abord sur une branche "
                f"`{suggested_branch}` via `git -C {path} switch -c {suggested_branch}`. "
                "Si la branche existe deja, utilise `git switch` vers cette branche ou choisis un suffixe clair.\n"
                "3. Utilise `shell.exec` pour executer :\n"
                f"   `git -C {path} add -A`\n"
                f"   `git -C {path} commit -m \"<ton message>\"`\n"
                "4. Reporte le resultat du commit (branche, hash, fichiers, message utilise).\n"
                "5. Ne lance jamais `git merge`, `git push`, suppression de branche ou rebase dans `/commit`. "
                "Apres le commit, demande explicitement a l'utilisateur s'il approuve le merge. "
                "Si l'utilisateur approuve ensuite, le prochain tour devra verifier que le working tree est propre, "
                "merger la branche Maurice vers la branche cible indiquee ci-dessus, et ne jamais push sans "
                "approbation separee.\n"
                "6. Si le commit echoue (hooks, rien a commiter, erreur), explique pourquoi et propose la correction.\n\n"
                "Ne coche pas les taches dans PLAN.md — elles sont deja cochees si elles figurent dans la liste ci-dessus."
            ),
            "agent_limits": {"max_tool_iterations": 6},
        },
    )


def _content_root(context: CommandContext) -> Path:
    if context.callbacks.get("scope") != "local":
        agent_workspace = _agent_workspace(context)
        content = agent_workspace / "content"
        content.mkdir(parents=True, exist_ok=True)
        return content
    content_root = context.callbacks.get("content_root")
    if content_root:
        content = Path(content_root).expanduser().resolve()
        content.mkdir(parents=True, exist_ok=True)
        return content
    content = _agent_workspace(context) / "content"
    content.mkdir(parents=True, exist_ok=True)
    return content


def _agent_workspace(context: CommandContext) -> Path:
    agent_workspace_for = context.callbacks.get("agent_workspace_for")
    if callable(agent_workspace_for):
        agent_workspace = agent_workspace_for(context.agent_id)
    else:
        agent_workspace = context.callbacks.get("agent_workspace")
    if agent_workspace is None:
        workspace = Path(context.callbacks.get("workspace") or ".").expanduser().resolve()
        agent_workspace = workspace / "agents" / context.agent_id
    content = Path(agent_workspace).expanduser().resolve() / "content"
    return content.parent


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


def _update_state(context: CommandContext, data: dict[str, str]) -> None:
    state = _read_state(context)
    state.update(data)
    _write_state(context, state)


def _active_project(context: CommandContext) -> str:
    active_path = _explicit_active_project_path(context)
    if active_path is not None:
        return active_path.name
    state_path = _state_active_project_path(context)
    if state_path is not None:
        return state_path.name
    value = _read_state(context).get("active_project")
    return value if isinstance(value, str) else ""


def _active_project_path(context: CommandContext) -> Path | None:
    active_project_path = _explicit_active_project_path(context)
    if active_project_path is not None:
        active_project_path.mkdir(parents=True, exist_ok=True)
        return active_project_path

    # Backward compatibility for older host surfaces.
    project_root = context.callbacks.get("project_root")
    if project_root:
        path = Path(project_root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    state_path = _state_active_project_path(context)
    if state_path is not None:
        state_path.mkdir(parents=True, exist_ok=True)
        return state_path
    active = _active_project(context)
    if not active:
        return None
    path = _content_root(context) / active
    path.mkdir(parents=True, exist_ok=True)
    return path


def _explicit_active_project_path(context: CommandContext) -> Path | None:
    value = context.callbacks.get("active_project_path")
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _state_active_project_path(context: CommandContext) -> Path | None:
    value = _read_state(context).get("active_project_path")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _remember_active_project(context: CommandContext, path: Path) -> None:
    record_seen_project(_agent_workspace(context), path)


def _known_project_rows(context: CommandContext) -> list[str]:
    content_root = _content_root(context)
    active_path = _explicit_active_project_path(context)
    seen_paths = {str(active_path)} if active_path is not None else set()
    rows = []
    for project in list_known_projects(_agent_workspace(context)):
        path = Path(project["path"]).expanduser().resolve()
        if str(path) in seen_paths or path.parent == content_root:
            continue
        suffix = "" if path.exists() else " (introuvable)"
        rows.append(f"{len(rows) + 1}. `{project['name']}` — `{path}`{suffix}")
    return rows


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


def _replace_plan_for_focus(plan_path: Path, focus: str) -> None:
    focus = " ".join(focus.strip().split())
    if not focus or not plan_path.exists():
        return
    task = f"- [ ] {focus.rstrip('.')}. [ non parallellisable ]"
    plan_path.write_text(
        "# Plan\n\n"
        "## Objectif\n\n"
        f"{focus}\n\n"
        "## Taches\n\n"
        f"{task}\n",
        encoding="utf-8",
    )



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


def _git_current_branch(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


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


def _dev_branch_name(path: Path, seed: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", seed.lower()).strip("-")
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")
    if not slug:
        slug = "work"
    return f"maurice/{slug[:48].strip('-') or 'work'}"


def _remember_dev_merge_target(
    context: CommandContext,
    path: Path,
    open_tasks: list[str],
    *,
    focus: str = "",
) -> str:
    if not _inside_git_repo(path):
        return ""
    current_branch = _git_current_branch(path)
    work_branch = _dev_branch_name(path, focus or (open_tasks[0] if open_tasks else path.name))
    if current_branch and not current_branch.startswith("maurice/"):
        _update_state(
            context,
            {
                "dev_project_path": str(path),
                "dev_work_branch": work_branch,
                "dev_merge_target_branch": current_branch,
            },
        )
        return current_branch
    return _read_state(context).get("dev_merge_target_branch", "")


def _merge_target_branch(context: CommandContext, current_branch: str) -> str:
    if current_branch and not current_branch.startswith("maurice/"):
        return current_branch
    return _read_state(context).get("dev_merge_target_branch", "")


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
    pitch_block = f'Demande utilisateur : "{pitch}"' if pitch else "Aucune demande fournie."
    existing_block = ""
    if existing_plan:
        existing_block = f"\nPlan existant :\n```markdown\n{existing_plan}\n```\n"
    if existing_decisions:
        existing_block += f"\nDecisions existantes :\n```markdown\n{existing_decisions}\n```\n"

    return (
        "Commande interne `/plan` : aide le modele a structurer sa pensee et a produire le contenu attendu "
        "dans `.maurice/PLAN.md` apres validation utilisateur.\n\n"
        f"Projet : {project_path.name}\n"
        f"Dossier : {project_path}\n"
        f"Fichier plan : {plan_path}\n"
        f"Fichier decisions : {decisions_path}\n"
        f"{pitch_block}\n"
        f"{existing_block}\n"
        "=== PHASE 1 : EXPLORATION (obligatoire avant toute proposition) ===\n"
        "Avant de proposer quoi que ce soit, explore le projet pour comprendre l'etat reel.\n"
        "Utilise explore.summary, filesystem.list, filesystem.read pour lire :\n"
        "- La structure generale du projet (arborescence racine, dossiers principaux)\n"
        "- Les fichiers de configuration pertinents pour la demande (package.json, configs de build, "
        "  fichiers de test, CI, configs d'env, lockfiles)\n"
        "- Les fichiers directement concernes par la demande (ex: gulpfile.js pour une migration build, "
        "  schema.prisma pour une migration BDD, app.module.ts pour une refonte framework)\n"
        "- Les dependances et versions actuelles si pertinent\n"
        "Ne propose rien avant d'avoir lu ces sources. Si le projet est volumineux, "
        "priorise les fichiers qui impactent directement la demande.\n\n"
        "=== PHASE 2 : CADRAGE ===\n"
        "- Si aucune demande n'est fournie, pose une seule question courte.\n"
        "- Si la demande est floue apres exploration, pose 1 a 2 questions de cadrage "
        "  (pas plus — prefere proposer et ajuster).\n"
        "- Si un plan existant a des taches, previens que la validation remplacera ces taches.\n"
        "- Ne cree ou ne modifie aucun fichier avant validation explicite.\n\n"
        "=== PHASE 3 : PROPOSITION ===\n"
        "Presente dans cet ordre :\n\n"
        "## Decouverte\n"
        "- Ce que tu as trouve dans le projet qui influence directement le plan :\n"
        "  fichiers cles, versions, patterns existants, points de blocage potentiels, "
        "  dependances entre composants, dette technique visible.\n"
        "- Ne liste pas tout — seulement ce qui change ou contraint l'approche.\n\n"
        "## Critique\n"
        "- Risques reels lies a CE projet specifique, pas des risques generiques.\n"
        "- Zones floues qui necesitent une decision utilisateur.\n"
        "- Choix techniques implicites dans la demande.\n"
        "- Points de rupture : qu'est-ce qui peut casser quoi.\n\n"
        "## Approche\n"
        "- Approche concrete adaptee a l'etat reel du projet.\n"
        "- Ordre de traitement justifie (pourquoi cette sequence).\n"
        "- Ce qui est conserve, ce qui est remplace, ce qui est refactore.\n\n"
        "## Taches\n"
        "Regle de granularite : une tache = ce qu'un agent peut executer en un seul passage "
        "autonome sans decision intermediaire. Pour un chantier complexe (migration, refonte, "
        "integration, changement d'architecture), un plan de 20 a 40 taches est normal et attendu. "
        "Ne pas regrouper artificiellement pour paraitre concis — la concision ici est un defaut.\n"
        "Chaque tache doit etre :\n"
        "- Suffisamment precise pour etre executable sans ambiguite\n"
        "- Verifiable (on sait quand c'est fini)\n"
        "- A la bonne maille (ni trop large 'migrer le build', ni trop fine 'ajouter une virgule')\n"
        "- Annotee `[ parallellisable ]` ou `[ non parallellisable ]` avec justification implicite\n"
        "- Les dependances entre taches doivent etre lisibles dans l'ordre\n\n"
        "Auto-verification avant de proposer : pour chaque tache, demande-toi — "
        "'Est-ce qu'un agent peut l'executer sans me demander une decision ?' "
        "Si non, soit la decompose, soit la marque explicitement comme decision utilisateur.\n\n"
        "Structure exacte attendue dans `PLAN.md` apres validation :\n"
        "# Plan\n\n"
        "## Objectif\n\n"
        "## Usage vise\n\n"
        "## Contraintes\n\n"
        "## Definition de fini\n\n"
        "## Decouverte\n\n"
        "## Critique\n\n"
        "## Approche\n\n"
        "## Taches\n\n"
        "- [ ] Description precise et verifiable. [ non parallellisable ]\n\n"
        "Structure attendue dans `DECISIONS.md` apres validation :\n"
        "# Decisions\n\n"
        f"- {datetime.now().strftime('%Y-%m-%d')} - Decision cle avec justification.\n\n"
        "Quand l'utilisateur valide, ecris `PLAN.md` et `DECISIONS.md` avec les outils disponibles. "
        "S'il demande un ajustement, integre l'ajustement puis repropose avant d'ecrire."
    )


def _dev_execution_prompt(
    project_path: Path,
    open_tasks: list[str],
    *,
    focus: str = "",
    merge_target_branch: str = "",
) -> str:
    meta = _meta_root(project_path)
    plan_path = meta / "PLAN.md"
    decisions_path = meta / "DECISIONS.md"
    agents_path = meta / "AGENTS.md"
    dreams_path = meta / "dreams.md"
    task_lines = "\n".join(f"- [ ] {task}" for task in open_tasks)
    focus_block = f"Demande utilisateur prioritaire pour ce passage : {focus}\n\n" if focus else ""
    branch_name = _dev_branch_name(project_path, focus or (open_tasks[0] if open_tasks else project_path.name))
    current_branch = _git_current_branch(project_path) if _inside_git_repo(project_path) else ""
    git_block = (
        "Depot Git detecte : oui\n"
        f"Branche actuelle : {current_branch or 'inconnue'}\n"
        f"Branche de travail Maurice suggeree : {branch_name}\n"
        f"Branche cible du merge apres approbation : {merge_target_branch or 'branche de depart a capturer avant switch'}\n"
    ) if _inside_git_repo(project_path) else (
        "Depot Git detecte : non\n"
        "Branche de travail Maurice suggeree : indisponible tant que le projet n'est pas un depot Git\n"
    )
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
        f"{focus_block}"
        "Taches ouvertes actuelles :\n"
        f"{task_lines}\n\n"
        "Regles d'execution :\n"
        "- Tu es en mode developpement : avance concretement, pas seulement en commentaire.\n"
        "- Workflow Git obligatoire quand le projet est un depot Git : avant toute modification de code, verifie la branche avec `shell.exec`, considere cette branche comme la branche de depart/cible de merge, puis travaille sur une branche dediee `maurice/...`.\n"
        f"- Si tu es sur `main`, `master`, `develop` ou une branche utilisateur non-Maurice, cree ou bascule d'abord sur `{branch_name}` avec `git -C {project_path} switch -c {branch_name}`. Si elle existe deja, utilise `git switch` vers cette branche ou ajoute un suffixe clair.\n"
        "- Ne lance jamais `git merge`, `git push`, suppression de branche ou rebase pendant `/dev`.\n"
        "- Les taches ouvertes representent la demande utilisateur. Ne les ecarte pas comme `deja fait` ou `plan depasse` sans preuve verifiee et sans rendre le plan coherent.\n"
        "- Si une tache parait deja partiellement presente, verifie l'acceptance reelle dans le code, termine l'integration manquante, puis coche seulement ce qui est effectivement fait.\n"
        "- Si le plan est ancien mais l'intention reste claire, corrige `.maurice/PLAN.md`/`.maurice/DECISIONS.md` toi-meme et continue. Ne demande une decision que si plusieurs choix produit/techniques incompatibles restent possibles.\n"
        "- Utilise les outils disponibles quand ils sont utiles pour lire, ecrire, verifier ou inspecter le projet.\n"
        "- Sois concis dans tes messages : pendant l'execution, evite les plans narratifs et les details de routine.\n"
        "- En fin de boucle, reponds en 3-5 lignes : changements faits, verification, blocage/prochaine decision si besoin.\n"
        "- Commence par les taches `[ non parallellisable ]`, dans l'ordre.\n"
        "- Pour les taches `[ parallellisable ]`, utilise `dev.spawn_workers` quand plusieurs missions peuvent avancer sans se bloquer; sinon execute sequentiellement.\n"
        "- Tu peux aussi utiliser `dev.spawn_workers` hors plan pour une enquete, verification ou tranche d'implementation vraiment independante.\n"
        "- Chaque worker doit recevoir le contexte minimal complet : projet, fichiers pertinents, objectifs, contraintes, sortie attendue, chemins d'ecriture quand ils sont connus.\n"
        "- Limites workers : 2-3 par defaut, 5 maximum par appel, 10 actifs maximum; fixe un budget temps raisonnable et ne cree jamais de boucle de workers.\n"
        "- Tu restes l'orchestrateur : un worker execute une mission, puis remonte `completed`, `blocked`, `needs_arbitration` ou `obsolete`.\n"
        "- Si un worker remonte un blocage, une contradiction, un conflit de fichiers, une permission requise, ou un impact sur d'autres workers, revise le plan toi-meme avant de continuer.\n"
        "- Si le plan change, considere les autres workers affectes comme obsoletes et relance seulement des workers frais avec un contexte corrige.\n"
        "- Integre les resultats des workers toi-meme avant de cocher une tache.\n"
        "- Apres chaque tache : verifie le code, cherche les impacts, nettoie le code mort, puis coche la tache dans `.maurice/PLAN.md`.\n"
        "- Note toute decision structurante dans `.maurice/DECISIONS.md`.\n"
        "- Mets a jour `.maurice/dreams.md` si tu decouvres des signaux utiles au dreaming.\n"
        "- Avant le commit final, fais une passe de finition proportionnee au changement : relis le diff complet, supprime le code mort/imports inutiles/fichiers temporaires, verifie qu'aucun hardcode ou chemin local personnel n'a ete introduit, et garde le commit coherent et revertable.\n"
        "- Mets a jour la documentation quand l'usage, la configuration, les commandes, les permissions, l'architecture ou un contrat de skill changent. Ne cree pas de doc ceremonielle pour un micro-fix purement interne.\n"
        "- Ajoute ou adapte les tests quand le comportement change, lance les tests pertinents, et mentionne clairement toute verification manuelle ou risque restant.\n"
        "- Si tu touches credentials, permissions, shell, reseau, rappels, Telegram ou filesystem, fais une passe de risque explicite avant commit et evite toute migration molle/cachee sauf demande utilisateur.\n"
        "- Quand toutes les taches du plan sont terminees et verifiees, prepare un commit sur la branche Maurice : `git add -A`, `git commit -m \"<message>\"`, puis arrete-toi et demande explicitement l'approbation utilisateur avant tout merge.\n"
        "- Si l'utilisateur approuve ensuite le merge, verifie que le working tree est propre, puis merge la branche `maurice/...` vers la branche de depart indiquee dans le contexte Git. Demande une precision seulement si cette branche de depart est inconnue. Ne push jamais sans approbation separee.\n"
        "- Stoppe et demande a l'utilisateur uniquement si une information manque, une autorisation est necessaire, ou un test manuel est indispensable.\n\n"
        "Contexte Git :\n"
        f"```text\n{git_block}```\n\n"
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
    signals = []
    for project in _dream_project_paths(context):
        if not project.exists():
            signals.append(
                {
                    "id": f"sig_dev_missing_{_signal_slug(project)}",
                    "type": "dev_project_missing",
                    "summary": f"Known project is missing: {project}",
                    "data": {"project": project.name, "path": str(project)},
                }
            )
            if len(signals) >= limit:
                break
            continue

        meta = _meta_root(project)
        files = {name: meta / name for name in DREAM_PROJECT_FILES}
        if not any(path.exists() for path in files.values()):
            continue
        plan_path = files["PLAN.md"]
        decisions_path = files["DECISIONS.md"]
        open_tasks = _open_tasks(plan_path)
        done_tasks = _done_tasks(plan_path)
        recent_decisions = _recent_decisions(decisions_path, limit=3)
        excerpts = {
            name: _read_excerpt(path, limit=1200)
            for name, path in files.items()
            if path.exists()
        }
        summary_parts = [
            f"Project {project.name}: {len(open_tasks)} open task(s), {len(done_tasks)} done task(s)."
        ]
        if open_tasks:
            summary_parts.append(f"Next: {open_tasks[0]}")
        if recent_decisions:
            summary_parts.append(f"Recent decisions: {'; '.join(recent_decisions)}")
        if excerpts.get("dreams.md"):
            summary_parts.append("Project dreams.md is present.")
        signals.append(
            {
                "id": f"sig_dev_{_signal_slug(project)}",
                "type": "dev_project_review",
                "summary": " ".join(summary_parts),
                "data": {
                    "project": project.name,
                    "path": str(project),
                    "meta_path": str(meta),
                    "files": {name: str(path) for name, path in files.items()},
                    "open_tasks": open_tasks[:5],
                    "done_count": len(done_tasks),
                    "recent_decisions": recent_decisions,
                    "excerpts": excerpts,
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
        limits=[
            "Uses only the active project, the agent project registry, and the machine project registry.",
            "Includes `.maurice/AGENTS.md`, `PLAN.md`, `DECISIONS.md`, and `dreams.md` excerpts.",
        ],
    )


def _dream_project_paths(context: PermissionContext) -> list[Path]:
    variables = context.variables()
    agent_workspace = Path(variables["$agent_workspace"]).expanduser().resolve()
    agent_content = Path(variables["$agent_content"]).expanduser().resolve()
    active_project = Path(variables["$project"]).expanduser().resolve()
    paths: list[Path] = []
    if active_project != agent_content and _meta_root(active_project).exists():
        paths.append(active_project)
    for project in _read_known_project_paths(agent_workspace / "projects.json"):
        paths.append(project)
    for project in list_machine_projects():
        path = project.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(Path(path).expanduser().resolve())
    return _dedupe_paths(paths)


def _read_known_project_paths(path: Path) -> list[Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, list):
        return []
    paths = []
    for project in projects:
        if not isinstance(project, dict) or not isinstance(project.get("path"), str):
            continue
        paths.append(Path(project["path"]).expanduser().resolve())
    return paths


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result = []
    for path in paths:
        key = str(path.expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path.expanduser().resolve())
    return result


def _signal_slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(path).strip()).strip("._-")[:80] or "project"
