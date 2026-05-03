# Turn Lifecycle

A turn starts with `AgentLoop.run_turn(agent_id, session_id, message)` in `kernel/loop.py`.

## Steps

```
1. Load or create session (SessionStore)
2. Emit turn.started event
3. Compact context if needed (see below)
4. Append user message to session
5. Loop (up to max_tool_iterations, default 10):
   a. Stream model output (provider.stream)
   b. Accumulate text_delta chunks → assistant text
   c. For each tool_call chunk → _handle_tool_call
   d. Break when: no tool calls, status=failed, or approval_required
6. Persist final assistant message and tool results
7. Emit turn.completed event
8. Return TurnResult
```

## Tool call pipeline (_handle_tool_call)

For every tool call the model requests, in order:

```
1. Lookup tool in SkillRegistry          — unknown tool → error
2. evaluate_permission (profile rules)   — deny → error
3. shell_parser.parse (shell.exec only)  — critical/elevated → requires new approval
4. _run_classifier (if mode=auto)        — block → classifier_blocked error
5. Check approval_store                  — missing approval → approval_required error
6. Execute tool_executor
7. Emit tool.executed event
```

Only step 6 touches the outside world. Steps 1–5 are pure policy.

## Context compaction

Triggered at step 3 of each turn. Three levels based on estimated token usage:

| Level | Threshold | Action |
|---|---|---|
| TRIM | 60% | Drop oldest turns, keep `keep_recent_turns` (default 10) |
| SUMMARIZE | 75% | LLM call to summarize oldest turns into one system block |
| RESET | 90% | LLM summary of full session → new first message; emits `session.compacted` |

Token estimate: `len(content) // 4` per message (configurable window, default 250k).
Compaction does nothing below 60% and is silent below RESET level. At RESET
level, Maurice leaves a visible notice in the session so the user knows the
history was compacted.
Can be disabled with `kernel.sessions.compaction: false`.

## Permission profiles

Three built-in profiles, set per agent in `agents.yaml`:

| Class | safe | limited | power |
|---|---|---|---|
| `fs.read` | allow ($workspace + $project) | allow ($workspace + $project) | allow ($workspace + $project + $home, protected paths excluded) |
| `fs.write` | ask ($workspace + $project) | allow ($workspace + $project) | allow ($workspace + $project) |
| `network.outbound` | ask | ask | allow |
| `shell.exec` | deny | ask | ask |
| `secret.read` | ask | ask | ask |
| `agent.spawn` | deny | deny | ask |
| `host.control` | deny | deny | ask |
| `runtime.write` | deny | deny | ask |

## Approval modes

Set via `kernel.approvals.mode`:

- `ask` (default) — interrupt the user for every `ask`-class permission
- `auto_deny` — deny all `ask`-class permissions without interrupting
- `auto` — run the LLM classifier to decide; fall back to `ask` if circuit breaker opens

## TurnResult

```python
@dataclass
class TurnResult:
    session: SessionRecord
    correlation_id: str
    assistant_text: str
    tool_results: list[ToolResult]
    status: str          # "completed" | "failed"
    error: str | None
```

Tool results include both successful and failed executions.
Failed results have `ok=False` and `error.code` set (e.g. `approval_required`, `classifier_blocked`, `permission_denied`).
