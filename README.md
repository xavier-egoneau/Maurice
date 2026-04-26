# Maurice

Maurice is a contract-first rewrite of the Jarvis agent runtime idea.

The current MVP focuses on a small, inspectable kernel:

- typed runtime contracts
- separated runtime and workspace roots
- scoped permissions and approval records
- append-only events
- persisted sessions
- strict skill loading
- system skills for filesystem, memory, dreaming, skill authoring, self-update proposals, web access, host inspection, reminders, and vision

New workspaces still default to a deterministic mock provider, but Maurice now also has generic API/auth provider families for real model backends.

## Requirements

- Python 3.12+
- `pip`

## Quick Start

From the repo root:

```bash
./install.sh
maurice start
```

`install.sh` checks Python 3.12+, tries to install Python support when missing,
installs Maurice, links the `maurice` command, and launches onboarding.

Stop it from another terminal:

```bash
make stop
```

Read recent logs:

```bash
make logs
```

Open the local dashboard:

```bash
make dashboard
```

Plain live dashboard mode:

```bash
maurice dashboard --plain --watch
```

Plain one-shot dashboard:

```bash
maurice dashboard --plain
```

By default this creates/uses:

```text
/home/egza/Documents/workspace_maurice
```

You can override it:

```bash
WORKSPACE=/path/to/workspace_maurice ./install.sh
maurice run --workspace /path/to/workspace_maurice --message "salut Maurice"
```

Start the local gateway:

```bash
make gateway
```

Maurice listens on `http://127.0.0.1:18791` by default.

You can also start Telegram polling only:

```bash
make telegram
```

`./install.sh` opens a small interactive setup and asks for:

- permission profile
- model provider:
  - `chatgpt`: ChatGPT subscription auth, no OpenAI API key
  - `openai_api`: OpenAI-compatible API, base URL plus API key
  - `ollama`: local or hosted Ollama
- gateway port
- optional Telegram bot pre-configuration: BotFather token and allowed Telegram user ids
- provider model/base URL/credential basics when relevant

For ChatGPT, the wizard lists models from `~/.codex/models_cache.json` when
available, and still accepts a manual model id. For Ollama, the wizard first asks
whether Ollama is `auto_heberge` or `cloud`. Auto-hosted Ollama asks for the URL
and lists models from `<ollama_url>/api/tags` when reachable. Ollama Cloud asks
for the endpoint, API key, and stores that key as an agent credential.

Web search is configured silently to local SearxNG at `http://localhost:8080`.
The wizard does not ask about it yet because SearxNG is the only implemented
search backend. Provider selection will return once more search backends exist.

For Telegram, create the bot with `@BotFather`, get your Telegram id with
`@userinfobot` or `@RawDataBot`, and enter one or more allowed ids separated by
commas. The wizard can ask you to send a first message to the bot and check that
the id matches before continuing.

During agent conversations, Maurice can also arm a one-message secret capture
for bot tokens. The next Telegram message is stored as a credential and is not
forwarded to the model or persisted in the agent session.

You can rerun `maurice onboard` later. The wizard shows the current values, and pressing Enter keeps them.

You can also create a durable agent with:

```bash
maurice onboard --agent coding
```

Change only an existing agent model with:

```bash
maurice onboard --agent coding --model
```

## Manual Install For Local Development

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

After install, use the console script:

```bash
maurice --help
```

You can also run commands without the console script:

```bash
python3 -m maurice.host.cli --help
```

## Run Tests

```bash
pytest -q
```

Expected at this checkpoint:

```text
189 passed
```

## Create A Workspace

Maurice separates runtime code from mutable agent work.

- runtime: this repo/package
- workspace: agent state, sessions, user skills, artifacts, proposals

Create a local workspace:

```bash
maurice onboard --interactive \
  --workspace /tmp/maurice-workspace \
  --permission-profile limited
```

Check it:

```bash
maurice doctor --workspace /tmp/maurice-workspace
```

Run one mock turn:

```bash
maurice run \
  --workspace /tmp/maurice-workspace \
  --message "salut Maurice"
```

This should print:

```text
Mock response: salut Maurice
```

The turn is persisted under:

```text
/tmp/maurice-workspace/sessions/main/default.json
/tmp/maurice-workspace/agents/main/events.jsonl
```

