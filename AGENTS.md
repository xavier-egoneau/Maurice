# Maurice Agents

## Base Model

Maurice supports durable agents and temporary task runs.

A durable agent is a configured peer:

- stable identity
- own enabled skills
- own permissions
- own sessions
- own event stream
- optional channel bindings

`main` is the default durable agent, not the only possible durable agent.

A subagent is not a permanent agent.
It is a temporary task run created from a durable agent or an agent profile.


## Operational Commands

Agents should prefer the user-facing `maurice` command when it is installed.
Use `python3 -m maurice.host.cli ...` only when the console script is unavailable.

Common operations:

```bash
maurice onboard
maurice doctor
maurice run --message "hello"
maurice start
maurice stop
maurice logs
maurice dashboard
```

Agent operations:

```bash
maurice onboard --agent <id>
maurice onboard --agent <id> --model
maurice run --agent <id> --message "hello"
maurice agents list
maurice agents update <id> --permission-profile safe
```

Dashboard action shortcuts should map back to these host operations. For example,
changing an agent model from the dashboard should direct the user to
`maurice onboard --agent <id> --model` rather than editing YAML directly.

When operating outside the default workspace, add:

```bash
--workspace /path/to/workspace
```

Do not edit `config/*.yaml` directly for normal user workflows when a host
command exists. Direct config edits are acceptable for tests, migrations, and
low-level repair work.


## Permanent Agents

Permanent agents may exist at the same level as `main`.

They are useful for durable roles such as:

- coding
- operations
- research
- personal assistant

They should not inherit each other's sessions, secrets, approvals, or permissions implicitly.

Each permanent agent should be configured explicitly.


## Agent Config

A permanent agent should declare:

- id
- workspace
- enabled skills
- permission profile
- optional model override
- optional channel bindings
- event stream

Example:

```yaml
agents:
  main:
    default: true
    workspace: "$workspace/agents/main"
    skills: ["filesystem", "memory", "dreaming", "web"]
    permission_profile: "safe"
    channels: ["telegram"]
    event_stream: "$workspace/agents/main/events.jsonl"

  coding:
    workspace: "$workspace/agents/coding"
    skills: ["filesystem", "git", "tests"]
    permission_profile: "limited"
    channels: []
    event_stream: "$workspace/agents/coding/events.jsonl"
```

An agent may be less permissive than the global onboarding profile freely.
Making an agent more permissive than the global profile requires explicit user confirmation.


## Channels

Channels bind to permanent agents, not to subagent runs.

An inbound channel message must resolve to:

- agent id
- session id
- peer id
- correlation id

Subagent runs do not receive direct channel traffic unless explicitly promoted into a permanent agent.


## Spawn Rules

A subagent run must be created with:

- explicit task
- explicit write scope
- explicit permission scope
- explicit context inheritance policy
- explicit parent or requesting agent
- explicit base agent profile when relevant

Subagent runs may be based on:

- a permanent agent
- a declared agent profile
- a temporary inline profile, if policy allows it

The resulting permissions must be equal to or narrower than the requesting agent unless the user explicitly approves elevation.


## Delegation Rules

Delegation between permanent agents is different from spawning a subagent run.

Permanent-agent delegation:

- targets an existing durable agent
- uses that agent's own workspace, skills, permissions, sessions, and event stream
- should not merge the parent session into the target agent automatically
- should return a structured result to the requester

Subagent-run delegation:

- creates a temporary run
- uses explicit task scope
- uses a fresh run session by default
- must produce checkpoint/final result envelopes

The parent or requester owns user-facing synthesis.
The delegated agent or run owns only the assigned result.


## Communication

Agent-to-agent communication should be structured.

Minimum shape:

- task
- inputs
- constraints
- output summary
- status

Free-form chat between agents should not be the primary protocol.


## Inheritance

By default, a subagent run inherits:

- current task context
- enabled skills subset
- same or narrower permissions
- a fresh session unless explicitly linked

It should not automatically inherit:

- all secrets
- all workspace write access
- all historical memory
- parent pending approvals


## Ownership

The spawning or requesting agent owns:

- task framing
- result integration
- user-facing synthesis

Subagent runs should stay disposable.


## Run Control

A subagent run should not be killed abruptly during normal operation.

Instead, it should support:

- soft deadline
- checkpoint request
- cancellation request
- clean pause
- resumable summary

On soft deadline or cancellation, the run should produce a checkpoint before stopping when possible.

The checkpoint should include:

- current status
- work completed
- artifacts or changed files
- remaining steps
- known risks
- whether it is safe to resume


## Run Lifecycle

A subagent run should move through explicit states:

- `created`
- `running`
- `checkpointing`
- `paused`
- `completed`
- `failed`
- `cancelled`

Normal flow:

1. validate request
2. create run workspace
3. create run session and event stream
4. resolve base agent/profile
5. narrow skills and permissions
6. execute task
7. emit checkpoint or final result
8. return result to requester

Cancellation flow:

1. requester or user asks for cancellation
2. kernel sends cancellation request to the run
3. run attempts checkpoint
4. run becomes `paused` or `cancelled`
5. parent receives the checkpoint

Hard termination should be reserved for crash, stuck execution, or explicit user intervention.


## Resume

A paused subagent run may be resumed only if its checkpoint says `safe_to_resume: true`.

Resume must preserve:

- run id
- workspace
- event stream
- current artifacts
- scoped permissions

Resume may refresh model context from the checkpoint summary rather than replaying the full run history.


## Communication Contract

Subagent run outputs should be machine-readable first, narrative second.

The minimal return shape should include:

- `status`
- `summary`
- `artifacts`
- `requested_followups`
- `errors`

This keeps orchestration generic and prevents every subagent run type from inventing its own protocol.

Permanent-agent delegation should also return a structured result.

Minimum shape:

```yaml
status: "completed|failed|needs_input"
summary: "..."
artifacts: []
requested_followups: []
errors: []
```


## Session And Approval Isolation

Subagent runs should not share the parent's live approval queue.

Each subagent run should have:

- its own session identity
- its own event stream
- its own approval scope

The parent may surface or reframe a subagent approval request, but that bridge should be explicit.


## Approval Bridging

A subagent run has its own approval scope.

If it needs an approval that cannot be resolved inside its own scope, it may emit an approval request to the parent.

The parent may:

- deny it
- ask the user
- narrow the scope and approve
- cancel or pause the run

Approving a parent-level request must not silently grant broader future permissions to the child run.


## Non-Goals

Maurice should avoid treating permanent agents as a rigid hierarchy.

Permanent agents are peers with explicit configuration.
Subagent runs are temporary executions with explicit scope and clean checkpoint behavior.
