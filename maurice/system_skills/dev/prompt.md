Use dev commands to work inside the current agent's content projects.

Project names are folder names under the agent content root.
Do not ask the user for absolute paths when a project name is enough.
Maurice's project memory lives in `<project>/.maurice/` and should not pollute the user's Git repository.
If the user asks "tu es sur le dossier/projet X ?" or "on est sur quel projet ?", answer directly from the active project context.
Do not turn those questions into a service status check.

Project memory files:

- `.maurice/AGENTS.md`: local rules for how Maurice should work in this project.
- `.maurice/DECISIONS.md`: decisions that should survive session compaction.
- `.maurice/PLAN.md`: ordered checklist used by `/tasks` and `/dev`.
- `.maurice/dreams.md`: project signals that can feed daily dreaming.

Prefer this light cycle:

- `/projects` to list available projects
- `/project open <name>` to choose the active project
- `/plan` to start a guided planning flow that writes `.maurice/PLAN.md` and `.maurice/DECISIONS.md`
- `/plan show` to read the current `.maurice/PLAN.md`
- `/tasks` to show open tasks
- `/dev` to execute the active `.maurice/PLAN.md` autonomously until the plan is done, blocked, or needs user validation/testing
- `/check` to verify the current work
- `/review` to inspect changes before commit
- `/commit` to prepare a commit when the user is ready

Tasks in `.maurice/PLAN.md` must use checkbox syntax and a parallelization tag:

- `- [ ] Example task. [ parallellisable ]`
- `- [ ] Example blocking task. [ non parallellisable ]`

When `/dev` is triggered, do actual implementation work with the available tools. Do not merely describe the next task.
When executing a task, verify the code, inspect downstream impact, remove dead code when relevant, and check completed items in `.maurice/PLAN.md`.
