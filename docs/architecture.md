# Architecture

Maurice is one runtime with two context levels. Folder and desktop-assistant
usage do not create two projects or two agent stacks; they resolve different
`MauriceContext` instances and feed the same kernel, server, permission,
session, event, memory, and skill machinery.

## Layers

```text
kernel/          turn loop, contracts, permissions, sessions, events
host/            CLI, context resolution, server lifecycle, channels, onboarding
system_skills/   shipped capabilities
```

Boundary rules:

- The kernel has no knowledge of channels or host commands. It consumes typed
  contracts.
- The host resolves context and wires providers, skills, sessions, events, and
  approvals into `AgentLoop`.
- Skills provide capabilities through the kernel's tool and command contracts.
- Skills do not import from `host/`; they receive runtime hooks through
  `SkillContext`.

## MauriceContext

`MauriceContext` is the canonical host contract for an execution surface.

It carries:

- `scope`: `local` or `global`
- `lifecycle`: `transient` or `daemon`
- `runtime_root`: Maurice installation/runtime code
- `context_root`: folder or global workspace being served
- `state_root`: where Maurice stores state for that context
- `content_root`: user-facing files for that context
- `active_project_root`: project folder used for relative work, when one is
  active
- sessions, events, approvals, memory, server pid/socket/meta paths
- config, enabled skills, skill config, skill roots

The central rule is simple: once a context is resolved, host wiring should use
`ctx.*` instead of re-deriving paths from legacy names.

## Folder Context

Folder context is project-centered and transient. It is used when a user stands
in a folder and launches Maurice directly, like a code assistant focused on that
folder.

```text
<project>/
  .maurice/
    config.yaml
    sessions/
    events.jsonl
    approvals.json
    memory.sqlite
    run/
      server.socket
      server.pid
      server.meta
  skills/              optional project skills
```

At this level:

- `context_root`, `content_root` and the permission `$workspace` all point at
  the project folder.
- memory is `./.maurice/memory.sqlite`.
- `./skills` and configured user skill roots can be loaded alongside system
  skills.
- the local server is transient and can be reused while its `server.meta` stays
  compatible.
- `maurice web` in folder usage exposes the same folder context through the HTTP
  gateway; it does not require or create an assistant workspace.

## Desktop Assistant Context

Desktop assistant context is workspace-centered and daemon-oriented. It is used
by `maurice start`, gateway, scheduler, dashboard, channels, durable agents, and
`maurice run`. `maurice start` is a daemon command, not an implicit context
expansion: it only uses the configured workspace when setup selected global
usage, or a workspace passed explicitly with `--workspace`.
`maurice web` uses this desktop context only when setup selected global usage,
or when `--workspace` is passed explicitly. In that case the workspace remains
the state root, while the folder where the command was launched is captured as
`active_project_root`, even if it is outside the workspace.

```text
<workspace>/
  agents/
    <agent-id>/
      content/
      events.jsonl
      approvals.json
      jobs.json
  content/
  sessions/
  skills/
    memory/
      memory.sqlite
  skills.yaml
  run/
    server.socket
    server.pid
    server.meta
```

Host-owned global config lives outside the mutable workspace:

```text
~/.maurice/
  config.yaml
  credentials.yaml
  skills/
  workspaces/<workspace-key>/config/
    host.yaml
    kernel.yaml
    agents.yaml
```

At this level:

- `context_root`, `state_root`, `content_root` and `$workspace` point at the
  assistant workspace.
- `$project` points at `active_project_root` when one is available; otherwise it
  falls back to the agent content project selected by legacy dev state.
- sessions are shared under `<workspace>/sessions`.
- per-agent events and approvals live under `<workspace>/agents/<agent-id>/`.
- memory is `<workspace>/skills/memory/memory.sqlite`.
- the daemon server writes compatibility metadata to
  `<workspace>/run/server.meta`.

## Lifecycle

```text
local/transient   one folder-centered server, started on demand, idle-stopped
global/daemon     one workspace-centered server, used by services and channels
```

Both lifecycles use `MauriceServer(ctx)`.

The daemon path also writes service metadata so `service status` can report
whether the running process matches the current context. Gateway turns route
through the global server when it is running; scheduler jobs build their handlers
from the same global context.

## Config

Local folder config:

```text
<project>/.maurice/config.yaml
```

Global workspace config:

```text
~/.maurice/workspaces/<workspace-key>/config/host.yaml
~/.maurice/workspaces/<workspace-key>/config/kernel.yaml
~/.maurice/workspaces/<workspace-key>/config/agents.yaml
<workspace>/skills.yaml
```

`skills.yaml` remains workspace-owned because it configures mutable user skills.
Credentials live in `~/.maurice/credentials.yaml`.

## Module Map

| Module | Responsibility |
|---|---|
| `host/context.py` | `MauriceContext`, local/global context resolution, command callbacks |
| `host/server.py` | `MauriceServer(ctx)` socket server for local and global lifecycles |
| `host/client.py` | socket client and server compatibility checks |
| `host/runtime.py` | `build_global_agent_loop`, `run_one_turn` public wrapper |
| `host/commands/service.py` | daemon lifecycle and service metadata |
| `host/commands/gateway_server.py` | gateway routing through the global server when available |
| `host/commands/scheduler.py` | scheduler jobs wired from global context |
| `kernel/loop.py` | `AgentLoop.run_turn` |
| `kernel/contracts.py` | typed contracts |
| `kernel/skills.py` | skill discovery, `SkillRegistry`, `SkillContext` |
| `kernel/permissions.py` | profile rules and scope variable evaluation |
| `kernel/session.py` | session persistence |
| `kernel/events.py` | append-only event store |
