# Maurice Post-MVP Plan

## Purpose

The MVP proves the kernel shape. The next work should turn Maurice from a
mock-driven runtime into a usable local-first agent without weakening the
host/kernel/skills boundary.

This plan starts from the current checkpoint:

- tests pass
- `main` agent exists
- workspace onboarding exists
- permissions, approvals, sessions, events, and skill loading exist
- malformed optional skills are isolated with loader errors and suggested fixes
- filesystem, memory, dreaming, skill authoring, self-update proposal, web, host inspection, reminders, and vision skills exist
- OpenAI-compatible and Ollama-compatible providers exist
- ChatGPT account/auth provider exists as `provider: auth`, `protocol: chatgpt_codex`, with experimental-backend caveats
- minimal scheduler job store exists
- scheduler can run due `dreaming.run` jobs through `maurice scheduler run-once`
- channel-neutral gateway envelopes and local routing exist
- local HTTP gateway service exists
- long-lived scheduler service exists
- `web` system skill exists with `web.fetch` and SearxNG-compatible `web.search`
- permanent agents can be listed, created, updated, selected, and audited through config events
- subagent run lifecycle, envelopes, parent review, approval bridging, and coordination events exist
- host install checks and service status/log inspection hooks exist
- local HTTP channel adapter exists over the gateway
- generic monitoring snapshots and event tail CLI exist
- reminders skill persists reminders and fires them through generic scheduler jobs
- vision skill prepares local image content and supports injectable analysis backends
- conservative Jarvis migration tooling can inspect, dry-run, and migrate compatible user-owned data
- host-owned self-update apply flow can validate, test, apply, report, and emit events for runtime proposals
- autonomous execution policy lets runs continue until blocked, complete, or bounded by limits
- channels and real subagent execution are not done yet


## Guiding Rule

Do not grow the kernel for product features.

The kernel should gain only generic runtime machinery:

- provider normalization
- scheduling
- gateway ingress and egress
- agent/run orchestration
- state snapshots

User-facing capabilities should become skills or host adapters.


## Post-MVP Phase 1: Make The Agent Real

Goal: replace the mock-only path with a real model-backed local agent loop.

Deliverables:

- harden ChatGPT account/auth provider for users with a ChatGPT subscription but no API key
- use `PROVIDERS.md` as the implementation contract for provider auth and normalization
- load provider credentials through the credentials store
- support tool calls from real provider responses
- normalize provider errors into runtime events
- add CLI flags for provider/model overrides per run
- keep `MockProvider` as the deterministic test provider

Acceptance:

- `maurice run --message "..."` works with a real configured provider
- `provider: api` supports OpenAI-compatible URL/key providers through `protocol`, `base_url`, and optional `credential`
- `provider: ollama` supports local/self-hosted and cloud Ollama endpoints through `protocol`, `base_url`, and optional `credential`
- `provider: auth`, `protocol: chatgpt_codex` supports login/session based ChatGPT account auth
- provider credentials never appear in normal config or events
- conversational secret capture stores the next Telegram message as a host
  credential without forwarding it to the model
- provider failures produce structured events and readable CLI output
- tests cover at least mock, provider config selection, and provider error normalization

Important distinction:

- `provider: api` means OpenAI-compatible URL/key style access.
- `provider: ollama` means Ollama-compatible access, either auto-hosted/local or cloud/remote.
- `provider: auth` means login/session style access. Example: ChatGPT account auth without API billing or an API key.
- The ChatGPT account/auth path must be isolated because it has different authentication, persistence, rate limits, and failure modes than the API path.


## Post-MVP Phase 2: Interactive Approvals

Goal: make the existing approval system usable from the CLI and ready for channels.

Deliverables:

- list pending approvals
- approve or deny a pending approval
- support one-shot and remembered approvals where policy allows it
- display approval id, status, tool, permission class, and summary
- resume or retry a blocked run after approval

Suggested CLI:

```bash
maurice approvals list --workspace /path/to/workspace
maurice approvals approve <approval-id> --workspace /path/to/workspace
maurice approvals deny <approval-id> --workspace /path/to/workspace
```

Acceptance:

- denied approvals are persisted and auditable
- remembered approvals fail if tool arguments or scope change
- approval events include correlation ids
- no approval path grants broader access than the stored scope


## Post-MVP Phase 3: Scheduler And Dream Jobs

Goal: implement the generic background runner without baking dreaming into the kernel.

Deliverables:

