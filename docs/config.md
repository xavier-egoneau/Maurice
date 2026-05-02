# Config Reference

Maurice has folder-level config and desktop-assistant workspace config. They are
two context anchors for the same runtime, not two separate systems.

## Folder Context Config

Folder usage reads global defaults from `~/.maurice/config.yaml`, then overlays
the current folder config:

```text
<project>/.maurice/config.yaml
```

Example:

```yaml
provider:
  type: mock
permission_profile: limited
skills:
  - filesystem
  - memory
skill_roots:
  - path: ./skills
    origin: user
    mutable: true
```

Folder-scoped state lives in `<project>/.maurice/`: sessions, events, approvals,
memory, and server metadata.

`~/.maurice/config.yaml` also records the setup-level context preference:

```yaml
usage:
  mode: local              # local | global
  workspace: /path/to/ws   # present when mode is global
```

When `mode` is `local`, Maurice starts from folders by default. Daemon commands
such as `maurice start` require an explicit `--workspace` or a setup switch to
`global`; they do not silently expand a folder-focused setup into a desktop
assistant. Browser chat follows the same rule for context level: `maurice web`
resolves a folder context when `mode` is `local`, and resolves the configured
workspace context when `mode` is `global`. In global mode, the current working
directory is still recorded as the active project for relative file work, even
when it is outside the workspace. `--dir` and `--workspace` are explicit
overrides for the context root choice.

## Desktop Assistant Workspace Config

Desktop assistant usage stores host-owned config under `~/.maurice` and keeps
mutable workspace-owned skill config in the workspace:

```text
~/.maurice/workspaces/<workspace-key>/config/host.yaml
~/.maurice/workspaces/<workspace-key>/config/kernel.yaml
~/.maurice/workspaces/<workspace-key>/config/agents.yaml
<workspace>/skills.yaml
```

### host.yaml

```yaml
host:
  runtime_root: /path/to/maurice
  workspace_root: /path/to/workspace
  gateway:
    host: 127.0.0.1
    port: 18791
  skill_roots:
    - path: /path/to/maurice/maurice/system_skills
      origin: system
      mutable: false
    - path: /path/to/workspace/skills
      origin: user
      mutable: true
  channels:
    local_http:
      adapter: local_http
      enabled: true
      agent: main
      credential: null
```

`workspace_root` is the desktop assistant state root used by daemon services,
sessions, durable agents, memory, and assistant-owned content. It is not
necessarily the folder currently being edited: global web and gateway turns can
carry an `active_project_root` resolved from the launch folder. Host runtime
wiring resolves both values into `MauriceContext`.

### kernel.yaml

```yaml
kernel:
  model:
    provider: api           # mock | api | auth | openai | ollama
    protocol: anthropic     # depends on provider
    name: claude-opus-4-5
    base_url: null
    credential: anthropic   # key in credentials store

  permissions:
    profile: limited        # safe | limited | power

  approvals:
    mode: ask               # ask | auto_deny | auto
    ttl_seconds: 1800
    remember_ttl_seconds: 600
    classifier_model: ""
    classifier_cache_ttl_seconds: 3600

  skills: []                # list of skill names to enable (empty = all available)

  scheduler:
    enabled: true
    dreaming_enabled: true
    dreaming_time: "09:00"
    daily_enabled: true
    daily_time: "09:30"

  events:
    retention_days: 30

  sessions:
    retention_days: 30
    compaction: true
    context_window_tokens: 100000
    trim_threshold: 0.60
    summarize_threshold: 0.75
    reset_threshold: 0.90
    keep_recent_turns: 10
```

### agents.yaml

```yaml
agents:
  main:
    id: main
    name: Maurice
    status: active          # active | disabled | archived
    default: true
    workspace: /path/to/workspace/agents/main
    permission_profile: limited
    credentials: ["*"]
    skills: []              # override kernel.skills for this agent
    model:                  # override kernel.model for this agent (optional)
      provider: api
      protocol: anthropic
      name: claude-opus-4-5
      credential: anthropic
    event_stream: /path/to/workspace/agents/main/events.jsonl
```

### skills.yaml

Per-skill config, keyed by `config_namespace` from each skill's `skill.yaml`:

```yaml
skills:
  filesystem: {}
  memory:
    backend: sqlite
  web:
    max_results: 10
  dev: {}
```

## Providers

| `provider` | `protocol` | Notes |
|---|---|---|
| `mock` | — | In-memory mock; no credential needed |
| `api` | `anthropic` | Anthropic API via `credential` |
| `api` | `openai_chat_completions` | OpenAI-compatible via `credential` |
| `openai` | — | OpenAI SDK; `credential: openai` |
| `ollama` | — | Local Ollama; `base_url: http://localhost:11434` |
| `auth` | `chatgpt_codex` | ChatGPT Codex stored auth token |

## Credentials Store

Credentials live in:

```text
~/.maurice/credentials.yaml
```

Each credential has `name`, `value`, and optional `base_url`. Agents access
credentials listed in `agents.<id>.credentials`, or `["*"]` for all.
