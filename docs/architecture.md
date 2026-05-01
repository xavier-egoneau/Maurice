# Architecture

## Three-layer model

```
kernel/     — turn loop, contracts, permissions, sessions, events
host/       — CLI, daemon, channels, onboarding, delivery
skills/     — capabilities (filesystem, memory, web, dev, …)
```

**Boundary rules:**
- The kernel has no knowledge of skills or channels. It consumes contracts.
- The host wires everything together but does not define behavior.
- Skills provide capabilities through the kernel's tool/command contracts.
- Skills cannot import from `host/`. They receive what they need through `SkillContext`.

## Physical layout

Two separate directories always exist:

```
<runtime>/          — read-only during normal agent execution
  kernel/
  host/
  system_skills/

<workspace>/        — mutable, owned by the agent
  agents/
    <agent-id>/
      content/      — user files, projects
      sessions/     — turn history
      events.jsonl  — append-only event log
      approvals.json
  skills/           — user-installed skills
```

The separation is a security boundary. Agents cannot modify the runtime through normal tools.

## Config files (in `<workspace>/`)

```
host.yaml       — runtime_root, workspace_root, gateway, skill_roots, channels
kernel.yaml     — model, permissions, approvals, skills, scheduler, sessions, events
agents.yaml     — list of agent definitions
skills.yaml     — per-skill config namespaces
```

## Module map

| Module | Responsibility |
|---|---|
| `kernel/loop.py` | `AgentLoop.run_turn` — the main turn loop |
| `kernel/contracts.py` | All typed contracts (ToolDeclaration, ProviderChunk, …) |
| `kernel/skills.py` | Skill discovery, `SkillRegistry`, `SkillContext` |
| `kernel/permissions.py` | Profile rules, `evaluate_permission` |
| `kernel/approvals.py` | Approval store and lifecycle |
| `kernel/classifier.py` | LLM-based auto-approval classifier |
| `kernel/shell_parser.py` | Fail-closed shell command risk analysis |
| `kernel/compaction.py` | Automatic context compaction (3 levels) |
| `kernel/session.py` | Session persistence |
| `kernel/events.py` | Append-only event store |
| `host/runtime.py` | `run_one_turn` — wires config → loop |
| `host/cli.py` | argparse dispatcher |
| `host/commands/` | CLI subcommand implementations |
| `host/telegram.py` | Telegram API calls |
| `host/delivery.py` | Reminder and daily digest delivery |
| `host/gateway.py` | Local HTTP + Telegram poll gateway |