- implement `maurice/kernel/scheduler.py`
- add job model, job store, and job events
- support recurring jobs and one-shot jobs
- run `dreaming.run` as a scheduled skill tool
- add cancellation, timeout, and failure reporting
- expose scheduler toggles in kernel config

Acceptance:

- scheduler can trigger a dream run with memory enabled
- dream output is persisted as a `DreamReport`
- failed jobs produce structured events
- disabling scheduler disables background work without disabling the skills


## Post-MVP Phase 4: Gateway And Channel-Neutral Runtime

Goal: make Maurice long-lived before adding specific channels.

Deliverables:

- implement `maurice/host/gateway.py`
- define inbound and outbound message envelopes
- route inbound messages to agent id, session id, peer id, and correlation id
- keep channel adapters thin
- expose health and state snapshots for host monitoring
- add graceful startup and shutdown events

Acceptance:

- gateway can process a local inbound message without Telegram or web UI
- sessions are routed by agent/session/peer identity
- outbound responses use a channel-neutral envelope
- gateway behavior does not introduce channel-specific logic into the kernel loop


## Post-MVP Phase 5: Web Skill

Goal: add internet access as a skill, not a kernel feature.

Status: implemented.

Deliverables:

- create `web` system skill: done
- support search and fetch tools: done
- start with a SearxNG-compatible search backend: done
- enforce `network.outbound` permission scopes: done
- mark fetched content as `external_untrusted`: done
- add `dreams.md` describing freshness and trust limits: done

Tools:

- `web.search`
- `web.fetch`

Acceptance:

- network calls are denied, allowed, or approval-gated by policy: done
- fetched content is clearly separated from trusted local state: done
- the kernel contains no web-specific behavior: mostly done; only generic URL-to-host permission narrowing lives in the kernel loop

Later:

- add a Brave Search backend as a first-class `web.search` provider
- add a DuckDuckGo backend as a first-class `web.search` provider
- expose web search provider selection in onboarding only when the backend is implemented and tested


## Post-MVP Phase 6: Permanent Agents

Goal: move from config-ready multi-agent support to actual durable agent peers.

Status: first implementation done.

Deliverables:

- create/list/update permanent agent configs: done
- create per-agent workspace directories: done
- bind skills and permission profiles per agent: done
- enforce more-permissive-than-global confirmation rules: done
- keep sessions, events, and approvals isolated by agent: done
- keep credentials isolated by agent: done at runtime through explicit per-agent credential allowlists; physical storage remains workspace-level
- support durable agent lifecycle operations (`disable`, `archive`, and explicit destructive `delete`): done

Suggested CLI:

```bash
maurice agents list --workspace /path/to/workspace
maurice agents create coding --workspace /path/to/workspace --credential llm
maurice agents update coding --workspace /path/to/workspace --default --credential llm
maurice agents disable coding --workspace /path/to/workspace
maurice agents archive coding --workspace /path/to/workspace
maurice agents delete coding --workspace /path/to/workspace --confirm
maurice run --agent coding --message "..."
```

Acceptance:

- `main` remains the default durable agent: done unless another agent is explicitly marked default
- another durable agent can run with its own event stream and session store: done
- agent config changes are auditable: done
- agents do not implicitly inherit each other's sessions or approvals: done
- agents do not implicitly inherit secrets: done through explicit credential allowlists
- removing an agent has a non-destructive path first, preserves audit by default, and requires explicit confirmation for data deletion: done


## Post-MVP Phase 7: Subagent Runs

Goal: implement disposable task runs with scoped permissions and checkpointing.

Status: lifecycle store, CLI, mission preparation, and executor skeleton implemented; real subagent model execution is not done yet.

Deliverables:

- create subagent run workspace: done
- create run session and event stream: done at lifecycle level; execution does not yet append live messages
- resolve base agent or inline profile: done at mission/lifecycle level; executor use is not done
- narrow skills and permissions: partial; scopes are recorded and final changed files are checked against write scope, but no run executor sandbox exists yet
- add run states: `created`, `running`, `checkpointing`, `paused`, `completed`, `failed`, `cancelled`: done
- produce checkpoint and final result envelopes: done
- support cancellation request and safe resume: done at lifecycle/checkpoint level
- build a standalone mission packet for each run: done
- optimize run context for tokens while preserving enough local context to work: partial; compact mission fields exist, automatic context selection is not done
- include relevant files, constraints, plan, and expected output contract in the mission packet: done
- require subagent self-check before final result for development runs: done at lifecycle/CLI level
- include verification results, changed files, risks, and followups in final envelopes: done
- define dependency policy for runs, including whether dependency installation can be requested: done and enforced for approval requests
- implement approval bridge from subagent run to parent agent/user for permission or dependency escalation: done at lifecycle/CLI level
- pause/checkpoint when a run blocks on approval: done
- route subagent-to-subagent coordination through the parent, not direct free-form chat: done at lifecycle/CLI level
- record coordination events with source run, affected run(s), impact, requested action, and acknowledgement status: done

