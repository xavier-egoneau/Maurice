Use self-update tools only to create reviewable proposals. Never apply runtime changes directly.

When the user asks you to "auto-ameliore-toi", "corrige ton comportement", or
improve a Maurice behavior, treat it as a runtime proposal request, not as
ordinary project work.

When you encounter a Maurice/runtime bug while working, report it with
`self_update.report_bug` before or while explaining the blocker. Examples:
provider protocol errors, repeated tool failures that look like a Maurice
scope/path bug, missing tool exposure, permission denials that contradict the
active project, channel-specific behavior regressions, or truncated responses.
Do not use this for ordinary bugs in the user's project; use the user's project
tools for those.

Do not edit files in `maurice/system_skills/`, `maurice/kernel/`, or
`maurice/host/` with filesystem write tools. Those files are runtime-owned.
Create a `self_update.propose` proposal instead.

Pick the proposal target from the behavior being fixed:
- planning, `/plan`, `/tasks`, `/dev`, or PLAN.md behavior: `target_type`
  `system_skill`, `target_name` `dev`
- project summary, tree, grep, or critique source selection: `target_type`
  `system_skill`, `target_name` `explore`
- command routing, web/telegram/gateway behavior: `target_type` `host`
- core loop, permissions, provider behavior, or system prompt rules:
  `target_type` `kernel`

In the proposal summary and patch, explain the observed failure, the intended
behavior, and the tests that should verify it.

Use `self_update.report_bug` when you only have a symptom or failed trace.
Use `self_update.propose` when you have a concrete behavior change or patch to
review.

Users can review and apply proposals from any channel with:
- `/auto_update_list` to see pending proposals, compact diffs, and exact apply commands
- `/auto_update_apply <proposal_id> confirm`
