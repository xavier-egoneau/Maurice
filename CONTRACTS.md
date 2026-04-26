# Maurice Contracts

## Purpose

Maurice should be governed by explicit contracts rather than accumulated conventions.

These contracts should be readable both by humans and by the runtime/agent layer.

Every important boundary must define:

- inputs
- outputs
- ownership
- failure mode


## Kernel Contracts

### Provider contract

A provider must support:

- `stream(messages, model, tools, system, limits) -> stream of chunks`

Provider output must be normalized into:

- text deltas
- tool calls
- usage metadata
- terminal status

Provider stream chunks should normalize into this shape:

```yaml
type: "text_delta|tool_call|usage|status"
delta: ""
tool_call:
  id: "call_..."
  name: "skill.tool"
  arguments: {}
usage:
  input_tokens: 0
  output_tokens: 0
status: "running|completed|failed"
error: null
```


### Tool contract

Every tool must declare:

- stable canonical name
- schema
- permission class
- trust level
- owning skill
- executor callback

Every tool must return a structured result envelope, not arbitrary prose alone.

All product capabilities available to the agent must be exposed through this declared tool surface.

The kernel should not hide real end-user capabilities behind side channels or special-case code paths.

The only acceptable non-tool internals are:

- kernel primitives
- lifecycle mechanics
- host-only operations that are not agent capabilities

Canonical tool names use:

```text
<skill>.<tool>
```

Examples:

```text
filesystem.read
filesystem.write
memory.remember
web.search
dreaming.run
```

Tool declarations should use this shape:

```yaml
name: "filesystem.write"
owner_skill: "filesystem"
description: "Write a text file inside an allowed scope."
input_schema: {}
permission:
  class: "fs.write"
  scope: {}
trust:
  input: "local_mutable"
  output: "local_mutable"
executor: "tools.write"
```

Tool results must use this envelope:

```yaml
ok: true
summary: "File written."
data:
  path: "$workspace/notes/today.md"
trust: "local_mutable"
artifacts:
  - type: "file"
    path: "$workspace/notes/today.md"
events:
  - name: "filesystem.file_written"
    payload:
      path: "$workspace/notes/today.md"
error: null
```

Failed tools must still return the same envelope:

```yaml
ok: false
summary: "Write denied."
data: null
trust: "trusted"
artifacts: []
events: []
error:
  code: "permission_denied"
  message: "fs.write outside allowed scope"
  retryable: false
```

The model-facing response should prefer `summary`.
Runtime, dashboard, tests, and audit consumers should use the structured fields.


### Permission scope contract

Every sensitive tool maps to one permission class and one typed scope.

Permission classes:

```text
fs.read
fs.write
network.outbound
shell.exec
secret.read
agent.spawn
host.control
runtime.write
```

Common permission rule shape:

```yaml
class: "fs.write"
decision: "allow|ask|deny"
scope: {}
ttl: "turn|session|duration|forever"
rememberable: false
reason: "Human-readable reason."
```

Filesystem scopes:

```yaml
class: "fs.write"
decision: "ask"
scope:
  paths:
    - "$workspace/**"
  exclude:
    - "$workspace/secrets/**"
    - "$runtime/**"
```

Network scopes:

```yaml
class: "network.outbound"
decision: "ask"
scope:
  hosts:
    - "api.openai.com"
    - "localhost:8080"
  ports:
    - 443
    - 8080
```

Shell scopes:

```yaml
class: "shell.exec"
decision: "ask"
scope:
  commands:
    - "git"
    - "pytest"
    - "ruff"
  cwd:
    - "$workspace/**"
  timeout_seconds_max: 300
```

Secret scopes:

```yaml
class: "secret.read"
decision: "ask"
scope:
  credentials:
    - "openai"
    - "telegram_main"
```

Agent spawn scopes:

```yaml
class: "agent.spawn"
decision: "ask"
scope:
  agents:
    - "coding"
    - "research"
  max_parallel: 3
  max_depth: 2
```

Host control scopes:

```yaml
class: "host.control"
decision: "ask"
scope:
  actions:
    - "service.restart"
    - "logs.read"
```

Runtime write scopes:

```yaml
class: "runtime.write"
decision: "ask"
scope:
  targets:
    - "kernel"
    - "system_skill:memory"
  mode: "proposal_only|apply"
```

Permission profiles set default rules.
They do not replace scoped checks.


### Event contract

Every emitted event must have:

- id
- time
- kind
- name
- origin
- agent id
- session id
- payload
- optional correlation id

Events are append-only facts.

The event contract should also distinguish:

- fact events
- progress events
- state snapshot events
- audit/security events

So monitoring and UI do not depend on ad hoc log parsing.

Event envelope:

```yaml
id: "evt_..."
time: "2026-04-26T00:00:00Z"
kind: "fact|progress|snapshot|audit"
name: "tool.completed"
origin: "kernel|host|skill:memory|agent:main"
agent_id: "main"
session_id: "sess_..."
correlation_id: "turn_..."
payload: {}
```

V1 event names:

```text
host.started
host.stopped
kernel.started
turn.started
turn.completed
turn.failed
tool.requested
tool.approved
tool.denied
tool.started
tool.completed
tool.failed
approval.requested
approval.resolved
skill.loaded
skill.failed
skill.backend_started
agent.spawn_requested
agent.spawned
agent.checkpointed
dream.started
dream.completed
dream.action_proposed
```


### Session contract

The session layer owns:

- ordered turn history
- compaction
- metadata
- pending approval linkage
- correlation ids for turns and tool calls

It does not own memory semantics.

It should also define:

- reset behavior
- retention behavior
- compaction markers
- visibility classes for technical vs user sessions


### Approval contract

The approval layer must define:

