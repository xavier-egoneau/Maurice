# Maurice Roadmap

## Purpose

This document turns the current design decisions into an implementation roadmap.

Maurice is a reset of Jarvis, not a feature downgrade.
The goal is to keep the useful product behavior proven by Jarvis while replacing hard-coded feature paths with clear contracts.


## Validated Decisions

### 1. Runtime And Workspace Separation

Maurice runtime and agent workspaces live in separate roots.

- runtime root contains kernel, host code, and system skills
- workspace root is chosen during onboarding
- workspace root contains agents, sessions, artifacts, and user skills
- agents write inside the workspace by default
- runtime and system skill writes require explicit high-trust approval


### 2. One Skill Contract

Maurice has one skill mechanism.

System skills and user skills use the same runtime contract.
They differ by origin, trust, mutability, distribution, and test coverage.

- system skills are shipped with Maurice and read-only during normal execution
- user skills live under the workspace and may be authored by agents when policy allows
- user skills must not silently replace system skills


### 3. Memory And Dreaming

Memory and dreaming are system skills, not kernel concepts.

They are shipped by default and may be enabled by default, but the kernel should not understand memory semantics.

- kernel schedules background jobs
- dreaming skill defines what a dream run means
- memory skill owns durable memory storage and retrieval
- Maurice global memory is a first-class dream source, not a secondary optional signal
- other skills expose dream inputs through their own contracts


### 4. Dreams Attachment

Each skill may provide a `dreams.md` attachment.

`dreams.md` explains what the dreaming pipeline may use from that skill:

- available data
- useful signals
- candidate actions
- freshness and trust assumptions
- limits and uncertainties

The dreaming pipeline should not infer hidden meaning from skill storage.


### 5. Permission Profiles

The user chooses a permission profile during onboarding.

Profiles set defaults, not bypasses.
All tool calls still pass through declarations, scoped permissions, trust labels, and approval records.

- `safe`: workspace-first, conservative shell/network/secrets/spawn/host control
- `limited`: workspace-first, broader access through scoped approvals
- `power`: broad access with fewer prompts, while runtime writes remain explicit high-trust actions


### 6. Permanent Agents And Subagent Runs

Permanent agents are durable peers.
`main` is the default agent, not the only possible durable agent.

Subagent runs are temporary task executions.
They should support checkpointing and controlled interruption rather than normal hard kills.


### 7. Host, Kernel, Skill Split

- host owns install, service startup, channels, local paths, and environment wiring
- kernel owns turn execution, providers, tools contract, sessions, events, approvals, extension lifecycle, and generic background scheduling
- skills own capabilities, storage, dream inputs, tools, and domain behavior


## Specs To Write Before Code

### 1. Permission Scope Schema

Define exact schemas for:

- `fs.read`
- `fs.write`
- `network.outbound`
- `shell.exec`
- `secret.read`
- `agent.spawn`
- `host.control`
- `runtime.write`

The schema must support scoped approvals by path, host, command, credential, agent, host action, or runtime target.


### 2. Tool Result Envelope

Every tool must return the same envelope shape.

Required fields:

- `ok`
- `summary`
- `data`
- `trust`
- `artifacts`
- `events`
- `error`

The model should mostly consume `summary`.
The runtime, dashboard, tests, and audit layer should consume the structured fields.


### 3. Event Schema

Define a stable event envelope:

- `id`
- `time`
- `kind`
- `name`
- `origin`
- `agent_id`
- `session_id`
- `correlation_id`
- `payload`

V1 event names should cover turns, tools, approvals, skills, agents, dreams, and host lifecycle.


### 4. Skill Manifest

Define `skill.yaml`.

Required areas:

- identity
- origin
- mutability
- version
- config namespace
- exported tools
- permissions requested
- optional backend
- optional storage
- optional dreams attachment
- optional event/state publishers


### 5. Skill Loading Contract

Define the loader phases:

1. discover roots
2. read manifests
3. validate schema
4. reject collisions
5. resolve dependencies
6. initialize storage
7. start backends
8. register tools
9. register dream hooks
10. publish skill health

Skill states:

- `loaded`
- `disabled`
- `disabled_with_error`
- `missing_dependency`
- `migration_required`

