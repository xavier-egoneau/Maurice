# Maurice

## Intent

Maurice is a rewrite of the Jarvis idea with one constraint above all:

- smaller core
- fewer files
- clearer boundaries
- equivalent end-user capabilities, but pushed outward into skills and dreams

The kernel should stay boring, inspectable, and easy to reason about.
Anything that is optional, domain-specific, or expensive to maintain should live outside the kernel.

The implementation roadmap is tracked in `ROADMAP.md`.


## Why A Rewrite

Jarvis proves the product is viable.
It already has the important pieces:

- long-lived gateway/runtime
- tool-using agent loop
- persistent memory
- proactive background work
- skill loading
- multi-provider support
- channel integrations

But the current codebase concentrates too much policy and product behavior into a few giant files:

- `jarvis/agent/tools.py`
- `jarvis/cli/onboard.py`
- `jarvis/config/settings.py`
- `jarvis/memory/dreaming.py`

The result is operational, but harder to evolve cleanly.


## Critique Of Jarvis

### 1. The kernel is not small enough

Jarvis says it is modular, but too much core behavior is still hard-coded in the kernel:

- web search behavior
- vision behavior
- cron behavior
- memory discipline
- onboarding defaults
- security approval semantics
- worker orchestration details

A lot of this belongs in extensions, not in the runtime center.


### 2. `tools.py` is acting like an entire application

`jarvis/agent/tools.py` is more than a tool registry.
It currently mixes:

- tool definitions
- tool execution
- web scraping/search adapters
- filesystem helpers
- security enforcement
- worker spawning
- content/project conventions
- model switching
- backend reloads

This is the strongest signal that the design wants another layer of decomposition.


### 3. The prompt/context system carries too much protocol

`jarvis/agent/context.py` is effective, but it has become a policy accumulator.
It injects:

- memory rules
- untrusted-content rules
- workspace conventions
- vision behavior
- tool protocol
- runtime clock rules
- skill material

That works, but it means behavior is split between code and prompt law.
Maurice should prefer fewer universal prompt rules and more explicit runtime contracts.


### 4. “Core vs skill” is still blurry

Jarvis has both:

- native core capabilities in `jarvis/core/`
- user skills in `jarvis/skills/`

That split is understandable historically, but architecturally it creates duplication:

- some capabilities feel like built-ins but are shaped like skills
- some product features still bypass the skill model entirely

Maurice should make one extension model the default.
A thing should be either:

- kernel
- host adapter
- skill

Not half-core, half-skill.


### 5. Configuration is too broad and too central

`jarvis/config/settings.py` is doing a lot:

- providers
- agent tiers
- permissions
- security profiles
- scheduler config
- web search config
- channel config
- context config

It is useful, but it also becomes the place where every new feature lands.
Maurice should keep kernel config tight and move feature config into skill manifests.


### 6. Onboarding/install currently carry product logic, infra logic, and provider logic together

Jarvis onboarding has become a smart wizard.
That is good for users, but risky for maintainability.

The install/onboard split we just introduced is the right direction:

- install prepares infrastructure
- onboard chooses product behavior

Maurice should preserve that discipline from day one.


### 7. Dreaming is powerful but too specialized inside the core

Jarvis dreaming is one of the most interesting parts of the project.
But it currently lives deep in core memory architecture, with a lot of product-specific behavior mixed in.

Maurice should treat dreams as an extension pipeline:

- the kernel schedules dream runs
- skills contribute dream context, evaluators, and actions
- the kernel only stores outputs and executes approved actions


### 8. The system is modular in practice, but not minimal in shape

Jarvis has many modules, but not always many clean boundaries.
It is modular by filesystem layout more than by runtime contract.

Maurice should optimize for:

- fewer modules
- sharper contracts
- more evented behavior
- less “utility gravity” in mega-files


## Maurice Design Principles

### 1. Tiny kernel

The kernel should only own:

- turn execution loop
- model/provider abstraction
- tool-calling protocol
- session store
- event bus
- extension loader
- approval gate
- background job runner


### 2. Everything optional is a skill

These should be skills, not kernel features:

- memory
- web search
- web fetch
- cron/reminders
- vision
- channel-specific helpers
- file project scaffolding
- agent delegation helpers
- dashboards and domain behaviors


### 3. Dreams are first-class extension hooks

Each skill may contribute:

- runtime prompt fragment
- tool set
- state store
- dream evaluator
- proposed actions

Dreaming is not a monolith.
It is a pipeline over installed skills.


### 4. One extension contract

Every skill should use one manifest and one lifecycle model:

- metadata
- prompts
- tools
- optional backend
- optional dream hooks
- optional storage schema


### 5. Explicit runtime contracts over implicit prompt discipline

Prefer:

- typed tool outputs
- structured events
- clear trust metadata

Over:

- ever-growing prompt instructions
- behavioral conventions hidden in prose


### 6. Host shell separate from agent kernel

Maurice should distinguish:

- `host`: install, services, channels, local infra
- `kernel`: agent runtime
- `skills`: capabilities

This keeps product behavior from leaking into the runtime center.


## Proposed Top-Level Shape

Keep the repo intentionally small.

```text
Maurice/
  README.md
  SPEC.md
  pyproject.toml
  maurice/
    kernel/
      loop.py
      session.py
      providers.py
      approvals.py
      events.py
      extensions.py
      scheduler.py
      config.py
    host/
      cli.py
      gateway.py
      channels/
    system_skills/
      memory/
      dreaming/
      filesystem/
      web/
```

