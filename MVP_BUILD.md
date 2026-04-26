# Maurice MVP Build

## Purpose

This document translates the roadmap into a concrete implementation backlog.

The MVP should prove the architecture with the smallest useful system:

- clean kernel
- workspace-selected host setup
- one default `main` agent
- system skills loaded through the same skill pipeline as user skills
- scoped permissions and approvals
- structured events
- filesystem, memory, dreaming, and skill-authoring system skills


## Target Package Shape

```text
maurice/
  kernel/
    __init__.py
    config.py
    contracts.py
    providers.py
    loop.py
    session.py
    events.py
    approvals.py
    permissions.py
    skills.py
    scheduler.py
  host/
    __init__.py
    cli.py
    onboard.py
    gateway.py
    credentials.py
    workspace.py
    channels/
      __init__.py
  system_skills/
    filesystem/
      skill.yaml
      prompt.md
      tools.py
      dreams.md
    memory/
      skill.yaml
      prompt.md
      tools.py
      dreams.md
      migrations/
    dreaming/
      skill.yaml
      prompt.md
      tools.py
    skills/
      skill.yaml
      prompt.md
      tools.py
```


## Phase 0: Repo Skeleton

Deliverables:

- `pyproject.toml`
- package directories
- test directory
- basic CLI entrypoint
- lint/test command

Acceptance:

- `maurice --help` runs
- tests can import `maurice.kernel`
- no runtime side effects on import


## Phase 1: Contracts In Code

Implement typed models from `CONTRACTS.md`.

Files:

- `maurice/kernel/contracts.py`

Models:

- `ProviderChunk`
- `ToolDeclaration`
- `ToolResult`
- `PermissionRule`
- `PermissionScope`
- `Event`
- `PendingApproval`
- `SkillManifest`
- `AgentConfig`
- `SubagentRun`
- `DreamInput`
- `DreamReport`

Acceptance:

- models validate the examples in `CONTRACTS.md`
- invalid permission classes fail validation
- tool result success and error envelopes share the same shape


## Phase 2: Config And Workspace

Implement config loading and workspace initialization.

Files:

- `maurice/kernel/config.py`
- `maurice/host/workspace.py`
- `maurice/host/credentials.py`

Deliverables:

- load defaults
- load host/kernel/agents/skills config
- resolve workspace root
- initialize workspace directories
- load typed credentials without placing secrets in normal config

Acceptance:

- onboarding can choose a workspace root
- workspace creates `agents/`, `skills/`, `sessions/`, `artifacts/`, `config/`
- runtime root and workspace root are distinct
- secrets are loaded only from credentials store


## Phase 3: Events And Sessions

Implement event store and session store.

Files:

- `maurice/kernel/events.py`
- `maurice/kernel/session.py`

Deliverables:

- append-only JSONL event store
- per-agent event stream
- session history storage
- session reset
- correlation ids

Acceptance:

- turn events are persisted
- tool events include correlation id
- resetting a session does not affect skill storage


## Phase 4: Permissions And Approvals

Implement permission profile resolution and approval lifecycle.

Files:

- `maurice/kernel/permissions.py`
- `maurice/kernel/approvals.py`

Deliverables:

- `safe`, `limited`, `power` profiles from `SECURITY.md`
- per-agent profile override
- scope validation
- pending approval store
- approval replay fingerprint
- approval events

Acceptance:

- workspace write in `safe` asks or allows according to matrix
- runtime write always produces proposal flow by default
- approval replay fails if arguments or scope change
- more permissive agent profile requires explicit config flag or confirmation marker


## Phase 5: Skill Loader

Implement the strict skill lifecycle.

Files:

- `maurice/kernel/skills.py`

Deliverables:

- discover system and user roots
- parse `skill.yaml`
- reject name collisions
- resolve required and optional dependencies
- track skill states
- load prompt fragments
- load `dreams.md`
- register tool declarations

