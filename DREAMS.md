# Maurice Dreams

## Principle

Dreams are not a monolithic feature.

They are a generic background review pipeline over skill-provided signals.


## Kernel Role

The kernel only owns:

- scheduling dream runs
- triggering the dreaming system skill
- calling the model
- storing reports
- enforcing action approvals

The kernel does not understand what memory, todos, calendars, or projects mean.
It only provides the generic background runner, provider call path, event store, and approval gate.


## Skill Role

Each skill may contribute:

- dream context
- prioritized signals
- candidate actions
- action validators
- action executors

Each skill describes those contributions in its own `dreams.md` file.

That file tells the dreaming pipeline:

- which data the skill can expose
- what the data means
- which signals are worth reviewing
- which actions may be proposed
- which limits, risks, or freshness rules apply

The dreaming pipeline should not infer hidden meaning from a skill's storage.
It should consume dream data through the skill contract and the attached `dreams.md` guidance.


## Dreaming System Skill

`dreaming` is a system skill.

It owns the meaning of a dream run:

- enumerate active skills
- load each `dreams.md`
- request dream inputs from skills
- assemble dream context
- ask the provider for a structured review
- route proposed actions back to owner skills for validation
- submit approved actions through the approval gate
- publish dream events and reports

The kernel schedules `dreaming.run`.
The dreaming skill defines what `dreaming.run` does.


## Dream Run Pipeline

A dream run should follow this order:

1. kernel background runner triggers `dreaming.run`
2. dreaming enumerates active skills
3. dreaming loads each active skill's `dreams.md`
4. each skill may provide `dream_inputs`
5. dreaming assembles context from inputs, freshness, trust, and limits
6. provider generates a structured dream report
7. each proposed action is sent to its owner skill for validation
8. approval gate applies policy
9. accepted actions are materialized by the owner skill
10. report and events are persisted


## Dream Inputs

A skill's dream input should be structured.

Example:

```yaml
skill: "memory"
trust: "local_mutable"
freshness:
  generated_at: "2026-04-26T00:00:00Z"
  expires_at: null
signals:
  - id: "sig_..."
    type: "stale_topic"
    summary: "Project X has not been revisited recently."
    data: {}
limits:
  - "Only includes non-archived memories."
```

Signals should be facts or carefully scoped interpretations.
Uncertainty should be explicit.


## Dream Report

A dream run should produce one structured report.

Example:

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
    risk: "low"
    requires_approval: true
uncertainties:
  - "..."
events:
  - name: "dream.action_proposed"
    payload: {}
```

The report is not itself an execution plan.
Actions remain proposals until validated and approved.


## Dream Output

A dream run should produce structured sections:

- summary
- follow-up signals
- local action proposals
- uncertainties

These are generic containers.
Their meaning comes from the contributing skills.


## Memory Relationship

Memory and dreams are related, but distinct.

- memory stores durable information
- dreams review, consolidate, and react to skill signals

If memory exists as a skill, dreams should consume it through that skill’s contract.

Memory may be a privileged system skill in distribution, but it remains outside the kernel.

The dreaming skill may depend on memory when memory is enabled.
It must still degrade gracefully when memory is disabled or unavailable.


## Action Rules

Dream actions must be:

- local
- reversible when possible
- low-risk by default
- owned by a skill
- validated by the owner skill
- passed through approval policy

Dreams do not directly perform arbitrary external actions.

External actions are not v1 dream actions.
If a skill wants to propose an external action later, it must declare the action type, permission class, validation rules, and approval behavior explicitly.


## Events

Dreaming should emit structured events:

- `dream.started`
- `dream.completed`
- `dream.action_proposed`
- `dream.action_validated`
- `dream.action_denied`
- `dream.failed`

Events should include the dream id, source skill, owner skill when relevant, and correlation id.