Target:

- kernel code in a handful of files
- skills each self-contained
- no single file equivalent to the current `jarvis/agent/tools.py`


## Kernel Runtime Model

### Kernel responsibilities

The kernel owns one turn:

1. load session
2. resolve active skills
3. build compact system context from kernel + skills
4. call provider
5. execute tool calls through registered skill tools
6. emit events
7. persist turn


### Event bus

Everything important emits events:

- `turn.started`
- `turn.completed`
- `tool.requested`
- `tool.started`
- `tool.completed`
- `tool.failed`
- `approval.requested`
- `approval.resolved`
- `dream.started`
- `dream.completed`
- `skill.backend.started`

Skills can subscribe without patching kernel flow.


### Tool model

Tools are registered by skills.
The kernel only knows:

- tool schema
- trust level
- permission class
- executor callback

The kernel should not embed tool-specific business logic.


## Skills

Each skill should be a folder with one manifest.

Example:

```text
skills/web/
  skill.yaml
  prompt.md
  tools.py
  dreams.md
  backend.py
```

`skill.yaml` should declare:

- name
- version
- dependencies
- required binaries
- tools exported
- backend entrypoint
- storage needs
- dream hooks


## Memory As A Skill

Maurice should make memory a first-class skill, not a universal hidden assumption.

That means:

- memory search/write/get come from the memory skill
- memory storage implementation can evolve independently
- dream consolidation over memory is contributed by that skill
- projects without durable memory can disable it cleanly

The kernel may still expose a tiny “state API”, but semantic memory belongs outside.


## Dreams

Dreams are background passes that work over skill outputs.

Base dream pipeline:

1. gather dream inputs from active skills
2. build one compact review context
3. ask model for synthesis/proposals
4. hand proposals back to owning skills
5. materialize approved local actions

Kernel responsibilities:

- scheduling
- storage of reports
- approval boundary

Skill responsibilities:

- what signals matter
- what actions are legal
- how results are stored


## Security

Keep a smaller security model than Jarvis.

Start with a compact matrix:

- `fs.read`
- `fs.write`
- `shell.exec`
- `network.outbound`
- `secret.read`
- `agent.spawn`
- `host.control`
- `runtime.write`

Each tool maps to one class.
Avoid duplicating policy logic across tools.


## Configuration

Split config by ownership.

### Kernel config

- primary model/provider
- session settings
- approval mode
- permission profile
- enabled skills


### Host config

- runtime root
- workspace root
- gateway bindings
- channel transport settings
- skill roots


### Agent config

- permanent agent identities
- agent workspaces
- agent skills
- agent permission profiles
- agent channel bindings


### Skill config

Owned by each skill in its own namespace.

Example:

```yaml
kernel:
  model:
    provider: ollama
    name: minimax-m2.7
  permissions:
    profile: safe
  skills:
    - memory
    - dreaming
    - web

host:
  runtime_root: /opt/maurice
  workspace_root: ~/.maurice/workspace

agents:
  main:
    default: true
    workspace: "$workspace/agents/main"
    skills: ["filesystem", "memory", "dreaming", "web"]
    permission_profile: safe

skills:
  web:
    provider: searxng
    base_url: http://localhost:8080
  memory:
    backend: sqlite
```

This keeps the kernel config from becoming a dumping ground.


## Install And Onboarding

Maurice should treat install and onboarding as separate products.

### `maurice install`

Responsibilities:

- check prerequisites
- prepare recommended local infra
- install/start service dependencies
- verify health

Example:

- launch SearxNG locally
- prepare local storage
- verify optional OCR/vision backend


### `maurice onboard`

Responsibilities:

- choose model provider
- choose enabled skills
- choose defaults
- connect channels

Onboarding should assume infra is already prepared whenever possible.


## Multi-Agent Story

Maurice should support permanent agents and temporary task runs.

Permanent agents are configured peers of `main`.
Subagent runs are scoped temporary executions.

Start with:

- one default `main` permanent agent
- config shape ready for additional permanent agents
- subagent runs with explicit task, scope, permissions, budget, and checkpoint

Do not bake a rigid hierarchy of agent tiers too early.


## Explicit Non-Goals For v1

- no giant TUI
- no too-smart onboarding wizard
- no separate runtime path for system skills and user skills
- no multiple overlapping policy systems
- no all-in-one mega tools file
- no giant context protocol file unless proven necessary


## Migration Mindset

Maurice is not “Jarvis but renamed”.
It should preserve the value and discard the accidental shape.

Keep:

- tool-using local-first assistant
- memory continuity
- dreams/proactivity
- pluggable providers
- workspace-centered operation

Change:

- much smaller kernel
- clearer extension boundaries
- fewer central files
- capabilities moved to skills
- install before onboard


## First Build Order

### Phase 1

- kernel loop
- provider abstraction
- session store
- extension loader
- approval gate
- simple CLI


### Phase 2

- memory skill
- web skill with SearxNG
- filesystem skill
- basic dream pipeline


### Phase 3

- cron skill
- vision skill
- delegation skill
- channel adapters


## Success Criteria

Maurice v1 is successful if:

- a new contributor can understand the kernel in one sitting
- adding a new capability usually means adding a skill, not editing the kernel
- no file becomes the new `tools.py`
- install + onboard produce a working local-first agent quickly
- dreams remain powerful without bloating the core