Acceptance:

- every run has explicit task, write scope, permission scope, parent, and context policy: done
- run output is machine-readable first: done
- cancellation attempts a checkpoint before stopping: done at lifecycle level
- resume requires `safe_to_resume: true`: done at lifecycle/CLI level
- subagent work is launched from a standalone, token-conscious mission packet: partial; mission packet is loaded into an isolated run session, real model execution is not done
- subagent permissions cannot broaden themselves; escalation must become an approval request to the parent: done at lifecycle/CLI level
- development subagent output includes self-check evidence before parent integration: done at lifecycle/CLI level
- parent remains responsible for reviewing/integrating subagent output before user-facing synthesis: done at lifecycle/CLI level through parent review envelopes
- subagents cannot silently change each other's plan; plan changes go through parent-owned coordination events: done at lifecycle/CLI level


## Post-MVP Phase 8: Host Install And Service Management

Goal: separate infrastructure setup from product onboarding.

Status: first host inspection layer implemented; restart/service control is not done yet.

Deliverables:

- add `maurice install`: done
- check local prerequisites: done for Python/runtime/package/workspace config
- prepare optional local services: not done
- add service status and logs hooks: done
- support host control permissions for restart/status/logs: partial; `host.status` and `host.logs` go through `host.control`, restart is not done
- keep onboarding focused on model, workspace, skills, agents, and channels: done

Acceptance:

- install can verify Python/runtime prerequisites: done
- service operations go through `host.control`: partial; agent-facing status/log tools do, direct user CLI inspection remains host-side
- onboarding does not silently start unrelated infrastructure: done


## Post-MVP Phase 9: Channel Adapters

Goal: add real user entry points through the gateway.

Status: first local HTTP channel adapter implemented; simple Telegram polling implemented.

Initial adapters:

- local HTTP or Unix socket channel for testing: local HTTP done
- Telegram adapter after the gateway contract is stable: simple polling done; webhook/service hardening not done

Deliverables:

- channel config and credential references: partial; local HTTP and Telegram config exist, broader credential-bearing adapters are not done
- inbound message normalization: done for local HTTP and Telegram polling
- outbound delivery handling: done for local HTTP inline delivery and Telegram `sendMessage`
- channel-specific formatting in host adapter only: done for local HTTP and Telegram polling
- delivery error events: partial; success events exist, explicit failure events are not done
- onboarding step for Telegram bot token, allowed users/chats, and target durable agent: done for polling mode
- Telegram polling through `maurice gateway telegram-poll`: done
- Telegram conversational secret capture for future agent/channel setup: done for polling mode

Acceptance:

- Telegram can send a message to `main` through the gateway: done for polling mode
- onboarding proposes Telegram setup without asking for irrelevant channel details: done for polling mode
- channel messages resolve to agent id, session id, peer id, and correlation id: done for local HTTP and Telegram polling
- subagent runs do not receive direct channel traffic unless promoted: structurally true; channel adapters resolve to durable agent ids


## Post-MVP Phase 10: Monitoring And Minimal Dashboard Data

Goal: expose state generically before building any rich UI.

Status: first generic snapshot and event-tail CLI implemented.

Deliverables:

- runtime state snapshot: done
- agent state snapshot: done
- skill health snapshot: done
- pending approval snapshot: done
- job/run state snapshot: done
- event tail endpoint or CLI: done as CLI

Acceptance:

- monitoring consumes generic events and state payloads: done
- no dashboard branch depends on a hard-coded skill: done
- CLI can inspect enough state to debug a local instance: done


## Post-MVP Phase 11: Cron And Reminder Skill

Goal: add user-facing scheduled actions as a skill over the generic scheduler.

Status: reminder skill implemented.

Deliverables:

- create `cron` or `reminders` system skill: done as `reminders`
- add tools to create/list/cancel reminders: done
- store reminder state under skill storage: done
- use scheduler for execution: done through `reminders.fire` jobs
- pass execution through normal tool and approval flow: done for agent tool calls

Acceptance:

