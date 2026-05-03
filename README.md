# Maurice

Maurice is one AI assistant with two conversation surfaces:

- **Punctual folder surface**: run `maurice` or `maurice chat` from a folder. Maurice is
  focused on that folder, stores project/server state in `./.maurice`, and uses
  the current agent's durable memory.
- **Persistent assistant surface**: choose global assistant usage during setup, then run
  `maurice start`. Maurice stays available in the background, uses a shared
  workspace with agent-scoped memory, and can serve the browser chat, scheduler,
  dashboard, channels, and durable agents. When launched from a folder, the
  workspace stays the assistant's state root while that folder is treated as the
  active project.

These are not two products. They share the same agent runtime, permissions,
memory, sessions, approvals, and skills; the daemon only adds persistence,
scheduling, and channels.

## Core Concepts

- **Open space workspace**: desktop mode is an open workspace, not a private
  silo. Each agent owns its files under `<workspace>/agents/<agent-id>/`, but
  another agent can still inspect them when the user explicitly asks.
- **Agent-scoped memory and routines**: memory, reminders, dreams, notes, and
  todos belong to an agent. The default memory path is
  `<workspace>/agents/<agent-id>/memory/memory.sqlite`.
- **One active project per conversation**: every chat window, Telegram thread,
  or API session has one current project target. Multiple surfaces can still
  work on different projects at the same time.
- **Known projects are contribution history**: each agent keeps
  `<workspace>/agents/<agent-id>/projects.json`, meaning “projects this agent
  has touched”, not “all projects currently open on disk”.
- **Dev worker model config**: `/dev` workers use the parent agent by default,
  or the agent's configured worker model chain when one is set. Worker spawning
  is bounded: max 5 per call, max 10 active, with a per-worker tool/time budget.
  Durable autonomous work should become an explicit long mission, not an
  unbounded worker; see `docs/missions.md`.
- **Dream and daily attachments**: skills can ship `dreams.md` and `daily.md`.
  `dreams.md` says what the skill can surface during background synthesis;
  `daily.md` says what that skill wants the morning digest to consider.
- **Lightweight user skills**: ordinary user skills live under `skills/<name>/`
  with `skill.md`, `dreams.md`, and `daily.md`. Add `tools.py` only when the
  skill needs deterministic code such as a dream input builder or callable chat
  tools declared with `tool_declarations()`.
- **Shareable skills are autonomous**: a user skill should document or provide
  its own install, credential, config, diagnostic, and validation path so it can
  later move into a skill store without relying on hidden local setup.
- **Daily as optional synthesis**: the `daily` system skill turns the latest
  agent dream report plus loaded skill `daily.md` contributions into a morning
  digest. If the `daily` skill is not enabled for an agent, no daily job is
  scheduled for that agent. See `docs/automations.md` for timing and
  configuration.
- **Collective dreaming and watch topics**: optional skills can contribute
  multi-agent memory summaries (`workspace_dreaming`) or external watch signals
  (`veille`) without changing the core scheduler contract.

## Requirements

- Python 3.12+

## Install

```bash
cd /path/to/Maurice
./install.sh
maurice setup
```

`maurice setup` asks whether Maurice should start from a folder by default or as
a desktop assistant. The choice only selects the default context level; it does
not create a separate product. You can run `maurice setup` again later to switch
between both levels.

Start Maurice automatically when your Linux desktop session opens:

```bash
./install_autostart.sh
./install_autostart.sh --workspace /path/to/workspace
./install_autostart.sh --remove
```

## Folder Use

```bash
cd /path/to/project
maurice chat          # terminal chat
maurice web           # browser chat for this folder
```

State is kept under:

```text
./.maurice/
  config.yaml
  sessions/
  events.jsonl
  approvals.json
  run/
```

## Desktop Assistant Use

To keep Maurice available like a desktop assistant, run the setup wizard and
choose the global context level:

```bash
maurice setup
```

During setup:

- choose **global** when asked for the starting context level
- choose the workspace folder Maurice will use for sessions, agents, and
  agent-owned content, reminders, dreams, and memory
- choose a permission profile for what Maurice can do inside the workspace and
  the active project folder
- connect a provider: OpenAI-compatible API, OpenAI/ChatGPT browser auth,
  Ollama local, Ollama Cloud/remote API, or Anthropic API

Then start the assistant:

```bash
maurice start
```

`maurice start` does not silently expand a folder-focused setup into the desktop
assistant. It starts the daemon for the workspace chosen during setup. If Maurice
was configured to start from folders, run `maurice setup` and choose **global**,
or pass `--workspace` explicitly for a one-off global daemon.

`maurice web` follows the configured context level. Folder-first setups open a
browser chat for the current folder. Desktop-assistant setups use the configured
workspace for central state, but keep the folder where `maurice web` was launched
as the active project. Use `--dir` to force a folder context or `--workspace` to
force a workspace context while still keeping the launch folder as the active
project.

Maurice intentionally has one active project per chat window or session. The
active project is the folder used for relative paths, Git state, project memory,
and dev commands such as `/plan`, `/dev`, `/check`, `/review`, and `/commit`.
You can still open several terminals or browser chats at the same time: one
`maurice web` launched from `app-a` can work on `app-a`, while another launched
from `app-b` works on `app-b`. The limit is only inside one conversation: there
is one default project target at a time.

Each turn with an active project records that project in two event-driven
registries: the machine history `~/.maurice/projects.json`, and the current
agent history `<agent-workspace>/projects.json`. Maurice does not scan the disk
or treat every IDE-open folder as active. A remembered project becomes active
only when you launch Maurice from that folder or select it explicitly.

```bash
maurice start          # start daemon services and open the browser chat
maurice restart        # restart daemon services after config or code changes
maurice web            # foreground browser chat for configured context
maurice logs           # show recent events
maurice dashboard      # open dashboard
maurice doctor         # diagnose install/config/workspace health
maurice stop           # stop daemon services
```

Use `maurice start --no-browser` on headless machines or when you only want the
daemon.
When Telegram is enabled, Maurice syncs the available slash commands to the bot
menu with Telegram's `setMyCommands` API.

Use an explicit workspace only when you want to bypass the workspace configured
during setup:

```bash
maurice start --workspace /path/to/workspace
maurice restart --workspace /path/to/workspace
maurice web --workspace /path/to/workspace
```

## Project Commands

When `dev` is enabled, project commands work against the active context:

```text
/plan                 frame and write PLAN.md
/tasks                list open tasks
/dev                  execute the plan autonomously
/check                check project state
/review               review before validation
/commit               prepare a commit
```

In folder context, the project is the current folder. In desktop context,
`maurice web` also treats the launch folder as the active project, even when it
is outside the assistant workspace. Maurice still offers `/projects` and
`/project open <name>` for workspace-owned projects, remembered projects, and
older workflows.

## Uninstall

```bash
./uninstall.sh
./uninstall.sh --delete-workspace
./uninstall.sh --workspace /path/to/workspace --delete-workspace
```

`--delete-workspace` is intentionally separate because the global workspace can
be a broad folder such as `~/Documents`.

## More

Development and runtime internals are documented in [README_DEVS.md](README_DEVS.md).
