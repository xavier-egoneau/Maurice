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
  development:
    web_agent_switching: false
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

By default, the web chat is bound to the surface agent: one web conversation
surface maps to one user/agent, and the browser does not expose an agent
switcher. Set `host.development.web_agent_switching: true` only for development
workflows that need to test several agents from the same browser UI.

### kernel.yaml

```yaml
kernel:
  models:
    default: anthropic_claude_opus_4_5
    entries:
      anthropic_claude_opus_4_5:
        provider: api
        protocol: anthropic
        name: claude-opus-4-5
        base_url: null
        credential: anthropic
        tier: high          # optional: high | middle | low, editable by the user
        capabilities: [text, tools]
        privacy: cloud      # local | cloud | unknown
      ollama_gemma4:
        provider: ollama
        protocol: ollama_chat
        name: gemma4
        base_url: http://localhost:11434
        credential: null
        tier: middle
        capabilities: [text, tools, vision]
        privacy: local

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

  subagents:
    templates:
      coder:
        id: coder
        description: Coding worker
        permission_profile: safe
        skills: [filesystem, dev]
        credentials: [ollama]
        model_chain:
          - ollama_gemma4

  events:
    retention_days: 30

  sessions:
    retention_days: 30
    compaction: true
    context_window_tokens: 250000
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
    model_chain:            # ordered model profile ids; first usable profile wins
      - anthropic_claude_opus_4_5
      - ollama_gemma4
    event_stream: /path/to/workspace/agents/main/events.jsonl
```

Model profiles live in `kernel.models.entries`, not inside each agent. An agent
chooses an ordered `model_chain`; Maurice uses the first available profile and
falls back to the next one when a profile is immediately unusable, for example
because its credential is not allowed or an auth token is missing. Older
`kernel.model` and `agent.model` blocks are migrated into this structure and
removed from the YAML; credentials themselves are not moved or rewritten.

The CLI exposes the same structure:

```bash
maurice models list --workspace /path/to/workspace
maurice models add --workspace /path/to/workspace --provider ollama --protocol ollama_chat --name gemma4 --base-url http://localhost:11434 --tier middle --capability text --capability vision
maurice models assign coding ollama_gemma4 api_gpt_4o_mini --workspace /path/to/workspace
maurice models default ollama_gemma4 --workspace /path/to/workspace
```

Reusable subagent templates also reference central model profile ids through
`model_chain`; they do not embed provider credentials or duplicate model config.
Agents can create disposable runs from these templates:

```bash
maurice runs template-add coder --workspace /path/to/workspace --skill filesystem --model ollama_gemma4
maurice runs create --workspace /path/to/workspace --task "Run tests" --template coder
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

OpenAI-compatible and ChatGPT setups default to a 250k token context budget for
the local context meter and automatic compaction. You can lower or raise it with
`kernel.sessions.context_window_tokens`.

## Credentials Store

Credentials live in:

```text
~/.maurice/credentials.yaml
```

Each credential has `name`, `value`, and optional `base_url`. Agents access
credentials listed in `agents.<id>.credentials`, or `["*"]` for all.
