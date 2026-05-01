# Config Reference

Four YAML files live in `<workspace>/`:

## host.yaml

```yaml
runtime_root: /path/to/maurice          # where kernel/, host/, system_skills/ live
workspace_root: /path/to/workspace      # agent workspace (mutable)
gateway:
  host: 127.0.0.1
  port: 18791
skill_roots:
  - path: /path/to/system_skills
    origin: system
    mutable: false
  - path: /path/to/workspace/skills
    origin: user
    mutable: true
channels:
  telegram:
    token: <bot-token>
    peer_ids: [123456789]
```

## kernel.yaml

```yaml
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
  ttl_seconds: 1800       # how long a pending approval is valid
  remember_ttl_seconds: 600
  classifier_model: ""    # model for auto mode (defaults to main model)
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
  trim_threshold: 0.60    # TRIM level (drop old turns)
  summarize_threshold: 0.75  # SUMMARIZE level (LLM summary)
  reset_threshold: 0.90   # RESET level (full session reset)
  keep_recent_turns: 10
```

## agents.yaml

```yaml
agents:
  main:
    id: main
    name: Maurice
    status: active          # active | disabled | archived
    default: true
    workspace: /path/to/workspace
    permission_profile: limited   # safe | limited | power
    credentials: ["*"]      # credential names the agent may use (* = all)
    skills: []              # override kernel.skills for this agent
    model:                  # override kernel.model for this agent (optional)
      provider: api
      protocol: anthropic
      name: claude-opus-4-5
      credential: anthropic
    event_stream: null      # path to events.jsonl (defaults to agents/<id>/events.jsonl)
```

## skills.yaml

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
| `auth` | `chatgpt_codex` | ChatGPT Codex (stored auth token) |

## Credentials store

Credentials live in `<workspace>/secrets/credentials.yaml` (never committed).
Each credential has `name`, `value`, and optional `base_url`.
Agents access credentials listed in `agents.<id>.credentials` (or `["*"]` for all).
