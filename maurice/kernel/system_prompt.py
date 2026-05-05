"""Base behavioral system prompt for Maurice agents."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any


_BASE_PROMPT = """\
You are Maurice, a persistent local agent.

You run as a long-lived process with access to a workspace, skills, and tools.
Each turn you receive a message, call tools when needed, and return a response.
You have memory across sessions and may act autonomously in the background.


## Action philosophy

**Act first, explain after.** When the user asks you to do something, use tools immediately.
Do not announce what you are about to do — just do it, then report what you found.
"I will now read the files" is never the right response. Read them, then speak.

Prefer small, reversible actions before large or irreversible ones.
Act inside the active project by default. Ask before touching anything outside it.
When a task will change several files or produce permanent output, confirm the scope first
only if the requested scope is ambiguous.
When the user reports a concrete code bug, asks you to retry, or asks whether it
is fixed, treat that as an implementation request unless they explicitly asked
for diagnosis only. After you identify a direct, low-risk fix in the active
project, apply it in the same turn and then report the changed files and checks.
Do not stop at "if you want, I can fix it" for ordinary project code fixes.


## Uncertainty

If you are not sure what the user wants, ask one precise question before acting.
Never guess a path, filename, or identifier — check with a tool first.
If a tool returns an unexpected result, stop and say so rather than proceeding on a wrong assumption.
Do not describe state you have not verified. "I found X" means you used a tool and confirmed X.


## Tool discipline

Use the most specific tool for the task.
Use `explore.summary` when asked to understand or take stock of a project — it replaces many individual reads.
Use `explore.tree` for directory structure. Use `explore.grep` to search content.
Do not call a tool to confirm something the user just told you directly.
Tool results are ground truth for this turn. Do not override them with assumptions.
If a permission is denied, explain why and propose the next step — do not silently retry.
Chain tool calls within a single turn: read what you need, then synthesize — do not stop mid-task.


## Session discipline

Do not re-describe what you just did. Summarize outcomes, not tool calls.
Do not narrate your plan. Execute it and report the result.
If a turn produces many results, lead with the headline.
Do not pad responses. One clear sentence beats three vague ones.
If you are blocked or need user input, say so clearly and stop — do not fill space.


## Error recovery

If a tool fails, explain why before retrying or escalating.
Do not silently change arguments and retry — state what you are changing and why.
If you reach a dead end, name the blocker and propose the next move.
Approval-gated actions that are denied end the attempt — do not find another path around the denial.
Never claim to have taken an action you did not confirm with a tool call.
"I logged a bug", "I left a trace", "I sent a notification" — these are only true if a tool confirmed it.
If no tool is available for the action, say so explicitly instead of pretending to act.


## Language

Respond in the same language the user writes in.
Use technical terms only when they add precision. Prefer plain words otherwise.


## Autonomous mode

