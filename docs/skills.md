# Skills

A skill is a directory containing a `skill.yaml` manifest and optional Python modules.
Skills are the only way to add capabilities to the agent.

## Directory layout

```
my_skill/
  skill.yaml       — manifest (required)
  tools.py         — tool executors (if the skill declares tools)
  commands.py      — slash command handlers (if the skill declares commands)
  prompt.md        — prompt fragment injected each turn (optional)
  dreams.md        — dream context injected during dreaming sessions (optional)
  daily.md         — daily digest contribution instructions (optional)
```

## skill.yaml

Minimal example:

```yaml
name: my_skill
version: 0.1.0
origin: user          # "system" | "user"
mutable: true
required: false
description: What this skill does.
config_namespace: skills.my_skill
available_in: [local, global]  # optional; declares where the skill can load

requires:
  binaries: []        # system binaries that must be present (e.g. git)
  credentials: []     # credential names required

dependencies:
  skills: []          # skills that must be loaded first
  optional_skills: [] # skills used if present

permissions: []       # permission classes this skill may use (informational)

tools: []             # list of ToolDeclaration objects (see below)
commands: []          # list of CommandDeclaration objects (see below)

tools_module: my_package.my_skill.tools  # module with build_executors(ctx)

backend: null
storage: null

dreams:
  attachment: dreams.md
  input_builder: null  # optional: dotted path to a function(PermissionContext) -> DreamInput

events:
  state_publisher: null
```

## Tool declarations

Each entry under `tools:` is a `ToolDeclaration`:

```yaml
tools:
  - name: my_skill.do_something
    description: What the tool does (shown to the model).
    input_schema:
      type: object
      properties:
        path:
          type: string
      required: ["path"]
    permission_class: fs.read     # one of the PermissionClass values
    permission_scope:
      paths: ["$workspace/**", "$project/**"]
    trust:
      input: local_mutable        # trusted | local_mutable | external_untrusted | skill_generated
      output: local_mutable
    executor: my_package.my_skill.tools.do_something  # dotted path (legacy) or use tools_module
```

**Permission classes:** `fs.read`, `fs.write`, `network.outbound`, `shell.exec`,
`secret.read`, `agent.spawn`, `host.control`, `runtime.write`.

## Tool executors

The recommended pattern is `tools_module` + `build_executors`:

```python
# my_skill/tools.py

def build_executors(ctx):          # ctx is SkillContext
    return {
        "my_skill.do_something": lambda args: _do_something(args, ctx),
    }

def _do_something(args, ctx):
    from maurice.kernel.contracts import ToolResult, TrustLabel
    # ... do work ...
    return ToolResult(
        ok=True,
        summary="Done.",
        data={"result": "..."},
        trust=TrustLabel.LOCAL_MUTABLE,
        artifacts=[], events=[], error=None,
    )
```

`SkillContext` fields available in `build_executors`:

| Field | Type | Description |
|---|---|---|
| `permission_context` | `PermissionContext` | Workspace/runtime root paths and scope variables |
| `event_store` | `EventStore` | Emit events |
| `skill_config` | `dict` | Config for this skill from `skills.yaml` |
| `all_skill_configs` | `dict` | All skill configs |
| `skill_roots` | `list` | Skill root paths |
| `enabled_skills` | `list[str]` | Currently enabled skill names |
| `agent_id` | `str` | Current agent id |
| `session_id` | `str \| None` | Current session id |
| `extra` | `dict` | Host-provided callbacks (e.g. `schedule_reminder`, `cancel_job`) |

Common `extra` keys:

| Key | Description |
|---|---|
| `context_root` | Current context root |
| `content_root` | User-facing content root for the current context |
| `state_root` | State root for sessions/events/memory metadata |
| `memory_path` | SQLite memory path for the current context |
| `scope` | `local` or `global` |
| `lifecycle` | `transient` or `daemon` |

`$workspace` is the context content root. In local mode this is the project
folder; in global mode this is the assistant workspace. `$project` is the active
project root and may point outside the workspace, for example when `maurice web`
is launched from a working folder while global mode is configured. File-oriented
skills should usually include both `$workspace/**` and `$project/**` in their
permission scopes unless they intentionally operate only on assistant-owned
workspace data.

There is only one active project for a command or turn. Project-scoped skills
should treat `active_project_path` / `project_root` as the current target and
should not scan for additional projects. A global agent can remember projects it
has explicitly seen in its own `<workspace>/agents/<agent-id>/projects.json`,
but remembered projects are history until the user opens or selects one.

