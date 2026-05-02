# Maurice Development

This document is for people changing Maurice itself: runtime code, host services,
system skills, tests, configuration contracts, and developer tooling.

For normal use, start with [README.md](README.md).

## Dev Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
pytest -q
```

## Runtime Shape

Maurice is a single agent runtime with two context anchors:

- `local`: folder-centered, transient server, state in `./.maurice`
- `global`: workspace-centered daemon services and state, with an optional
  active project root that may be outside the workspace

Both modes use the same agentic core: `MauriceContext`, `MauriceServer`,
`AgentLoop`, permissions, sessions, events, memory, and skills.

## Features

- typed runtime contracts
- unified local/global context model
- kernel/host/skills separation
- scoped permissions and approval records
- automatic context compaction
- append-only event log
- persisted named sessions
- strict skill loading with dynamic executor wiring

## Gateway

```bash
maurice gateway serve --workspace /path/to/ws
```

Local gateway listens on `http://127.0.0.1:18791` by default. When the global
daemon is running, gateway turns route through the same server; one-shot gateway
commands can still run directly.

## Install Testing

```bash
./uninstall.sh                    # remove ~/.maurice, autostart and repo-owned CLI link
./uninstall.sh --delete-workspace  # also remove the configured global workspace
./uninstall.sh --workspace /path/to/workspace --delete-workspace
```

The setup wizard hides the mock provider for normal users. To expose it for
tests or local runtime development:

```bash
maurice setup --show-mock
```

## Docs

- [docs/architecture.md](docs/architecture.md) — runtime, contexts, layouts
- [docs/turn-lifecycle.md](docs/turn-lifecycle.md) — what happens during a turn
- [docs/skills.md](docs/skills.md) — how to write a skill
- [docs/security.md](docs/security.md) — permission profiles, approval modes, shell parser
- [docs/config.md](docs/config.md) — config keys and defaults
