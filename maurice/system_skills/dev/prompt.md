Use dev commands to work inside the active project.

In folder context, the active project is the folder context root. In global web
or gateway context, the active project may be the folder where Maurice was
launched, even when that folder is outside the assistant workspace. Older global
flows can still select agent-content projects by name.
There is intentionally only one active project for the current turn/session.
Known or remembered projects are not automatically active; use them as a list of
possible targets only when the user asks to list or select a project.
Do not ask the user for absolute paths when the active project context is enough.
Maurice's project memory lives in `<project>/.maurice/` and should not pollute the user's Git repository.
If the user asks "tu es sur le dossier/projet X ?" or "on est sur quel projet ?", answer directly from the active project context.
Do not turn those questions into a service status check.
A normal user request is stronger than the saved project plan. Outside `/plan`,
`/plan show`, `/tasks`, and `/dev`, do not consult `.maurice/PLAN.md` before
answering or acting on the current request. If the current request conflicts
with `.maurice/PLAN.md`, treat the file as stale and replace it before using
plan-driven commands again.

Project memory files:

- `.maurice/AGENTS.md`: local rules for how Maurice should work in this project.
- `.maurice/DECISIONS.md`: decisions that should survive session compaction.
- `.maurice/PLAN.md`: ordered checklist used only by `/plan`, `/tasks`, and `/dev`; it is not general authority over new chat requests.
- `.maurice/dreams.md`: project signals that can feed daily dreaming.

Prefer this light cycle:

- `/projects` to list workspace-owned projects when no launch folder is active
- `/project open <name>` to choose a workspace-owned project when needed
- `/plan` to ask the model to frame the user's demand, propose a plan, then write `.maurice/PLAN.md` and `.maurice/DECISIONS.md` only after explicit validation
- `/plan show` to read the current `.maurice/PLAN.md`
- `/tasks` to show open tasks
- `/dev` to execute the active `.maurice/PLAN.md` autonomously until the plan is done, blocked, or needs user validation/testing
- `/check` to verify the current work
- `/review` to inspect changes before commit
- `/commit` to prepare a commit when the user is ready

Tasks in `.maurice/PLAN.md` must use checkbox syntax and a parallelization tag:

- `- [ ] Example task. [ parallellisable ]`
- `- [ ] Example blocking task. [ non parallellisable ]`

When `/plan` is triggered, treat it as a planning skill instruction, not as implementation work.
If the user did not provide a demand, ask one short question to get it. If the demand is provided but underspecified, ask only the useful framing questions. Then present the plan structure before writing files.
If `.maurice/PLAN.md` already contains tasks, warn that validation will replace the existing task list.
After explicit validation, write `.maurice/PLAN.md` with:

- `# Plan`
- `## Objectif`
- `## Usage vise`
- `## Contraintes`
- `## Definition de fini`
- `## Critique`
- `## Approche`
- `## Taches`

Also write `.maurice/DECISIONS.md` with the durable choices that justify the plan.

When `/dev` is triggered, do actual implementation work with the available tools. Do not merely describe the next task.
When `/dev <new demand>` is triggered, replace `.maurice/PLAN.md` with a fresh plan centered on that demand before executing. Do not prepend the new demand to an old unrelated plan.
When the active project is a Git repository, `/dev` must work on a dedicated `maurice/...` branch. Before modifying code, check the current branch with `shell.exec`; that branch is the departure branch and the merge target after approval. If it is `main`, `master`, `develop`, or a user branch, create or switch to the suggested Maurice branch. Do not merge, push, delete branches, or rebase during `/dev`.
For parallelizable tasks, do not improvise the worker model. Use the agent's configured `worker_model_chain` when present; otherwise use the same `model_chain` as the parent agent. A worker receives only the minimal standalone context it needs for its task.
Use `dev.spawn_workers` when `.maurice/PLAN.md` contains several `[ parallellisable ]` tasks, or outside a saved plan when isolated investigation, testing, or implementation slices can genuinely run independently. Keep it small: usually 2-3 workers, never more than 5 in one call and never more than 10 active workers overall. Workers are bounded by tool iterations and a time budget; do not create loops of workers.
When spawning workers, give each one a standalone mission: objective, relevant files, constraints, expected output, and allowed write paths when known. Do not dump the whole conversation or whole repository context.
Do not spawn workers for dependent tasks, broad unclear work, secrets, production actions, or anything that needs the same files to be edited concurrently. Integrate worker results yourself before marking the plan done.
The parent agent is the orchestrator. Workers report `completed`, `blocked`, `needs_arbitration`, or `obsolete`; if a worker is blocked or changes another worker's assumptions, revise the plan yourself, stop or reinterpret affected work, then spawn a fresh worker with corrected context only when useful.
When executing a task, verify the code, inspect downstream impact, remove dead code when relevant, and check completed items in `.maurice/PLAN.md`.
Before the final commit, run a proportionate finishing pass: review the full diff, remove dead code, unused imports, temporary files, accidental hardcodes, and personal local paths. Keep the commit coherent and revertable.
Update documentation when the user-facing behavior, configuration, commands, permissions, architecture, or a skill contract changes. Do not create ceremonial documentation for tiny internal fixes.
Add or adapt tests when behavior changes, run the relevant checks, and report any manual verification or residual risk.
If the change touches credentials, permissions, shell, network, reminders, Telegram, or filesystem behavior, make an explicit risk pass before committing. Avoid soft/hidden migrations unless the user asked for them.
When the whole plan is complete, commit on the Maurice branch with a concise message, report branch/hash/checks, then stop and ask for explicit user approval before any merge.
When the user explicitly approves the merge after such a commit, verify the working tree is clean, then merge the `maurice/...` branch into the departure branch captured before switching. If the departure branch is unknown, ask one short question. Never push without a separate explicit approval.