Acceptance:

- user `memory` cannot shadow system `memory`
- broken optional skill becomes `disabled_with_error`
- broken required skill blocks startup
- reload applies only to future turns


## Phase 6: Provider And Turn Loop

Implement the smallest useful agent loop.

Files:

- `maurice/kernel/providers.py`
- `maurice/kernel/loop.py`

Deliverables:

- provider interface
- mock provider for tests
- OpenAI-compatible provider stub
- Ollama-compatible provider stub
- one-turn execution
- tool call execution through registered tools
- event emission
- session persistence

Acceptance:

- mock provider can call a filesystem tool
- tool execution goes through permissions
- turn output persists in session
- no skill-specific logic in loop


## Phase 7: Filesystem System Skill

Implement first real system skill.

Files:

- `maurice/system_skills/filesystem/skill.yaml`
- `maurice/system_skills/filesystem/tools.py`
- `maurice/system_skills/filesystem/prompt.md`
- `maurice/system_skills/filesystem/dreams.md`

Tools:

- `filesystem.list`
- `filesystem.read`
- `filesystem.write`
- `filesystem.mkdir`

Acceptance:

- paths are scoped by permission rules
- writes outside workspace are denied or require approval according to profile
- tool results use `ToolResult`


## Phase 8: Host CLI And Onboarding

Implement CLI enough to initialize and run.

Files:

- `maurice/host/cli.py`
- `maurice/host/onboard.py`
- `maurice/host/gateway.py`

Commands:

- `maurice onboard`
- `maurice run`
- `maurice doctor`

Acceptance:

- onboarding writes host/kernel/agents/skills config
- user chooses workspace root
- user chooses permission profile
- `maurice run --message "..."` executes one turn


## Phase 9: Memory System Skill

Implement minimal durable memory outside kernel.

Files:

- `maurice/system_skills/memory/skill.yaml`
- `maurice/system_skills/memory/tools.py`
- `maurice/system_skills/memory/dreams.md`
- migrations

Tools:

- `memory.remember`
- `memory.search`
- `memory.get`

Acceptance:

- memory storage is under workspace skill storage
- kernel does not import memory internals
- session reset does not delete memory
- memory emits structured tool results


## Phase 10: Dreaming System Skill

Implement minimal dream run.

Files:

- `maurice/system_skills/dreaming/skill.yaml`
- `maurice/system_skills/dreaming/tools.py`
- `maurice/system_skills/dreaming/prompt.md`

Deliverables:

- enumerate active skill dream inputs
- load `dreams.md`
- produce `DreamReport`
- validate proposed actions through owner skill hook
- emit dream events

Acceptance:

- dreaming works with memory enabled
- dreaming degrades if memory disabled
- proposed actions are not executed without validation and approval


## Phase 11: Skill Authoring System Skill

Implement user skill creation.

Tools:

- `skills.create`
- `skills.reload`
- `skills.list`

Acceptance:

- creates user skills only under workspace skill root
- cannot write system skills
- reload affects future turns only
- collisions are rejected


## Phase 12: Self-Update Proposals

Implement proposal generation, not runtime application.

Deliverables:

- proposal directory creation
- `proposal.yaml`
- patch attachment
- risk and test plan files

Acceptance:

- agent can create proposal under workspace
- proposal uses `runtime.write` with `proposal_only`
- no runtime files are changed by agent tools


## MVP Exclusions

Not in MVP:

- full dashboard
- rich TUI
- Telegram integration
- full Jarvis migration
- vision
- cron/reminders beyond generic scheduler
- direct runtime self-apply
- permanent multi-agent UI
- remote plugin marketplace


## First Coding Milestone

The first milestone is intentionally small:

1. package skeleton
2. typed contracts
3. config/workspace initialization
4. event and session stores
5. permission matrix
6. skill loader with one fake skill

After this milestone, the kernel shape should be visible before product features accumulate.
