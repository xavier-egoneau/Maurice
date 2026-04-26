# Maurice Providers

## Purpose

Providers translate model backends into the Maurice provider contract.

The kernel should only consume normalized chunks:

- text deltas
- tool calls
- usage metadata
- terminal status
- structured provider errors

Provider-specific authentication, request formats, streaming formats, and retry
behavior should stay inside provider implementations or host auth helpers.


## Provider Families

### `mock`

Purpose:

- deterministic tests
- runtime wiring validation
- examples without external services

Authentication:

- none

Status:

- implemented


### `api`

Purpose:

- URL/key based model access
- one provider family for OpenAI-compatible APIs, local OpenAI-compatible proxies, and Ollama

Authentication:

- `base_url`
- optional API key loaded from credentials store
- protocol-specific request and stream parsing

Protocols:

- `openai_chat_completions`
- `ollama_chat`

Example:

```yaml
kernel:
  model:
    provider: api
    protocol: openai_chat_completions
    name: gpt-5.4-mini
    base_url: https://api.openai.com/v1
    credential: openai

credentials:
  openai:
    type: api_key
    value: sk-...
```

Local Ollama example:

```yaml
kernel:
  model:
    provider: ollama
    protocol: ollama_chat
    name: llama3.2
    base_url: http://localhost:11434
```

Provider families:

- `provider: openai` maps to `provider: api`, `protocol: openai_chat_completions`
- `provider: ollama` is a first-class Ollama provider and uses `protocol: ollama_chat`
- Ollama may be auto-hosted/local or cloud/remote; cloud endpoints require a credential

Status:

- implemented in Maurice
- OpenAI-compatible behavior was implemented in Jarvis as `jarvis/providers/openai.py`
- Ollama behavior was implemented in Jarvis as `jarvis/providers/ollama.py`


### `auth`

Purpose:

- login/session based model access
- used when URL/key is not enough

Authentication:

- explicit browser login/session flow
- token/session stored as a secret credential
- refresh when possible
- logout/status commands in host CLI

Jarvis reference:

- `jarvis/auth/openai.py` implements a browser OAuth PKCE flow
- `jarvis/providers/chatgpt.py` implements a ChatGPT provider using an access token
- `jarvis/cli/auth.py` provides login, status, and logout commands
- `tests/test_chatgpt_provider.py` covers response and tool-call normalization helpers

Important caveat:

- Jarvis uses a ChatGPT/Codex backend path and experimental response headers.
- Maurice may reuse the shape and tests, but the endpoint and auth details must be revalidated before treating this as stable.
- This path should remain isolated behind the provider contract because it has different auth, rate-limit, expiry, and failure behavior than API-key providers.

Implemented protocol:

- `chatgpt_codex`

Status:

- implemented in Maurice
- needs real-world validation because the ChatGPT/Codex backend path is not the same as stable OpenAI API access


## Current Maurice Contract

Provider implementations must expose:

```python
stream(
    *,
    messages,
    model,
    tools,
    system,
    limits=None,
) -> Iterable[ProviderChunk]
```

`ProviderChunk` is the only shape the kernel should see.

Provider chunks:

- `text_delta`
- `tool_call`
- `usage`
- `status`

Provider terminal states:

- `running`
- `completed`
- `failed`


## Normalization Rules

### Messages

Maurice session messages currently enter providers as dictionaries.

Each provider should translate those dictionaries into its native request shape
internally.

Provider code should support:

- `user` messages
- `assistant` messages
- tool result messages when the loop adds them
- assistant tool-call history when needed by the backend


### Tools

Maurice tools are `ToolDeclaration` records.

Providers must convert declarations into their backend-native function/tool
schema without changing the canonical tool names.

Canonical names stay:

```text
<skill>.<tool>
```


### Tool Calls

Provider tool calls must normalize into:

```yaml
id: "call_..."
name: "skill.tool"
arguments: {}
```

If a backend returns arguments as a JSON string, the provider should parse it
into a dictionary before yielding `ProviderChunk`.

Invalid JSON should produce a failed provider status or a structured provider
error, not an untyped traceback.


### Usage

If the backend reports usage, normalize it into:

```yaml
input_tokens: 0
output_tokens: 0
```

If usage is unavailable, omit the usage chunk.


### Errors

Provider failures should become:

```yaml
type: "status"
status: "failed"
error:
  code: "..."
  message: "..."
  retryable: false
```

Secrets must not appear in error messages, event payloads, or CLI output.


## Auth And Secret Storage

API keys, refresh tokens, access tokens, cookies, and browser session material
are credentials.

They must not be stored in:

- `kernel.yaml`
- `host.yaml`
- `agents.yaml`
- `skills.yaml`
- session history
- event payloads

Preferred locations:

- workspace `credentials.yaml` for simple local secrets
- a future host credential backend for stronger storage

For `provider: auth`, the provider should not read or write `~/.jarvis`.
Maurice should store its own auth material under the Maurice workspace or host
credential system.


## Host CLI Commands

Suggested commands:

```bash
maurice auth login chatgpt --workspace /path/to/workspace
maurice auth status chatgpt --workspace /path/to/workspace
maurice auth logout chatgpt --workspace /path/to/workspace
```

The login command may open a browser and start a local callback server.

The status command should report only:

- connected or not connected
- expiry time or approximate remaining lifetime
- account/workspace identity when safe

It should not print tokens.


## Implementation Order

Recommended order:

1. Keep `provider: api` as the generic OpenAI-compatible URL/key provider family.
2. Keep `provider: ollama` as a first-class provider with local and cloud deployment modes.
3. Add CLI/provider config tests.
4. Validate `provider: auth`, `protocol: chatgpt_codex` against a real ChatGPT subscription.
5. Harden auth error reporting and expiry handling based on real failures.

This keeps the common URL/key case simple while leaving room for login-based
providers that need more than a base URL and API key.


## What To Reuse From Jarvis

Good to reuse conceptually:

- PKCE login flow shape from `jarvis/auth/openai.py`
- token refresh behavior
- provider registry distinction between `openai` and `chatgpt`
- ChatGPT response/tool-call normalization tests
- Ollama request/streaming translation

Adapt before reuse:

- move storage from `~/.jarvis/auth/openai.json` to Maurice credentials
- replace Jarvis async provider contract with Maurice `ProviderChunk`
- convert Jarvis `ToolCall.arguments` JSON strings into Maurice dictionaries
- emit Maurice events for auth and provider failures
- keep endpoint and header choices configurable where needed

Avoid copying:

- direct dependency on Jarvis settings
- direct dependency on Jarvis credential resolution
- provider behavior hidden in agent tools or prompts
- tokens in config files or logs
