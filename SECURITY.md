# Maurice Security

## Principle

Security in Maurice should be simple enough to understand in one pass.

The kernel owns policy enforcement.
Skills may request actions, but they never define the final security policy.


## Permission Classes

Every sensitive tool maps to one permission class:

- `fs.read`
- `fs.write`
- `network.outbound`
- `shell.exec`
- `secret.read`
- `agent.spawn`
- `host.control`
- `runtime.write`

This list should stay short.


## Decisions

Each permission class supports only:

- `allow`
- `ask`
- `deny`

There should not be several overlapping policy systems.


## Permission Profiles

The user chooses a permission profile during onboarding.

The default profile should keep agents scoped to the workspace.
More permissive profiles may allow broader access, but the runtime root remains protected unless the user explicitly approves runtime-level changes.

Profiles:

- `safe`: agents stay inside the workspace; sensitive writes, shell, network, secrets, agent spawning, and host control are denied or require explicit approval.
- `limited`: agents still default to workspace scope, but may request approved access to selected external paths, network hosts, commands, or host operations.
- `power`: agents may request broad system access with fewer prompts, but runtime and system skill modification should still require explicit user approval.

Profiles set defaults.
They do not bypass tool declarations, scoped permissions, trust labels, or approval records.


## Profile Matrix

Profile rules are defaults applied before per-agent overrides and tool-specific scopes.

`rememberable` means the user may choose to remember an approval for the declared scope.
Durable approvals must stay explicit.

### `safe`

```yaml
fs.read:
  decision: allow
  scope:
    paths: ["$workspace/**"]
    exclude: ["$workspace/secrets/**"]
  rememberable: false

fs.write:
  decision: ask
  scope:
    paths: ["$workspace/**"]
    exclude: ["$workspace/secrets/**"]
  rememberable: true

network.outbound:
  decision: ask
  scope:
    hosts: []
  rememberable: true

shell.exec:
  decision: deny
  scope:
    commands: []
  rememberable: false

secret.read:
  decision: ask
  scope:
    credentials: []
  rememberable: false

agent.spawn:
  decision: deny
  scope:
    agents: []
    max_parallel: 0
  rememberable: false

host.control:
  decision: deny
  scope:
    actions: []
  rememberable: false

runtime.write:
  decision: deny
  scope:
    mode: "proposal_only"
  rememberable: false
```

### `limited`

```yaml
fs.read:
  decision: allow
  scope:
    paths: ["$workspace/**"]
    exclude: ["$workspace/secrets/**", "$runtime/**"]
  rememberable: false

fs.write:
  decision: allow
  scope:
    paths: ["$workspace/**"]
    exclude: ["$workspace/secrets/**", "$runtime/**"]
  rememberable: false

network.outbound:
  decision: ask
  scope:
    hosts: []
  rememberable: true

shell.exec:
  decision: ask
  scope:
    commands: ["git", "pytest", "ruff"]
    cwd: ["$workspace/**"]
    timeout_seconds_max: 300
  rememberable: true

secret.read:
  decision: ask
  scope:
    credentials: []
  rememberable: false

agent.spawn:
  decision: ask
  scope:
    agents: []
    max_parallel: 3
    max_depth: 2
  rememberable: true

host.control:
  decision: ask
  scope:
    actions: ["logs.read", "service.status"]
  rememberable: true

runtime.write:
  decision: ask
  scope:
    targets: ["kernel", "system_skill:*"]
    mode: "proposal_only"
  rememberable: false
```

### `power`

```yaml
fs.read:
  decision: allow
  scope:
    paths: ["$workspace/**", "$home/**"]
    exclude: ["$runtime/**", "$home/.ssh/**", "$home/.gnupg/**"]
  rememberable: false

fs.write:
  decision: allow
  scope:
    paths: ["$workspace/**"]
    exclude: ["$runtime/**", "$workspace/secrets/**"]
  rememberable: false

network.outbound:
  decision: allow
  scope:
    hosts: ["*"]
  rememberable: false

shell.exec:
  decision: ask
  scope:
    commands: ["*"]
    cwd: ["$workspace/**"]
    timeout_seconds_max: 900
  rememberable: true

secret.read:
  decision: ask
  scope:
    credentials: []
  rememberable: false

agent.spawn:
  decision: allow
  scope:
    agents: ["*"]
    max_parallel: 6
    max_depth: 3
  rememberable: false

host.control:
  decision: ask
  scope:
    actions: ["logs.read", "service.status", "service.restart"]
  rememberable: true

runtime.write:
  decision: ask
  scope:
    targets: ["kernel", "host", "system_skill:*"]
    mode: "proposal_only"
  rememberable: false
```

Even in `power`, direct runtime writes are not normal agent writes.
They produce proposals by default.


## Profile Overrides

An agent may have a profile different from the global default.

An agent profile may be less permissive freely.
Making an agent more permissive than the onboarding default requires explicit user confirmation during configuration.


## Trust Labels

Maurice should tag important data with trust metadata:

- `trusted`
- `local_mutable`
- `external_untrusted`
- `skill_generated`

Trust labels travel with tool outputs and event payloads.


## Skill Boundaries

A skill may declare:

- the permission classes its tools require
- whether it needs a backend
- whether it needs secrets

But the kernel validates execution every time.


## Filesystem Boundary

Maurice runtime code and agent workspaces should live in separate roots.
The workspace root is selected by the user during onboarding.

The runtime root contains:

- kernel code
- host code
- shipped system skills

The workspace root contains:

- agent sub-workspaces
- sessions
- artifacts
- user-created skills

By default, normal agent filesystem permissions should target the workspace root only.
The selected permission profile may allow broader access through scoped approvals.

The runtime root should be treated as trusted and read-only during normal agent execution.
Agents may read selected runtime docs or manifests when policy allows, but they should not write to kernel code, host code, or shipped system skills.

If an agent proposes a change to the runtime or a system skill, it should produce a patch or proposal that requires explicit user approval outside normal workspace-scoped writes.

Runtime self-update proposals are specified in `SELF_UPDATE.md`.


## Delegation Safety

Subagents should inherit a restricted view by default:

- same or narrower permissions
- explicit write scope
- explicit task scope
- no implicit secret access

Delegation must never widen power.


## Secrets

Secrets remain outside main config.

Credentials should stay:

- separate
- typed
- minimal

Kernel config may reference them, but should not duplicate them.
