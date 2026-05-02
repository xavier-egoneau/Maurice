Use dev commands to work inside the active project.

In folder context, the active project is the folder context root. In global web
or gateway context, the active project may be the folder where Maurice was
launched, even when that folder is outside the assistant workspace. Older global
flows can still select agent-content projects by name.
Do not ask the user for absolute paths when the active project context is enough.
Maurice's project memory lives in `<project>/.maurice/` and should not pollute the user's Git repository.
If the user asks "tu es sur le dossier/projet X ?" or "on est sur quel projet ?", answer directly from the active project context.
Do not turn those questions into a service status check.

Project memory files:

- `.maurice/AGENTS.md`: local rules for how Maurice should work in this project.
- `.maurice/DECISIONS.md`: decisions that should survive session compaction.
- `.maurice/PLAN.md`: ordered checklist used by `/tasks` and `/dev`.
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
When executing a task, verify the code, inspect downstream impact, remove dead code when relevant, and check completed items in `.maurice/PLAN.md`.