- reminders survive process restart: done via JSON storage under workspace content
- reminder execution emits events: done
- scheduler remains domain-neutral: done; it only dispatches the `reminders.fire` job name to host skill handlers


## Post-MVP Phase 12: Vision Skill

Goal: add image understanding without kernel-specific vision logic.

Status: first vision skill implemented with local image inspection and injectable backend analysis.

Deliverables:

- create `vision` system skill: done
- define image input artifact handling: done through `vision.inspect`
- support configured provider or backend: partial; injectable backend supported, provider-backed multimodal routing is not done
- tag image-derived content with trust metadata: done
- document dream usage in `dreams.md`: done

Acceptance:

- image analysis is exposed through declared tools: done
- vision can be disabled without affecting the kernel: done
- no image-specific behavior is added to the turn loop: done


## Post-MVP Phase 13: Jarvis Migration Tools

Goal: migrate only stable user-owned data.

Status: conservative migration inspect/run implemented.

Deliverables:

- inspect Jarvis workspace: done
- migrate selected credentials where safe: partial; credentials are detected and explicitly skipped pending provider-specific review
- migrate user skills where compatible: done for skill directories with `skill.yaml`
- migrate memory through explicit export/import: done for JSON exports
- copy selected content: done behind `--include-content`
- write migration report: done

Acceptance:

- raw Jarvis config and sessions are not imported directly: done
- every migrated artifact has provenance: done
- migration can run in dry-run mode: done


## Post-MVP Phase 14: Runtime Self-Update Apply Path

Goal: keep proposal-first behavior, then add a host-owned apply flow.

Status: first host-owned apply flow implemented.

Deliverables:

- list proposals: done
- validate proposal metadata: done
- run proposal test plan: done for `$ ...` commands in `test_plan.md`
- apply approved patch through host operation: done through `git apply` with `--confirm-approval`
- record apply events: done
- support rollback instructions or generated reverse patch: done with rollback instructions

Acceptance:

- agents still cannot directly modify runtime files through normal tools: done
- apply requires explicit approval: done through `--confirm-approval`
- failed apply leaves a clear report and does not corrupt runtime state: done


## Post-MVP Phase 15: Autonomous Execution Policy

Goal: let agents continue within their authorized scope without asking between micro-phases.

Status: first execution policy implemented for subagent runs; real model-backed subagent execution is still not done.

Deliverables:

- define an explicit autonomy policy for runs: done
- support `continue_until_blocked`: done at policy/lifecycle level
- define stop conditions such as user decision, approval, permission denial, failing tests, plan complete, or missing executor: done
- support max step and checkpoint interval bounds: done
- write an autonomy report with stop reason and checkpoint path: done
- keep safety boundaries unchanged: done

Acceptance:

- an in-scope run does not require user confirmation between planned micro-steps: partial; policy exists, but no real execution engine is registered yet
- a run stops only on declared stop conditions or configured limits: done at executor skeleton level
- blocked runs produce resumable checkpoints: done
- permission/approval boundaries are unchanged: done


## Post-MVP Phase 16: User-Friendly Live Dashboard

Goal: turn `maurice dashboard` into a clear local control room for non-technical users without hard-coding product behavior into the kernel.

Status: first Textual/Rich shell implemented; real interaction model and generic data contracts still need work.

Product principle:

- the dashboard is a host UI over generic runtime state
- it reads snapshots, events, and declared capabilities
- it does not know that a skill is special unless the skill exposes generic metadata
- every action in the dashboard maps to an existing host command or generic runtime operation
- every mutation emits an auditable event
- labels use user words first, technical words only when unavoidable

Vocabulary:

- `Gateway` becomes `Service`
- `Scheduler` becomes `Automatismes`
- `Runs` becomes `Sessions` or `Taches`, depending on the row type
- `Cron Jobs` becomes `Automatismes`
- `Security` becomes `Permissions`
- `Provider` becomes `Modele`
- `Channels` becomes `Acces`
- `Skills` becomes `Capacites`
- `Logs` stays `Journal`

Dashboard areas:

- `Cluster`: show durable agents and temporary sub-tasks in real time
- `Automatismes`: show scheduled jobs, owner agent, next run, last run, status, and enabled state
- `Sessions`: show active and recent sessions, linked agent, origin, peer, and current status
- `Modeles`: show configured model per agent and allow changing it when the provider exposes choices
- `Permissions`: show and change each agent permission mode through the normal permission rules
- `Capacites`: show active skills per agent, skill source (`system` or `user`), health, and errors
- `Journal`: show colored events and errors with scrolling