## Workspace Shape

Onboarding creates:

```text
workspace/
  agents/main/
  artifacts/
  config/
    agents.yaml
    host.yaml
    kernel.yaml
    skills.yaml
  credentials.yaml
  sessions/
  skills/
```

Secrets stay in `credentials.yaml`, separate from normal config.
Agents do not receive credentials implicitly. Storing a credential and allowing
an agent to use it are separate steps:

```bash
python3 -m maurice.host.cli auth login chatgpt --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli agents update main --workspace /tmp/maurice-workspace --credential chatgpt
```

Agents may inspect credential names and metadata through the host skill, but
secret values are hidden. When a new token is needed from a conversation, the
host captures the next channel message directly into `credentials.yaml` and only
returns the credential name to the agent.

## Current CLI Commands

Common user-facing commands, once `./install.sh` has exposed `maurice`:

```bash
maurice onboard
maurice onboard --agent coding
maurice onboard --agent coding --model
maurice doctor
maurice run --message "hello"
maurice run --agent coding --message "hello"
maurice start
maurice stop
maurice logs
maurice dashboard
maurice dashboard --plain
maurice dashboard --plain --watch
```

Use the explicit workspace form when working outside the default workspace:

```bash
python3 -m maurice.host.cli --help
python3 -m maurice.host.cli doctor
python3 -m maurice.host.cli doctor --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli install
python3 -m maurice.host.cli install --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli onboard --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli onboard --workspace /tmp/maurice-workspace --agent coding
python3 -m maurice.host.cli onboard --workspace /tmp/maurice-workspace --agent coding --model
python3 -m maurice.host.cli run --workspace /tmp/maurice-workspace --message "hello"
python3 -m maurice.host.cli start --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli stop --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli logs --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli dashboard --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli agents list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli agents create coding --workspace /tmp/maurice-workspace --credential llm
python3 -m maurice.host.cli agents update coding --workspace /tmp/maurice-workspace --default --credential llm
python3 -m maurice.host.cli agents disable coding --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli agents archive coding --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli agents delete coding --workspace /tmp/maurice-workspace --confirm
python3 -m maurice.host.cli runs create --workspace /tmp/maurice-workspace --task "Run tests" --context-summary "Focused test task" --relevant-file tests/test_runs.py --constraint "Keep scope tight" --plan-step "Add coverage" --requires-self-check --write-path tests/** --permission-class fs.read
python3 -m maurice.host.cli runs create --workspace /tmp/maurice-workspace --task "Inline task" --inline-profile '{"id":"inline_coder","skills":["filesystem"],"permission_profile":"safe"}'
python3 -m maurice.host.cli runs list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs checkpoint <run-id> --workspace /tmp/maurice-workspace --summary "Paused safely"
python3 -m maurice.host.cli runs resume <run-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs execute <run-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs complete <run-id> --workspace /tmp/maurice-workspace --summary "Done" --changed-file tests/test_runs.py --verification-command "pytest tests/test_runs.py" --verification-status passed
python3 -m maurice.host.cli runs review <run-id> --workspace /tmp/maurice-workspace --status accepted --summary "Parent accepted the run"
python3 -m maurice.host.cli runs coordinate <run-id> --workspace /tmp/maurice-workspace --affects <other-run-id> --impact "Plan changed" --requested-action "Notify affected run"
python3 -m maurice.host.cli runs coordination-list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs coordination-ack <coordination-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs coordination-resolve <coordination-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs request-approval <run-id> --workspace /tmp/maurice-workspace --type dependency --reason "Need a dev package" --scope package_manager=pip
python3 -m maurice.host.cli runs approvals-list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs approvals-approve <approval-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli runs approvals-deny <approval-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli auth login chatgpt --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli auth status chatgpt --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli auth logout chatgpt --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli approvals list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli approvals approve <approval-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli approvals deny <approval-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli scheduler schedule-dream --workspace /tmp/maurice-workspace --skill memory
python3 -m maurice.host.cli scheduler run-once --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli scheduler serve --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli gateway local-message --workspace /tmp/maurice-workspace --message "hello"
python3 -m maurice.host.cli gateway serve --workspace /tmp/maurice-workspace
curl -X POST http://127.0.0.1:18791/channels/local_http/message -d '{"peer":"browser","message":"hello"}'
python3 -m maurice.host.cli service status --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli service logs --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli monitor snapshot --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli monitor events --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli migration inspect --jarvis /path/to/jarvis-workspace
python3 -m maurice.host.cli migration run --jarvis /path/to/jarvis-workspace --workspace /tmp/maurice-workspace --dry-run
python3 -m maurice.host.cli self-update list --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli self-update validate <proposal-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli self-update test <proposal-id> --workspace /tmp/maurice-workspace
python3 -m maurice.host.cli self-update apply <proposal-id> --workspace /tmp/maurice-workspace --confirm-approval
```

