# Maurice

A modular, persistent AI agent runtime with a small inspectable kernel.

- typed runtime contracts
- kernel/host/skills separation
- scoped permissions and approval records
- automatic context compaction
- append-only event log
- persisted sessions
- strict skill loading with dynamic executor wiring

## Requirements

- Python 3.12+

## Install

```bash
./install.sh
```

Checks Python, installs the package, links the `maurice` CLI, and runs onboarding.

## Quick start

```bash
maurice start          # start daemon
maurice logs          # tail logs
maurice dashboard     # open dashboard
make stop             # stop daemon
```

Run one turn directly:

```bash
maurice run --message "salut Maurice"
```

Override workspace:

```bash
WORKSPACE=/path/to/ws ./install.sh
maurice run --workspace /path/to/ws --message "salut"
```

## Dev install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
pytest -q
```

## Docs

- [docs/architecture.md](docs/architecture.md) — layer model, module map, physical layout
- [docs/turn-lifecycle.md](docs/turn-lifecycle.md) — what happens during a turn
- [docs/skills.md](docs/skills.md) — how to write a skill
- [docs/security.md](docs/security.md) — permission profiles, approval modes, shell parser
- [docs/config.md](docs/config.md) — all config keys and defaults

## Gateway

```bash
make gateway          # local HTTP + Telegram polling
make telegram         # Telegram polling only
```

Local gateway listens on `http://127.0.0.1:18791` by default.

## Dev skill

When `dev` is enabled, project commands work in agent context:

```text
/projects             list projects
/project open <name>  open or create a project
/plan                 frame and write PLAN.md
/tasks                list open tasks
/dev                  execute the plan autonomously
/check                check project state
/review               review before validation
/commit               prepare a commit
```

Project files live in `agents/<agent>/content/<project>/.maurice/`.