Generic data contracts needed:

- add a `DashboardSnapshot` or extend monitoring snapshots with UI-neutral rows
- include agent activity state derived from structured events, not string matching
- include session summaries with agent id, session id, peer id, channel, origin, last event, and updated time
- include job summaries with owner agent, schedule, next run, last run, status, enabled flag, and last error
- include skill summaries with agent id, skill name, source, enabled flag, health, errors, and suggested fixes
- include model summaries with agent id, provider kind, model name, available choices when known, and auth state
- include permission summaries with agent id, current profile, global maximum, and whether escalation needs confirmation
- include log/event rows with level, source, timestamp, message, correlation id, and severity color

Interaction rules:

- navigation is local UI only; state changes always go through host operations
- pressing `Enter` on a row opens a simple detail/action panel
- toggles are explicit: enable/disable, never hidden side effects
- user skills can be enabled or disabled per agent
- system skills can be viewed and diagnosed, but not casually disabled from the dashboard unless policy allows it
- permission profile changes that broaden access require explicit confirmation
- job enable/disable changes only the selected job, not the whole automation service
- model changes apply only to the selected agent
- logs are read-only in the dashboard
- failed actions show a human-readable reason and keep the previous state

UX rules:

- every tab title uses simple words
- every table column answers a user question: who, what, state, next action, last problem
- active agents show a small animated status marker
- inactive agents are visually quieter
- errors are red, warnings yellow, active work orange, success green, neutral text muted
- empty states explain what is missing and the next useful command
- footer shows only the keys that work on the current tab
- details panels should explain consequences before mutating anything
- long logs need scroll support and should preserve color by severity/source

Implementation plan:

1. Define a dashboard view model independent of Textual widgets.
2. Add host operations for dashboard actions: set agent permission profile, set agent model, toggle user skill for agent, enable/disable job.
3. Enrich monitoring snapshots with sessions, job details, skill source/enabled state, model state, and log severity.
4. Replace hard-coded dashboard row builders with renderers over the dashboard view model.
5. Add Textual interactions: row selection, detail panel, confirmation modal, action feedback.
6. Add active-agent animation based on recent `turn.started`, `turn.completed`, run lifecycle, and job lifecycle events.
7. Add scrollable colored journal with severity/source styling.
8. Add tests for snapshot contracts, action permissions, and UI row generation.
9. Keep `--plain` as a stable script/debug output using the same view model.

Acceptance:

- `maurice dashboard` lets a user see which agents are active without reading logs
- the cluster tab shows a visible activity animation while an agent or sub-task is working
- automatismes can be enabled or disabled per job and show their owner agent
- sessions show active/recent conversations and task sessions with their agent and origin
- permissions can be changed per agent without bypassing global safety rules
- user skills can be toggled per agent, while system skills are clearly identified
- logs are scrollable, colored, and highlight errors
- no dashboard action mutates config directly without going through a host operation
- no tab depends on a hard-coded skill name or provider name


## Suggested Milestones

### Milestone A: Usable Local Agent

- real providers
- interactive approvals
- basic scheduler and dream jobs

Exit criteria:

- a user can onboard, configure a provider, run Maurice, approve a tool action, and see persisted memory/dream events

### Milestone B: Long-Lived Maurice

- gateway
- state snapshots
- service lifecycle
- first local channel

Exit criteria:

- Maurice can stay running and process channel-neutral inbound messages

### Milestone C: Useful Skills

- cron/reminder skill
- better skill storage/migration health

Exit criteria:

- common capabilities are added without changing the kernel contract

### Milestone D: Multi-Agent Runtime

- permanent agents
- subagent runs
- checkpoint/resume/cancel

Exit criteria:

- Maurice can delegate scoped work while preserving session, approval, and permission isolation

### Milestone E: Product Surface

- Telegram adapter
- monitoring/dashboard data
- Jarvis migration tools
- self-update apply flow
- live dashboard UX

Exit criteria:

- Maurice becomes practical as a daily assistant and migration target


## Immediate Next Sprint

Start with:

1. define the dashboard view model and user vocabulary
2. enrich monitoring snapshots with session, job, skill, model, permission, and log rows
3. implement dashboard host actions for toggles and profile/model changes
4. wire the Textual dashboard to the generic view model
5. keep `--plain` output generated from the same data

This sequence turns the MVP from a proven skeleton into something usable while keeping the architecture honest.