`run` uses the configured model provider. Non-interactive development workspaces can still default to the mock provider, but the interactive onboarding only offers real providers. The generic `api` provider supports OpenAI-compatible APIs, `ollama` supports local/self-hosted and cloud Ollama endpoints, and `auth` supports the ChatGPT account/session path.

## Test Skills Directly

Until richer CLI tool-calling exists, use Python snippets to test system skills directly.

### Filesystem

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.filesystem.tools import write_text, read_text

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace").mkdir()
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))

    print(write_text({"path": "notes.md", "content": "hello"}, ctx).summary)
    print(read_text({"path": "notes.md"}, ctx).data["content"])
PY
```

### Memory

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.memory.tools import remember, search

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace").mkdir()
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))

    print(remember({"content": "Memory lives outside the kernel.", "tags": ["architecture"]}, ctx).summary)
    print(search({"query": "kernel"}, ctx).data["memories"][0]["content"])
PY
```

### Dreaming

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoader, SkillRoot
from maurice.system_skills.memory.tools import remember, build_dream_input
from maurice.system_skills.dreaming.tools import run

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace").mkdir()
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))

    remember({"content": "Dreams consume skill-provided signals.", "tags": ["dreaming"]}, ctx)
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["memory", "dreaming"],
    ).load()
    result = run(
        {"skills": ["memory"]},
        ctx,
        registry,
        dream_input_builders={"memory": lambda: build_dream_input(ctx)},
    )
    print(result.summary)
    print(result.data["path"])
PY
```

### Skill Authoring

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillRoot
from maurice.system_skills.skills.tools import create, reload

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace" / "skills").mkdir(parents=True)
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))
    roots = [
        SkillRoot(path="maurice/system_skills", origin="system", mutable=False),
        SkillRoot(path=str(root / "workspace" / "skills"), origin="user", mutable=True),
    ]

    print(create({"name": "notes_helper"}, ctx, roots).summary)
    print(any(skill["name"] == "notes_helper" for skill in reload({}, roots).data["skills"]))
PY
```

### Self-Update Proposal

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.self_update.tools import propose

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace").mkdir()
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))

    result = propose({
        "target_type": "system_skill",
        "target_name": "memory",
        "runtime_path": "$runtime/maurice/system_skills/memory",
        "summary": "Improve memory search ranking.",
        "patch": "diff --git a/memory b/memory\n",
        "risk": "low",
        "test_plan": "Run pytest.",
        "mode": "proposal_only",
    }, ctx)
    print(result.summary)
    print(result.data["path"])
PY
```

### Web

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.web.tools import fetch

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "workspace").mkdir()
    (root / "runtime").mkdir()
    ctx = PermissionContext(workspace_root=str(root / "workspace"), runtime_root=str(root / "runtime"))

    result = fetch({"url": "https://example.com", "max_chars": 120}, ctx)
    print(result.summary)
    print(result.trust)
PY
```

## MVP Status

Implemented:

- Phase 0: repo/package skeleton
- Phase 1: typed contracts
- Phase 2: config and workspace
- Phase 3: events and sessions
- Phase 4: permissions and approvals
- Phase 5: skill loader
- Phase 6: provider interface and one-turn loop
- Phase 7: filesystem system skill
- Phase 8: host CLI and onboarding
- Phase 9: memory system skill
- Phase 10: dreaming system skill
- Phase 11: skill authoring system skill
- Phase 12: self-update proposals
- Post-MVP: generic API/auth provider families, ChatGPT account auth, approval CLI, scheduler-driven dream jobs, scheduler service, channel-neutral gateway routing, local HTTP gateway, local HTTP channel adapter, web/host/reminders/vision system skills, permanent agent config management, subagent run lifecycle, autonomous execution policy, host install/service inspection, generic monitoring snapshots, conservative Jarvis migration tooling, and host-owned self-update apply flow