A broken non-required skill should degrade to `disabled_with_error`.
A broken required skill may block startup.


### 6. Agent Config And Subagent Run Contract

Define durable agent config:

- id
- default flag
- workspace
- enabled skills
- permission profile
- channels
- model override
- event stream

Define subagent run config:

- parent agent
- base agent or profile
- task
- workspace
- scoped permissions
- budget
- checkpoint requirement
- output envelope


### 7. Dreams Pipeline

Define the full pipeline:

1. kernel background runner triggers a dream job
2. dreaming system skill enumerates active skills
3. dreaming connects to Maurice global memory when the memory skill is available
4. dreaming loads each `dreams.md`
5. each skill provides `dream_inputs`
6. dreaming assembles memory context plus skill signals
7. provider generates structured report
8. owner skill validates proposed actions
9. approval gate applies policy
10. report and events are persisted


### 8. Storage And Migrations

Each skill that stores data owns:

- schema
- storage path
- schema version
- migrations
- health checks

The loader orchestrates migrations.
The kernel does not understand skill tables.


### 9. Onboarding Contract

Onboarding writes four areas:

- host config: runtime root, workspace root, channels, gateway, skill roots
- kernel config: model, enabled skills, permission profile, approvals
- credentials: provider keys, channel tokens, backend secrets
- workspace bootstrap: agents, sessions, artifacts, user skills directory

Secrets never go into normal config.


### 10. Runtime Self-Update

Agents do not modify runtime or system skills directly during normal execution.

The detailed workflow is defined in `SELF_UPDATE.md`.

Workflow:

1. agent writes a proposal under workspace
2. proposal contains patch, reason, and risk
3. user explicitly approves
4. host applies the runtime change


## Jarvis Lessons To Keep

Jarvis already proves these product ideas are useful:

- long-lived gateway/runtime
- tool-using agent loop
- workspace chosen during onboarding
- security profiles
- approvals with TTL and action fingerprints
- persistent memory
- dreaming over memory and skill signals
- skill authoring from the agent
- user skills under workspace
- core/system skills shipped with the runtime
- Telegram and web gateway channels
- worker-style parallel task execution
- structured JSON logs
- backend processes for skills


## Jarvis Patterns Not To Copy Directly

Maurice should avoid these Jarvis shapes:

- one giant central tools file
- feature config accumulating in one global settings schema
- memory hardwired into gateway startup
- dreaming hardwired into memory internals
- first-wins skill collision behavior
- core/user skill split as separate runtime semantics
- tool results as arbitrary prose only
- command policy hidden in prompts
- channel-specific behavior leaking into the turn loop
- agents modifying runtime config through ordinary workspace-like tools


## Implementation Order

The concrete MVP backlog is tracked in `MVP_BUILD.md`.

### Phase 1: Contracts

- permission scope schema
- tool result envelope
- event schema
- skill manifest
- agent and subagent schemas


### Phase 2: Minimal Kernel

- config loading
- provider abstraction
- session store
- event store
- approval gate
- tool execution contract
- skill loader skeleton


### Phase 3: Host And Workspace

- onboarding
- runtime/workspace root setup
- credentials store
- gateway startup
- channel-neutral ingress/egress
- workspace bootstrap


### Phase 4: System Skills

- filesystem skill
- memory skill
- dreaming skill
- skills authoring skill
- web skill if kept as a shipped default


### Phase 5: Agents

- durable agent config
- main agent
- additional permanent agents
- subagent run contract
- checkpoint and resume behavior


### Phase 6: Migration From Jarvis

Migrate only stable user-owned data:

- credentials where possible
- user skills where possible
- selected memory export if schema can be mapped
- workspace artifacts when useful

Do not migrate raw Jarvis config or sessions directly.


## V1 Recommendation

Maurice v1 should include:

- one clean kernel
- host onboarding
- workspace root selection
- `safe`, `limited`, `power` profiles
- one default `main` agent
- config model ready for multiple permanent agents
- filesystem system skill
- memory system skill
- dreaming system skill
- skill authoring for user skills
- provider support for OpenAI-compatible and Ollama-compatible backends
- structured events and approvals

Everything else should prove itself as a skill.
