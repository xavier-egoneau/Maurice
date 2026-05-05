# Automations

Maurice automations are scheduler jobs stored per agent. They are only created
from explicit configuration and loaded skills; Maurice does not run background
dreaming or daily work just because a chat window is open.

## When They Run

The scheduler runs in the persistent assistant surface:

- `maurice start` starts the scheduler by default, unless `--no-scheduler` is
  passed;
- `maurice scheduler serve --workspace <workspace>` runs only the scheduler in
  the foreground;
- `maurice scheduler run-once --workspace <workspace>` runs due jobs once, which
  is useful for tests and manual diagnosis.

Folder/local usage can use the same runtime and commands, but there is no
always-on daemon unless the user starts one explicitly.

## Default Jobs

Defaults live in `kernel.scheduler`:

```yaml
kernel:
  scheduler:
    enabled: true
    dreaming_enabled: true
    dreaming_time: "09:00"
    daily_enabled: true
    daily_time: "09:30"
    sentinelle_enabled: true
    sentinelle_time: "09:10"
```

At scheduler startup, Maurice ensures these recurring jobs for each served
agent:

| Job | Skill required | Default time | Storage |
|---|---|---|---|
| `dreaming.run` | `dreaming` | `09:00` local time | `<workspace>/agents/<agent-id>/jobs.json` |
| `sentinelle.scan` | `sentinelle` | `09:10` local time | `<workspace>/agents/<agent-id>/jobs.json` |
| `daily.digest` | `daily` | `09:30` local time | `<workspace>/agents/<agent-id>/jobs.json` |

Skill-owned jobs such as `sentinelle.scan` come from the skill's `setup.json`.
Their user-facing variables are stored in `<workspace>/skills.yaml`, not as
hard-coded scheduler fields.

Jobs repeat every 24 hours. If the configured time has already passed today, the
next run is scheduled for tomorrow. Times are interpreted in the machine's local
timezone and stored internally as UTC run times.

If an agent has an explicit `skills` list, the job is created only when that list
contains the corresponding skill. For example, disabling `daily` for an agent
also removes that agent's scheduled daily digest. If the agent has no explicit
skill list, the workspace `kernel.skills` list is used; an empty workspace list
means all available skills are considered enabled.

## Configuration Commands

In Maurice web, open **Agent**, then adjust **Automatismes**. This writes the
same `kernel.scheduler` values as the CLI.

Use the CLI when you want to script or diagnose the same settings:

```bash
maurice scheduler configure \
  --workspace /path/to/workspace \
  --dream-time 08:45 \
  --sentinelle-time 09:10 \
  --daily-time 09:15

maurice scheduler configure --workspace /path/to/workspace --disable-dreaming
maurice scheduler configure --workspace /path/to/workspace --enable-sentinelle
maurice scheduler configure --workspace /path/to/workspace --enable-daily
```

Supported time formats include `09:30`, `9h`, and `9h30`.

Use `maurice doctor` to validate config and `maurice logs` to inspect recent
scheduler failures. Agents with the `admin`/`host` skill can use `host.doctor`
and `host.logs` for the same diagnostics from chat.

## Dreaming

`dreaming.run` is a background synthesis pass for one agent.

It gathers dream inputs from loaded skills:

- a skill's `dreams.md` explains what the skill can surface;
- a skill can expose `tools.build_dream_input` for deterministic signals;
- the current agent memory is included by default when the `memory` skill is
  available;
- project signals come from active/known projects with `.maurice/` project
  memory when the `dev` skill contributes them.

The output is written under:

```text
<workspace>/agents/<agent-id>/dreams/dream_<id>.json
```

Dreaming does not directly message the user. It prepares structured context,
ideas, warnings, and candidate actions for later daily synthesis or manual
inspection.

## Daily

`daily.digest` builds the user-facing morning digest for one agent.

It uses:

- the latest dream report from `<workspace>/agents/<agent-id>/dreams/`;
- `daily.md` fragments from currently loaded skills;
- delivery settings for that agent/channel.

The daily is delivered into the agent's `daily` session and to configured
Telegram recipients when Telegram is enabled for that agent. If no recent dream
exists, the daily can still produce a minimal digest, but the intended flow is:

```text
dreaming.run -> dream report -> daily.digest -> delivered daily
```

## Skill Contributions

For a user skill:

- `dreams.md` answers: what should dreaming notice or connect for this skill?
- `daily.md` answers: what should the morning digest include or ignore?
- `tools.py` is optional and should expose deterministic data only when needed.

Keep these files as instructions, not as prose to paste directly to the user.