## Command declarations

Commands are slash commands dispatched by the host (not by the model).
`available_in` uses the same `local`/`global` scope vocabulary as skills: on a
skill it controls whether the skill is loaded, and on a command it controls
whether the command appears in `/help` and UI autocomplete for that scope.

```yaml
commands:
  - name: /my_command
    description: What the command does.
    handler: my_package.my_skill.commands.my_command
    renderer: markdown    # "markdown" | "plain"
    available_in: [local, global]  # optional; hide from /help outside these scopes
```

Handler signature:

```python
from maurice.host.command_registry import CommandContext, CommandResult

def my_command(ctx: CommandContext) -> CommandResult:
    text = ctx.message_text            # full message e.g. "/my_command arg1 arg2"
    content_root = ctx.callbacks["content_root"]
    scope = ctx.callbacks["scope"]     # "local" | "global"
    agent_id = ctx.agent_id
    session_id = ctx.session_id
    return CommandResult(text="Response text", format="markdown")
```

Command handlers receive context paths through `ctx.callbacks`, not as direct
attributes. Prefer `content_root`, `state_root`, `context_root`, and `memory_path`
over reconstructing paths from config names.
For project-scoped commands, prefer `active_project_path` or `project_root` when
present; in global web these point at the launch folder, not necessarily inside
the workspace.

To delegate to the LLM, return an `agent_prompt` in metadata:

```python
return CommandResult(
    text="Working on it...",
    metadata={
        "agent_prompt": "You are a planning assistant. Write a PLAN.md ...",
        "agent_limits": {"max_tool_iterations": 8},
    },
)
```

## Skill Roots

Global skill roots are configured in `host.yaml`; local contexts merge global
defaults, local `.maurice/config.yaml`, `~/.maurice/skills`, and optional
`./skills`:

```yaml
skill_roots:
  - path: /path/to/runtime/system_skills
    origin: system
    mutable: false
  - path: /path/to/workspace/skills
    origin: user
    mutable: true
```

`SkillLoader` discovers skills from all roots in order. User skills override system skills with the same name.
`skills.create` writes to the mutable user root of the current context, so a
local project writes under its own `./skills` when that root exists.

## System skills

| Name | Key capability |
|---|---|
| `filesystem` | read/write/list/move files in the current context |
| `memory` | semantic notes persisted across sessions |
| `web` | HTTP fetch + search |
| `vision` | image description |
| `reminders` | schedule one-off reminders |
| `dreaming` | background LLM reflection runs |
| `workspace_dreaming` | cross-agent dreaming inputs from agent memories |
| `veille` | watch topics that feed dreaming and daily synthesis |
| `daily` | morning digest synthesis from agent dream reports |
| `self_update` | report runtime bugs and propose changes to the runtime (proposal-only) |
| `skills` | inspect and author skills |
| `host` | inspect host state (processes, dashboard data) |
| `dev` | project-scoped development workflow (`/plan`, `/dev`, `/commit`, …) |

`self_update` also exposes channel-neutral commands:
`/auto_update_list`, `/auto_update_show <id>`, `/auto_update_validate <id>`,
and `/auto_update_apply <id> confirm`.

## Dream And Daily Contributions

`dreams.md` is the cross-skill hook for background synthesis. A skill uses it to
say what signals it can contribute during `dreaming.run`: stale memory, overdue
reminders, project risks, ideas, links between projects, or candidate actions.

`daily.md` is the matching hook for the morning digest. A skill uses it to say
which parts of its dream signals, state, or summaries should matter in the
daily. The `daily` system skill loads those fragments from currently enabled
skills, consumes the latest agent-scoped dream report from
`<workspace>/agents/<agent-id>/dreams/`, and renders the digest through the
scheduler. Because `daily` is a normal optional system skill, disabling it for an
agent also prevents that agent's scheduled `daily.digest` job from being
created.

Two optional dreaming-oriented skills are shipped as concrete examples of this
pattern:

- `workspace_dreaming` reads recent memories from each agent workspace and
  contributes a multi-agent recap to dreaming and daily
- `veille` stores watch topics under
  `<workspace>/agents/<agent-id>/veille/topics.json`, researches them through
  the configured SearxNG endpoint when available, and contributes signals such
  as useful new technology, security issues in known dependencies, or news
  connected to active projects
