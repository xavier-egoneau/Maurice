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

Folder-scoped project/server state lives in `<project>/.maurice/`: sessions,
events, approvals, project notes, and server metadata. Durable `memory` skill
storage stays agent-scoped under `$agent_workspace/memory/memory.sqlite`.

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

The scheduler creates agent-scoped recurring jobs under
`<workspace>/agents/<agent-id>/jobs.json`. `dreaming.run` is scheduled when
`dreaming_enabled` is true and the agent has the `dreaming` skill enabled;
`daily.digest` is scheduled when `daily_enabled` is true and the agent has the
`daily` skill enabled. `maurice start` runs the scheduler by default in
persistent assistant mode. Use the Maurice web **Agent > Automatismes** section
or `maurice scheduler configure --workspace ...` to change times or
enable/disable either job. See [automations](automations.md).

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
    worker_model_chain:     # optional dev-worker model ids; empty = inherit model_chain
      - ollama_gemma4
    event_stream: /path/to/workspace/agents/main/events.jsonl
```

Model profiles live in `kernel.models.entries`, not inside each agent. An agent
chooses an ordered `model_chain`; Maurice uses the first available profile and
falls back to the next one when a profile is immediately unusable, for example
because its credential is not allowed or an auth token is missing. Older
`kernel.model` and `agent.model` blocks are migrated into this structure and
removed from the YAML; credentials themselves are not moved or rewritten.

Development workers use `worker_model_chain` when it is configured on the
parent agent. If it is empty, `/dev` workers inherit the parent agent
`model_chain`. Users choose the worker provider/model from the agent config or
with the CLI. Maurice may launch workers for parallelizable dev tasks, but the
orchestration is bounded: at most 5 workers in one call, at most 10 active
workers, and each worker receives a narrow standalone context plus a tool/time
budget.

The CLI exposes the same structure:

```bash
maurice models list --workspace /path/to/workspace
maurice models add --workspace /path/to/workspace --provider ollama --protocol ollama_chat --name gemma4 --base-url http://localhost:11434 --tier middle --capability text --capability vision
maurice models assign coding ollama_gemma4 api_gpt_4o_mini --workspace /path/to/workspace
maurice models worker coding ollama_gemma4 --workspace /path/to/workspace
maurice models default ollama_gemma4 --workspace /path/to/workspace
```

### skills.yaml

Per-skill config, keyed by skill name. Advanced `skill.yaml` manifests can
override this with `config_namespace`, but lightweight user skills use
`skills.<skill_name>` by convention:

```yaml
skills:
  filesystem: {}
  memory:
    backend: sqlite
  web:
    search_provider: searxng
    base_url: http://localhost:18080
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