Known MVP limitations:

- provider integrations are still early; `MockProvider`, generic `api`, and `auth/chatgpt_codex` are implemented, but ChatGPT auth uses an experimental ChatGPT/Codex backend path inherited from the Jarvis prototype and still needs real-world validation
- approvals are stored, replayed, and resolvable through the CLI, but there is no channel or dashboard approval UI yet
- scheduler jobs can be run once or through the long-lived `scheduler serve` loop
- gateway routing exists for local channel-neutral messages, a stdlib HTTP gateway, a `local_http` channel adapter, and simple Telegram polling
- web fetch/search exists as a system skill; search expects a configured SearxNG-compatible endpoint
- permanent agents can be listed, created, updated, disabled, archived, deleted with confirmation, selected for `run`, scoped to explicit credentials, and guarded against unconfirmed permission elevation
- subagent runs can be created, listed, started, checkpointed, completed, failed, and cancelled with machine-readable checkpoint/final envelopes
- each subagent run writes a standalone `mission.json` with task, compact context, relevant files, constraints, plan, scopes, dependency policy, and output contract
- development runs that require self-check cannot complete without verification evidence in the final envelope
- paused or cancelled runs can only resume when `safe_to_resume` is true
- subagent-to-subagent coordination is parent-owned through auditable coordination events
- subagent runs can request parent approval for permission/dependency escalation and are checkpointed while blocked
- subagent runs carry an explicit autonomy policy so execution continues until blocked, complete, or limited, rather than asking between micro-phases
- run final envelopes reject changed files outside the declared `write_scope`
- dependency approval requests must be allowed by the run mission dependency policy
- every run workspace includes an isolated `session.json` alongside `mission.json`
- run missions resolve and snapshot their base agent profile at creation time
- run missions may use either an existing base agent or an explicit inline profile
- `runs execute` follows the run autonomy policy and checkpoints with a clear stop reason until a real execution engine is registered
- completed runs require an explicit parent review before they are considered accepted
- `install` checks local Python/runtime/workspace prerequisites without starting services
- `service status` and `service logs` expose host-side inspection hooks for local workspaces
- agent-facing host inspection is exposed through `host.status` and `host.logs` with `host.control` permission checks
- `monitor snapshot` and `monitor events` expose generic runtime, agent, skill, approval, job, run, and event state for future dashboard work
- `reminders.create`, `reminders.list`, and `reminders.cancel` persist reminder state and schedule `reminders.fire` jobs through the generic scheduler
- `vision.inspect` prepares local image artifacts, and `vision.analyze` is ready for an injected/configured backend while keeping image logic out of the kernel loop
- `migration inspect/run` can dry-run and migrate compatible Jarvis user skills, explicit memory exports, and selected artifacts with provenance; raw Jarvis config/sessions are excluded
- malformed optional skills are isolated as `disabled_with_error` with suggested fixes instead of breaking the runtime
- CLI `run` does not yet expose a friendly way to trigger arbitrary tool calls
- no dashboard, TUI, Telegram, vision, cron, or remote marketplace
- self-update tools only create proposals; applying runtime patches is a host-owned `self-update apply --confirm-approval` flow with validation, tests, events, report, and rollback instructions

## Useful Files

- [SPEC.md](SPEC.md): product intent and rewrite rationale
- [ARCHITECTURE.md](ARCHITECTURE.md): host/kernel/skills split
- [CONTRACTS.md](CONTRACTS.md): runtime envelopes and typed contracts
- [SECURITY.md](SECURITY.md): permission classes and profiles
- [PROVIDERS.md](PROVIDERS.md): generic API/auth provider families and Jarvis migration notes
- [MVP_BUILD.md](MVP_BUILD.md): implementation backlog
- [POST_MVP_PLAN.md](POST_MVP_PLAN.md): next work after the completed MVP
- [SELF_UPDATE.md](SELF_UPDATE.md): runtime proposal workflow