- pending approval record shape
- permission category
- scope
- TTL
- rememberability
- replay behavior after approval

Approvals should be session-scoped by default.

Durable approvals must be explicit and limited to approved categories.

Approval outcomes should be observable through structured events, not only text messages.

Pending approval record:

```yaml
id: "approval_..."
agent_id: "main"
session_id: "sess_..."
correlation_id: "turn_..."
tool_name: "filesystem.write"
permission_class: "fs.write"
scope: {}
arguments_hash: "sha256:..."
summary: "Write file notes/today.md"
reason: "The tool requests fs.write."
created_at: "2026-04-26T00:00:00Z"
expires_at: "2026-04-26T00:30:00Z"
rememberable: false
status: "pending|approved|denied|expired"
```

Approval replay must match:

```text
permission_class + normalized_scope + tool_name + normalized_arguments_hash
```

If any field changes, the kernel must request a new approval.


## Skill Contracts

Each skill must define:

- identity
- configuration namespace
- exported tools
- prompt fragments
- dreams attachment
- dream hooks
- event/state publishers
- allowed side effects

If a skill needs storage, it owns its schema.

If a skill exposes monitorable state, it should publish a typed payload rather than require custom dashboard code.

Skill manifests use `skill.yaml`.

Minimal shape:

```yaml
name: "memory"
version: "0.1.0"
origin: "system|user"
mutable: false
description: "Durable memory storage and retrieval."
config_namespace: "skills.memory"
requires:
  binaries: []
  credentials: []
permissions:
  - class: "fs.read"
    scope:
      paths: ["$workspace/skills/memory/**"]
  - class: "fs.write"
    scope:
      paths: ["$workspace/skills/memory/**"]
tools:
  - name: "memory.remember"
    input_schema: {}
    permission_class: "fs.write"
backend: null
storage:
  engine: "sqlite"
  path: "$workspace/skills/memory/memory.sqlite"
  schema_version: 1
  migrations:
    - "migrations/001_init.sql"
dreams:
  attachment: "dreams.md"
  input_builder: "dreams.build_inputs"
events:
  state_publisher: "state.publish"
```

Loader states:

```text
loaded
disabled
disabled_with_error
missing_dependency
migration_required
```

Loader phases:

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

Skill name collisions are errors in v1.
A user skill must not replace a system skill silently.


## Dream Contracts

Dream contributions from skills must be explicit.

Each skill may attach a `dreams.md` file that explains what the dreaming pipeline may use from that skill.

The attachment should define:

- available data
- useful signals
- freshness and trust assumptions
- candidate action types
- validation rules
- known limits

A skill may provide:

- dream input builder
- dream prompt fragment
- action proposal validator
- action materializer

The kernel never guesses dream meaning on behalf of a skill.

Dream input envelope:

```yaml
skill: "memory"
trust: "local_mutable"
freshness:
  generated_at: "2026-04-26T00:00:00Z"
  expires_at: null
signals:
  - id: "sig_..."
    type: "stale_topic|open_loop|nearby_date|candidate_cleanup"
    summary: "Project X has not been revisited recently."
    data: {}
limits:
  - "Only includes non-archived memories."
```

Dream report envelope:

```yaml
id: "dream_..."
run_at: "2026-04-26T00:00:00Z"
summary: "Short synthesis."
signals:
  - source_skill: "memory"
    signal_id: "sig_..."
    summary: "..."
proposed_actions:
  - id: "act_..."
    owner_skill: "memory"
    action_type: "consolidate"
    payload: {}
    risk: "low|medium|high"
    requires_approval: true
uncertainties:
  - "..."
events:
  - name: "dream.action_proposed"
    payload: {}
```

Dream actions are never executed directly by the model.
The owner skill validates the action, then the approval gate applies policy.


## Agent Contracts

Permanent agents and subagent runs must have explicit rules for:

- context inheritance
- skill inheritance
- permission inheritance
- write scope
- communication format
- termination semantics
- event visibility
- session isolation
- result ingestion format

No invisible privilege escalation is allowed during delegation.

Permanent agents are durable peers with explicit identity, permissions, skills, sessions, and event streams.

Subagent runs are temporary task executions.
They should support controlled interruption through checkpointing rather than abrupt timeout during normal operation.

Permanent agent config:

```yaml
id: "coding"
default: false
workspace: "$workspace/agents/coding"
skills:
  - "filesystem"
  - "git"
  - "tests"
permission_profile: "limited"
model:
  provider: "ollama"
  name: "minimax-m2.7"
channels: []
event_stream: "$workspace/agents/coding/events.jsonl"
```

Subagent run request:

```yaml
id: "run_..."
parent_agent: "main"
base_agent: "coding"
task: "Review the storage module."
workspace: "$workspace/agents/main/runs/run_..."
context:
  inheritance: "task_only|selected_session|none"
  artifacts: []
permissions:
  fs.read:
    paths: ["$workspace/project/**"]
  fs.write:
    paths: ["$workspace/project/docs/**"]
budget:
  max_tool_calls: 60
  soft_deadline_seconds: 1200
  checkpoint_required: true
```

Subagent checkpoint:

```yaml
run_id: "run_..."
status: "running|paused|completed|failed|cancelled"
summary: "Current state of the work."
completed: []
changed_files: []
artifacts: []
remaining: []
risks: []
resume_hint: "Where to continue."
safe_to_resume: true
errors: []
```

Subagent final result:

```yaml
run_id: "run_..."
status: "completed|failed|cancelled"
summary: "..."
artifacts: []
changes: []
requested_followups: []
errors: []
```


## Failure Philosophy

Contracts should prefer:

- typed failure
- partial degradation
- observable error events

Over:

- hidden fallback
- silent coercion
- prompt-only recovery