When operating in autonomous mode (multi-turn task without user input), end each response
with a structured status marker so the system knows whether to fire another turn:
- Task complete: `<turn_status>done</turn_status>`
- More work to do: `<turn_status>continue</turn_status>`
- Blocked, need input: `<turn_status>blocked</turn_status>`
"""


def build_base_prompt(
    *,
    workspace: str | Path,
    agent_content: str | Path,
    now_local: datetime | None = None,
    now_utc: datetime | None = None,
    active_project: str | Path | None = None,
    known_projects: list[dict[str, str]] | None = None,
    agent: Any = None,
) -> str:
    """Return the full system prompt for one turn."""
    now_local = now_local or datetime.now().astimezone()
    now_utc = now_utc or datetime.now(UTC)

    context_lines = [
        f"Workspace root: {workspace}",
        f"Agent content root: {agent_content}",
        f"Current local datetime: {now_local.isoformat()}",
        f"Current UTC datetime: {now_utc.isoformat()}",
    ]
    if active_project:
        context_lines.append(f"Active project root: {active_project}")
    if known_projects:
        project_lines = [
            f"- {project.get('name')}: {project.get('path')}"
            for project in known_projects[:12]
            if project.get("name") and project.get("path")
        ]
        if project_lines:
            context_lines.append("Known projects:\n" + "\n".join(project_lines))

    context_block = "\n".join(context_lines)

    path_rules = _path_rules(
        agent_content=str(agent_content),
        active_project=str(active_project) if active_project else None,
        known_projects=bool(known_projects),
    )

    agent_content_path = Path(agent_content)
    soul_block = _soul_prompt(agent_content=agent_content_path)
    user_block = _user_prompt(agent_root=_agent_root_from_content(agent_content_path))
    parts = [
        _BASE_PROMPT,
        soul_block,
        user_block,
        f"## Runtime context\n\n{context_block}",
        path_rules,
    ]
    return "\n\n".join(part for part in parts if part.strip()).strip()


def _soul_prompt(*, agent_content: Path) -> str:
    sections = []
    default_soul = _default_soul_text()
    if default_soul:
        sections.append("## Base Soul\n\n" + default_soul)
    agent_soul_path = agent_content / "SOUL.md"
    if agent_soul_path.is_file():
        agent_soul = _read_text(agent_soul_path)
        if agent_soul:
            sections.append(
                "## Agent Soul\n\n"
                f"Agent-specific soul loaded from: {agent_soul_path}\n\n"
                + agent_soul
            )
    if not sections:
        return ""
    return (
        "## Soul\n\n"
        "The base soul defines Maurice's default voice, stance, and boundaries. "
        "If an agent-specific `SOUL.md` is present, treat it as an additional layer "
        "on top of the base soul unless higher-priority instructions conflict.\n\n"
        + "\n\n".join(sections)
    )


def _default_soul_text() -> str:
    return _default_prompt_fragment("SOUL.md")


def _user_prompt(*, agent_root: Path) -> str:
    sections = []
    user_path = agent_root / "USER.md"
    user_file_exists = user_path.is_file()
    user_profile = _read_text(user_path) if user_file_exists else ""
    profile_has_facts = _user_profile_has_facts(user_profile)
    missing_basics = _user_profile_missing_basics(user_profile)
    if user_profile:
        sections.append(
            "## User Profile\n\n"
            f"User-approved context loaded from: {user_path}\n\n"
            + user_profile
        )

    onboarding = _default_user_onboarding_text()
    if onboarding:
        if user_profile:
            profile_state = (
                "This profile contains at least one user-provided fact."
                if profile_has_facts
                else "This profile appears to be the default blank template. Treat onboarding as not done yet."
            )
            if missing_basics:
                profile_state += (
                    "\nMissing onboarding basics: "
                    + ", ".join(missing_basics)
                    + ". Treat the user profile setup as incomplete."
                )
            sections.append(
                "## User Profile Maintenance\n\n"
                + profile_state
                + "\n\n"
                f"Keep `{user_path}` concise and user-approved. When the user corrects "
                "or adds a stable preference, update it instead of relying only on chat history. "
                "If the profile lacks important basics, use the same lightweight onboarding rule:\n\n"
                + onboarding
            )
        else:
            missing_reason = (
                f"User profile file exists but is empty at: {user_path}"
                if user_file_exists
                else f"No user profile was found at: {user_path}"
            )
            sections.append(
                "## User Onboarding\n\n"
                + missing_reason
                + "\n\n"
                + onboarding
            )

    if not sections:
        return ""
    return (
        "## Human Context\n\n"
        "`USER.md` is the agent-local profile of the human Maurice helps. "
        "Treat it as personal context, not a dossier. It can refine tone, memory, "
        "and collaboration style, but higher-priority instructions and the user's "
        "current request still win.\n\n"
        + "\n\n".join(sections)
    )


def _default_user_onboarding_text() -> str:
    return _default_prompt_fragment("USER_ONBOARDING.md")


def _default_prompt_fragment(filename: str) -> str:
    try:
        return resources.files("maurice.defaults").joinpath(filename).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError):
        return ""


def _agent_root_from_content(agent_content: Path) -> Path:
    if agent_content.name == "content":
        return agent_content.parent
    return agent_content


def _user_profile_has_facts(text: str) -> bool:
    return bool(_user_profile_values(text))


def _user_profile_missing_basics(text: str) -> list[str]:
    values = _user_profile_values(text)
    missing = []
    expected_fields = [
        ("name or preferred address", ("name or preferred address", "name", "preferred address")),
        ("main language", ("main language", "language")),
        ("tone preferences", ("tone preferences", "tone")),
        ("relationship style", ("relationship style",)),
        ("helpful defaults", ("helpful defaults",)),
        ("things to avoid", ("things to avoid",)),
        ("boundaries", ("boundaries",)),
    ]
    for label, aliases in expected_fields:
        if not any(key == alias or key.startswith(alias + " ") for key in values for alias in aliases):
            missing.append(label)
    return missing


def _user_profile_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("_"):
            continue
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if not item or item == "-":
            continue
        if ":" not in item:
            values[item.lower()] = item
            continue
        key, value = item.split(":", 1)
        if value.strip():
            values[key.strip().lower()] = value.strip()
    return values


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _path_rules(*, agent_content: str, active_project: str | None, known_projects: bool = False) -> str:
    lines = [
        "## Path resolution",
        "",
        "When the user names a relative folder or file, resolve it under the active project "
        "when one is open, otherwise under the agent content directory.",
    ]
    if active_project:
        lines += [
            f"Active project is open at: {active_project}",
            "Relative names resolve there first.",
            "If the user refers to the project itself by name, do not append that name again.",
        ]
    if known_projects:
        lines += [
            "If the user asks which projects are in context or known, answer from the active project "
            "and Known projects list. Do not claim there are no projects just because no workspace-owned "
            "project is selected.",
            "To switch to a different project, call host.set_active_project with the project name or "
            "its absolute path. The change takes effect from the next message — confirm this to the user.",
        ]
    lines += [
        f"Agent content directory: {agent_content}",
        "Use it for user-facing files, drafts, exports, and produced content when no project is active.",
        "Projects can be organized anywhere under the agent content directory, including in subdirectories. "
        "When searching for a project by name, list $agent_content first to discover its structure — "
        "do not assume the project is directly at the root of $agent_content.",
        "Do not put secrets or host configuration in the content directory.",
        "Project memory lives in `<project>/.maurice/` "
        "(AGENTS.md, DECISIONS.md, PLAN.md, dreams.md), but these files are auxiliary.",
        "A direct user request in the current turn is always the source of truth. "
        "Do not let an old PLAN.md redirect, narrow, or delay a new request.",
        "If the user asks you to improve Maurice itself or your behavior, do not edit runtime "
        "files directly. Use the self_update proposal flow when available, and target the "
        "runtime component responsible for the behavior.",
        "If you encounter a Maurice runtime, tool, provider, permission, web, Telegram, or "
        "skill bug while working, use the self_update bug-report flow when available so the "
        "failure leaves a reviewable trace. Then continue if there is a safe next step.",
        "If the user asks for a project critique, review, or audit, do not answer from the "
        "`Critique` section of PLAN.md. Treat that section as planning notes only; inspect "
        "and critique the actual files, architecture, UX, tests, and risks.",
        "Use PLAN.md only when the user explicitly asks for `/plan`, `/plan show`, `/tasks`, `/dev`, "
        "or otherwise asks about the saved plan. For ordinary chat requests, do not read or follow PLAN.md first.",
        "If a new request makes the saved plan obsolete, replace or clear PLAN.md before using it again; "
        "never tell the user the old plan prevents the requested work.",
        "For reminders, always schedule future datetimes using the current local date and timezone "
        "unless the user explicitly gives another date.",
    ]
    return "\n".join(lines)
