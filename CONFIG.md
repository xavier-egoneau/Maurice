# Maurice Config

## Principle

Configuration should be split by ownership.

Do not put every setting in one global schema.


## Three Config Domains

### Kernel config

Owns:

- default model/provider
- session rules
- permission profile and approval mode
- enabled skills
- scheduler toggles
- session retention/compaction
- event retention
- monitoring/state exposure toggles


### Skill config

Owns:

- skill-specific runtime settings
- storage options
- backend endpoints
- feature flags


### Host config

Owns:

- service bindings
- install-time infra
- channel transport settings
- local paths
- runtime and workspace roots
- onboarding defaults
- channel command policy
- dashboard endpoint wiring


## Credentials

Credentials stay separate from config.

Config may reference a credential name.
Config should not duplicate secret values.

Credential records should be typed.

Example:

```yaml
credentials:
  openai:
    type: api_key
    value: "..."
  telegram_main:
    type: token
    value: "..."
  searxng:
    type: url
    base_url: "http://localhost:8080"
    value: ""
```


## Workspace Selection

The user chooses the workspace root during onboarding.

That path becomes the mutable root for:

- agent sub-workspaces
- user-created skills
- sessions
- artifacts
- local skill storage when appropriate

The chosen workspace root is persisted in host config.

Changing it later is a host-level reconfiguration, not a normal agent action.


## Onboarding Outputs

Onboarding should write four areas.

Host config:

- runtime root
- workspace root
- gateway binding
- channel transport settings
- skill roots
- onboarding defaults

Kernel config:

- default model
- enabled skills
- permission profile
- approval defaults
- session rules
- event retention
- scheduler toggles

Credentials:

- provider keys
- channel tokens
- backend secrets

Workspace bootstrap:

- `agents/`
- `skills/`
- `sessions/`
- `artifacts/`
- default agent directories

Onboarding may create directories and default files.
It should not put secrets in normal config.


## Suggested Files

Maurice may keep config split physically or logically.

Suggested shape:

```text
maurice-runtime/
  config/defaults.yaml

maurice-workspace/
  config/
    host.yaml
    kernel.yaml
    agents.yaml
    skills.yaml
  credentials.yaml
  agents/
  skills/
  sessions/
  artifacts/
```

The exact file layout may change, but ownership boundaries should not.


## Example Shape

```yaml
kernel:
  model:
    provider: ollama
    protocol: ollama_chat
    name: minimax-m2.7
    base_url: http://localhost:11434
  permissions:
    profile: safe
  approvals:
    mode: ask
    ttl_seconds: 1800
    remember_ttl_seconds: 600
  skills:
    - memory
    - dreaming
    - web
  scheduler:
    enabled: true
  events:
    retention_days: 30
  sessions:
    retention_days: 30
    compaction: true

host:
  runtime_root: /opt/maurice
  workspace_root: ~/.maurice/workspace
  gateway:
    host: 127.0.0.1
    port: 18791
  skill_roots:
    - path: /opt/maurice/system_skills
      origin: system
      mutable: false
    - path: ~/.maurice/workspace/skills
      origin: user
      mutable: true
  channels:
    telegram:
      enabled: true
      bot_token_credential: telegram_main

agents:
  main:
    default: true
    workspace: "$workspace/agents/main"
    skills: ["filesystem", "memory", "dreaming", "web"]
    permission_profile: safe
    channels: ["telegram"]
  coding:
    workspace: "$workspace/agents/coding"
    skills: ["filesystem", "git", "tests"]
    permission_profile: limited
    channels: []

skills:
  web:
    provider: searxng
    base_url: http://localhost:8080
    credential: searxng
  memory:
    backend: sqlite
    path: "$workspace/skills/memory/memory.sqlite"
```


## Config Resolution

Runtime resolution should follow a predictable order:

1. load packaged defaults from runtime
2. load host config
3. load kernel config
4. load agent config
5. load skill config
6. resolve credential references
7. apply explicit CLI flags
8. validate final config

CLI flags may override runtime values for a process, but should not persist unless the command is explicitly a configuration command.


## Reconfiguration

Changing host-level values is a host reconfiguration.

Host-level values include:

- runtime root
- workspace root
- gateway binding
- channel transport binding
- system skill roots

Changing these should be explicit and user-initiated.

Changing kernel, agent, or skill config may be allowed through agent tools only when policy allows it and the write target is inside the workspace.

Runtime root changes are never normal agent writes.


## Rules

- kernel config stays small
- permission profile is chosen during onboarding and persisted in kernel config
- skill config lives under `skills.<name>`
- host config stays operational, not behavioral
- secrets never live inline in normal config
- runtime root and workspace root must stay physically separate
- normal agent write permissions target the workspace, not the runtime root
- system skill roots are read-only during normal agent execution
- user skill roots may be writable when policy allows skill authoring
- session config must stay separate from memory skill config
- approval policy must be centrally declared, not copied into skills
- dashboard/monitoring config should point to generic state sources, not per-feature hard-coded flags
