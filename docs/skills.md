# Skills

A skill is a directory that teaches Maurice a capability. User skills are
lightweight by default: `skill.md`, `dreams.md`, and `daily.md` are enough.
Advanced skills can still use `skill.yaml` when they need explicit tool or
command declarations.

Skills should be autonomous and shareable. Maurice is expected to grow toward a
skill store, so a skill must not rely on undocumented local setup from the
author's machine. If the skill needs binaries, credentials, config files,
services, caches, or setup commands, the skill folder must explain or provide
that setup path.

## Directory layout

```
my_skill/
  skill.md         — main instructions with name/description frontmatter
  dreams.md        — what dreaming should notice or propose
  daily.md         — what the morning digest should include or ignore
  tools.py         — optional code hooks for deterministic dream inputs
```

`skills.create` writes this lightweight layout. It does not create `skill.yaml`
unless you write an advanced skill by hand.

## skill.md

Minimal example:

```markdown
---
name: calendar_notes
description: Read local calendar notes and help prepare the day.
---

# Calendar Notes

Use this skill when the user asks about agenda, preparation, or upcoming events.
Read the configured calendar sources cautiously and never invent availability.
```

Maurice infers the runtime manifest from the folder name and `skill.md`
frontmatter. If `tools.py` exists and `dreams.md` exists, Maurice automatically
wires `tools.build_dream_input` as the skill's dreaming input builder. If
`tools.py` also defines `tool_declarations()`, those tools are registered for
chat use and must be backed by `build_executors(ctx)`.

For shareability, `skill.md` should include:

- required binaries and install commands for common systems
- expected Maurice credentials and how to capture them
- generated config locations and whether files may be overwritten
- validation commands that prove the skill is ready
- privacy and secret-handling rules

Do not assume the user's machine already has hidden config from the skill
author's environment.

## dreams.md And daily.md

`dreams.md` tells the background dreaming pass what this skill can notice,
connect, or propose. It should describe signals and boundaries, not user-facing
digest prose.

`daily.md` tells the morning digest what to include from that skill and when the
skill should stay silent. This is where you say things like "surface today's
calendar events first, then important events in the next 7 days only when useful."

## Optional Code

When the skill needs deterministic reads, parsing, API calls, or other code, put
the code inside the skill folder:

```text
my_skill/
  skill.md
  dreams.md
  daily.md
  tools.py
```

For dreaming, expose:

```python
from datetime import UTC, datetime
from maurice.kernel.contracts import DreamInput
from maurice.kernel.permissions import PermissionContext

def build_dream_input(
    context: PermissionContext,
    *,
    config=None,
    all_skill_configs=None,
) -> DreamInput:
    return DreamInput(
        skill="my_skill",
        trust="skill_generated",
        freshness={"generated_at": datetime.now(UTC), "expires_at": None},
        signals=[],
        limits=[],
    )
```

For callable chat tools in a lightweight skill, expose declarations and
executors from the same `tools.py`:

```python
def tool_declarations():
    return [
        {
            "name": "my_skill.check",
            "description": "Check whether the integration is ready.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "permission_class": "integration.read",
            "permission_scope": {"integrations": ["calendar"]},
            "trust": {"input": "local_mutable", "output": "skill_generated"},
            "executor": "my_skill.check",
        }
    ]


def build_executors(ctx):
    return {"my_skill.check": lambda args: _check(args, ctx)}
```

Use `integration.read` for read-only local integrations. Use
`integration.write` for maintenance or sync actions that update local caches,
configuration, or remote integration state.

Keep `tools.py` as the entry point. If the code grows, add helper modules or
small packages under the same skill folder and import them from `tools.py`.

For autonomous skills, prefer adding a local diagnostic/setup CLI to `tools.py`
when setup can fail:

```bash
python3 tools.py doctor
python3 tools.py write-config ...
```

Generated config should reference Maurice credentials or OS keyrings instead of
copying raw secrets into config files.

## Advanced skill.yaml

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

Use `skill.yaml` only when you need explicit tools, slash commands, runtime
dependencies, permissions, or non-default attachment names.

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

**Permission classes:** `fs.read`, `fs.write`, `network.outbound`,
`integration.read`, `integration.write`, `shell.exec`, `secret.read`,
`agent.spawn`, `host.control`, `runtime.write`.

The system `host` skill exposes Maurice administration diagnostics to agents.
Use `host.doctor` as the tool equivalent of `maurice doctor`, `host.logs` as the
tool equivalent of `maurice logs`, and `host.status` for live service status.
These are admin actions and remain gated by `host.control`.

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
should not scan for additional projects. Maurice records explicitly seen
projects in `~/.maurice/projects.json` and in the current agent's
`projects.json`, but remembered projects are history until the user opens or
selects one.

## Command declarations

Commands are slash commands dispatched by the host (not by the model).
`available_in` uses the same `local`/`global` scope vocabulary as skills: on a
skill it controls whether the skill is loaded, and on a command it controls
whether the command appears in `/help` and UI autocomplete for that scope.
Use `project_required: true` for commands that need a currently opened project.
Those commands are hidden from `/help` and the UI command list when no active
project exists, and direct invocation is refused with a clear message instead
of falling back to an arbitrary folder.

```yaml
commands:
  - name: /my_command
    description: What the command does.
    handler: my_package.my_skill.commands.my_command
    renderer: markdown    # "markdown" | "plain"
    available_in: [local, global]  # optional; hide from /help outside these scopes
    project_required: true          # optional; requires active_project_path/project_root
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
| `exec` | scoped shell command execution in the active project or workspace |
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
`/auto_update_list` and `/auto_update_apply <id> confirm`. The list command is
the review surface: it prints each pending proposal with its id, compact diff,
and exact apply command so Telegram/CLI users do not need to guess ids.

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
created. Scheduler timing, storage, delivery, and CLI configuration are covered
in [automations](automations.md).

Two optional dreaming-oriented skills are shipped as concrete examples of this
pattern:

- `workspace_dreaming` reads recent memories from each agent workspace and
  contributes a multi-agent recap to dreaming and daily
- `veille` stores watch topics under
  `<workspace>/agents/<agent-id>/veille/topics.json`, researches them through
  the configured SearxNG endpoint (`skills.web.base_url`, default
  `http://localhost:18080`), and contributes signals such
  as useful new technology, security issues in known dependencies, or news
  connected to active projects
