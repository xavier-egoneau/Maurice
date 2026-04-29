# Maurice Skills

## Principle

Skills are the default way to add capabilities.

The kernel should not grow for optional features.


## Skill Package

A skill should be self-contained.

Minimal shape:

```text
skills/<name>/
  skill.yaml
  prompt.md
  tools.py
  dreams.md
```

Optional:

```text
  backend.py
  storage/
  ui.json
```


## Skill Roots

Maurice uses one skill contract, but skills may come from different physical roots.

System skills are shipped with Maurice and live under the runtime root.
They should be read-only during normal agent execution.

User skills live under the workspace root.
They may be created or modified by agents when policy allows skill authoring.

Example:

```text
maurice-runtime/system_skills/
  memory/
  dreaming/

maurice-workspace/skills/
  project_skill/
```

The loader may read both roots through the same skill pipeline.
A user skill must not silently replace a system skill with the same name.


## Loader Lifecycle

Skill loading is strict and phased.

The loader should run these phases:

1. discover configured skill roots
2. read all `skill.yaml` manifests
3. validate manifest schemas
4. reject name collisions
5. resolve dependencies
6. check required binaries and credentials
7. initialize or migrate storage
8. start optional backends
9. register tools
10. register prompt fragments
11. register dreams attachments and hooks
12. publish skill health

There should be no special loader path for system skills.
System and user skills use the same phases.


## Skill States

Every discovered skill should end in one explicit state:

- `loaded`
- `disabled`
- `disabled_with_error`
- `missing_dependency`
- `migration_required`

State meaning:

- `loaded`: the skill is active and its tools/hooks may be registered
- `disabled`: the skill is intentionally disabled by config
- `disabled_with_error`: the skill failed but is not required
- `missing_dependency`: required binary, credential, backend, or dependency is absent
- `migration_required`: storage needs a migration that was not applied

A broken non-required skill should not crash Maurice.
It should become `disabled_with_error`, publish a health event, and include
suggested fixes when the loader can infer a safe correction.

A broken required skill may block startup.


## Collision Rules

Skill names are globally unique.

In v1, name collision is an error.
Tool-name collisions disable the later non-required skill and keep already
loaded skills active. Required skills may still block startup if their tools
collide.

Examples:

```text
system:memory + user:memory = error
user:todo + user:todo = error
```

User skills must not silently override system skills.

If the user wants to change a system skill, Maurice should create a patch or proposal in the workspace.
It should not modify the system skill directly during normal execution.


## Dependencies

A skill may depend on other skills.

Dependencies should be declared in `skill.yaml`.

Example:

```yaml
dependencies:
  skills:
    - "memory"
  optional_skills:
    - "web"
```

Missing required dependencies produce `missing_dependency`.
Missing optional dependencies should not block loading.


## Backend Lifecycle

A skill may declare an optional backend.

The loader starts backends only after manifest validation, dependency checks, and storage migration.

Backend startup should publish health:

- `starting`
- `ready`
- `failed`
- `stopped`

If a backend fails:

- required skill: startup may fail
- non-required skill: skill becomes `disabled_with_error`


## Storage Lifecycle

If a skill needs persistence, it owns:

- schema
- storage path
- schema version
- migrations
- health checks

The loader may orchestrate migrations, but the skill owns migration meaning.

Storage migration failure should produce `migration_required` or `disabled_with_error` depending on whether the migration can safely wait.


## Reload Behavior

Reload is allowed for user skills.

Reload should:

1. re-scan user skill roots
2. validate changed manifests
3. reject collisions
4. start new backends
5. stop removed user skill backends when safe
6. update active skill registry for future turns
7. emit reload events

Reload should not hot-patch an in-flight turn.
Changes apply to the next turn or next session boundary.

System skills are not reloaded from mutable workspace changes.
Runtime/system skill updates go through the runtime self-update proposal flow.


## Manifest

`skill.yaml` should define:

- name
- version
- description
- origin
- mutable
- config namespace
- required binaries
- exported tools
- backend entry
- dreams entry
- dependencies
- storage
- event/state publishers
- required flag
- permissions requested
- tools_module (dotted Python module path where `build_executors` lives)


## Executor Convention

Every skill that exposes tools must implement a `build_executors` function in its tools module:

```python
def build_executors(ctx: SkillContext) -> dict[str, ToolExecutor]:
    ...
```

`SkillContext` carries the full runtime context:

- `permission_context` — scoped path and credential context
- `registry` — the loaded skill registry (available at executor-build time)
- `event_store` — optional event bus
- `skill_config` — this skill's config slice from `skills.<name>`
- `all_skill_configs` — full skills config dict
- `skill_roots` — discovered skill roots
- `enabled_skills` — active skill names
- `agent_id` / `session_id` — current agent and session
- `extra` — host-provided hooks (callbacks, backends, etc.)

The `extra` dict carries anything that requires host wiring at runtime, such as scheduler callbacks or provider backends. Skills should look up their hooks from `ctx.extra` by agreed key names.

The kernel calls `registry.build_executor_map(ctx)` to dynamically load all executors from loaded skills. No caller needs to import individual skill modules directly.


## Prompt Fragments

A skill may contribute prompt fragments.

Prompt text should be:

- focused
- capability-specific
- minimal

Prompt fragments are not the main enforcement mechanism.


## Dreams Attachment

Each skill may attach a `dreams.md` file.

This file explains to the dreaming pipeline what data this skill can contribute and how that data should be interpreted.

It should define:

- available dream inputs
- useful signals
- candidate action types
- limits and uncertainty
- data freshness and trust assumptions

`dreams.md` is not a generic prompt dump.
It is the skill-owned contract between the skill and the dreaming pipeline.

The kernel may load and summarize it, but the meaning of the dream data remains owned by the skill.


## Tool Ownership

Every tool belongs to one skill.

The kernel knows the tool contract.
The skill owns the behavior.

If a skill provides a user-meaningful capability, it should provide it as a tool.

Skills should not depend on hidden runtime affordances for normal behavior.


## Skill-Owned State

If a skill needs persistence, it owns:

- schema
- lifecycle
- migration path

The kernel should not absorb skill-specific state.


## Generic UI Metadata

If a skill wants to expose status or metrics to the dashboard, it should provide data, not hard-coded UI logic.

That should look like:

- a schema
- a state payload
- optional rendering hints

The dashboard should consume generic skill state.
