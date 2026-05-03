# Long Missions

Maurice has two different autonomy shapes:

- **temporary dev workers**: short, bounded helper executions spawned by a parent
  agent for a narrow task;
- **long missions**: future durable autonomous work sessions, started
  intentionally when the product needs work that should survive one turn.

Long missions are not the old persistent runs system. Do not reintroduce a
generic independent run surface unless the user-facing mission need is clear.

## Temporary Workers

Workers are implementation helpers, not independent assistants.

- They are spawned by a parent agent, usually from `/dev`.
- They receive narrow standalone context: objective, relevant files, constraints,
  expected output, and allowed write paths.
- They are bounded by count, depth, tool iterations, and time budget.
- They do not own the plan, do not revise product direction, do not ask the user
  directly, and do not spawn other workers.
- They stop with a structured status: `completed`, `blocked`,
  `needs_arbitration`, or `obsolete`.
- The parent agent integrates their output, updates the plan, and decides
  whether to spawn fresh workers.

Worker records may exist for observability under the agent workspace, but they
are not durable product sessions.

## Long Missions

A long mission is a first-class autonomous session owned by an agent.

Use a mission only when a task needs persistence beyond a normal turn, for
example:

- continue a dev plan over a long unattended window;
- monitor a project, service, or feed until a condition changes;
- prepare a report that requires multiple scheduled passes;
- coordinate several bounded workers over time.

A mission must have:

- an owner agent;
- an active project or explicit workspace scope;
- a written objective and definition of done;
- a departure branch when it will modify a Git project;
- explicit budgets for wall time, tool calls, worker count, and network usage;
- approval gates for merge, push, secrets, risky shell, external writes, and
  destructive filesystem actions;
- observable state for dashboard and CLI;
- a cancellation path.

## Lifecycle

Recommended states:

```text
proposed -> approved -> running -> waiting
                         |        |
                         v        v
                      blocked   completed
                         |
                         v
                      cancelled
```

- `proposed`: the agent describes the mission, risks, scope, budget, and gates.
- `approved`: the user authorizes the mission to run unattended within its
  budget.
- `running`: Maurice executes mission turns and may spawn temporary workers.
- `waiting`: the mission is intentionally paused until a schedule, event, or
  external condition.
- `blocked`: the mission cannot proceed without user input or a new approval.
- `completed`: the definition of done is reached.
- `cancelled`: the user or host stops the mission.

Missions should heartbeat regularly and write compact progress records. If a
mission exceeds budget, loses context, hits repeated model failures, or reaches a
permission gate, it must stop as `blocked` instead of looping.

## Storage Shape

Prefer agent-scoped storage:

```text
<workspace>/agents/<agent-id>/missions/<mission-id>/
  mission.yaml       # objective, scope, budgets, status, branch metadata
  events.jsonl       # progress and tool summaries
  checkpoints.md     # human-readable compact state
```

Project-specific durable knowledge still belongs in the project:

```text
<project>/.maurice/PLAN.md
<project>/.maurice/DECISIONS.md
<project>/.maurice/dreams.md
```

Do not duplicate project memory into mission storage except as compact
checkpoints needed to resume safely.

## Commands

Future user-facing commands should be explicit:

```text
/mission propose <goal>
/mission start <mission-id>
/mission status [mission-id]
/mission pause <mission-id>
/mission cancel <mission-id>
/mission resume <mission-id>
```

Avoid overloading `/dev` with invisible background continuation. `/dev` can
propose a mission when the plan is too long for one supervised turn, but the
user should approve a mission explicitly.

## Permissions

Mission permissions are the owner agent permissions plus mission budgets and
gates. A mission must not broaden the agent's profile.

- `safe`: missions can be proposed and read state, but unattended writes should
  remain approval-gated.
- `limited`: missions may read/write inside `$workspace` and `$project`, and use
  scoped shell when allowed by policy.
- `power`: missions may use broader allowed capabilities, but destructive,
  secret, push, merge, and external-write gates still require explicit approval
  unless the user approved that exact mission gate.

Secrets should never be requested or captured while unattended. Network writes,
publishing, pushing Git branches, deleting files, and modifying host/runtime
configuration require clear approval gates.

## Dashboard

The dashboard should show missions separately from temporary workers:

- active missions with owner agent, project, state, objective, last heartbeat,
  budget used, and next gate;
- recent temporary workers grouped under the parent mission or parent turn;
- blocked missions at the top because they need human attention.

This keeps observability benefits without pretending workers are independent
long-running assistants.

