# Maurice Architecture

## Goal

Maurice is a modular agent system with a deliberately small kernel.

The architecture is split into three layers:

- `host`
- `kernel`
- `skills`

The rule is simple:

- the host prepares and exposes runtime infrastructure
- the kernel runs turns and enforces contracts
- skills provide capabilities


## Physical Layout

Maurice separates the runtime from the agent workspace.

The kernel and shipped runtime code live in a Maurice installation directory.
Agent work happens in a separate workspace directory.
The workspace directory is chosen by the user during onboarding.

By default, normal agent permissions are scoped to the workspace, not to the Maurice runtime directory.
The onboarding permission profile may allow broader scoped access, while runtime and system skill modification remain explicit high-trust actions.

Example shape:

```text
maurice-runtime/
  kernel/
  host/
  system_skills/

maurice-workspace/
  agents/
    main/
    coding/
  skills/
    user_skill/
  sessions/
  content/
```

The runtime directory is trusted and should be read-only during normal agent execution.
The workspace is mutable and contains agent state, user-created skills, content, and sub-workspaces.

This physical separation is a security boundary.
The kernel may load user skills from the workspace, but agents should not be able to modify the kernel or shipped system skills through normal tools.


## Layers

### Host

The host owns local operational concerns:

- install
- service startup
- local dependencies
- channels
- filesystem locations
- environment wiring

The host does not define agent behavior.


### Kernel

The kernel owns only the minimal runtime:

- turn loop
- provider abstraction
- tool-call execution contract
- session persistence
- event bus
- approvals
- background scheduling
- extension lifecycle
- channel-neutral ingress/egress contracts
- runtime state snapshots for monitoring

The kernel does not own domain capabilities.


### Skills

Skills own capabilities.

Examples:

- memory
- web
- cron
- vision
- delegation
- dashboard data providers

If a feature can be disabled without breaking the kernel, it should be a skill.


## Runtime Flow

One turn should look like this:

1. load session
2. resolve active skills
3. assemble kernel prompt + skill prompt fragments
4. stream model output
5. execute skill tools through the kernel contract
6. emit events
7. persist turn

There should be no special-case capability path outside this flow.


## Session And Memory Boundary

Maurice should keep a hard boundary between session and memory.

- session is short-horizon turn history
- memory is a skill-owned semantic layer
- session compaction must not invent memory semantics
- resetting a session must not implicitly erase memory

This boundary should stay explicit in code, docs, and UI.


## Capability Exposure Rule

If the agent can do something meaningful for the user, that capability should be exposed as a declared tool.

This keeps the system:

- testable
- observable
- composable
- enforceable by policy

It also prevents the architecture from drifting toward hidden power in the kernel.


## Extension Model

All extensions use one contract.

A skill may provide:

- prompt fragments
- tools
- event subscribers
- background hooks
- dream hooks
- optional backend
- optional storage

The kernel interacts with all of them through the same interfaces.


## Approvals And Policy

Approval behavior belongs to the kernel, not to individual features.

The kernel should own:

- pending approval lifecycle
- approval scopes
- approval TTL rules
- remembered approvals
- category-level policy enforcement

Skills may request sensitive actions, but they should not define approval semantics by themselves.


## Storage Shape

Storage is split by ownership:

- kernel storage
- skill storage
- host storage

The kernel should not become the owner of every persistent concept.


## Background Work

Background work is generic in the kernel.

The kernel may schedule:

- recurring jobs
- dream runs
- cleanup
- delivery tasks

But the meaning of the work usually belongs to skills.


## Channels And Host Policy

Channels should stay generic and thin.

The kernel should define only:

- inbound message shape
- outbound message shape
- correlation and session routing hooks

Anything channel-specific such as onboarding flavor, slash commands, formatting quirks, or delivery policy should live in host adapters or skills rather than inside the turn loop itself.


## Events, State, And Dashboard

Maurice should prefer structured events and generic state snapshots over hard-coded dashboard branches.

The kernel should expose:

- append-only runtime events
- typed state snapshots
- agent state
- skill health/state payloads
- approval and job state

The dashboard should mostly render these generic structures rather than encode feature-specific logic.


## Anti-Goals

Maurice should avoid:

- a giant central tools file
- separate extension systems for “core” and “user” features
- hidden behavior in prompts that should be runtime contracts
- dashboard logic tied to hard-coded feature branches


## Agent-Accessible Docs

Foundational docs are not just for developers.

They should also be consumable by the agent at runtime in compact form.

This means:

- kernel rules should exist as stable docs
- skill contracts should be readable by skills and by the agent runtime
- docs should be concise enough to inject or summarize
- no critical rule should exist only in a human-only README or in scattered code comments
