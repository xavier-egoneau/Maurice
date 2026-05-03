Use skill authoring tools to create user skills under the workspace. Never modify system skills directly.

Design user skills as autonomous, shareable packages. In the future Maurice will
have a skill store, so a skill must not depend on hidden setup from the author's
machine. If it needs binaries, credentials, config files, services, caches, or
setup commands, document that in `skill.md` and provide a concrete diagnostic or
setup path inside the skill folder.

Default Maurice user skills are lightweight folders:

```text
<workspace>/skills/<skill_name>/
  skill.md   # frontmatter + main instructions
  dreams.md  # what the dreaming pass should notice or propose
  daily.md   # what the morning digest should include or ignore
```

Do not create `skill.yaml` for ordinary user skills. Maurice infers the runtime
manifest from `skill.md`, `dreams.md`, and `daily.md`.

When the skill needs code, put it inside the skill folder:

```text
<workspace>/skills/<skill_name>/
  tools.py
```

If `tools.py` defines `build_dream_input(context, *, config=None,
all_skill_configs=None)`, Maurice automatically wires it as this skill's dream
input builder. Use this for deterministic reads of local data, APIs, caches, or
files that should become dream signals.

If the skill needs callable chat tools, `tools.py` must also define
`tool_declarations()` returning Maurice tool declarations, and
`build_executors(ctx)` returning the matching executor functions. Use
`integration.read` for configured local integrations such as calendars instead
of pretending they are workspace file reads. Use `integration.write` for
maintenance or sync actions that update an integration cache, config, or remote
state.

For larger code, keep `tools.py` as the entry point and place helper modules or
small packages under the same skill folder. Keep instructions in `skill.md`;
keep generated data and caches outside the skill unless the user explicitly asks
for bundled fixtures.

Autonomous skill checklist:

- name required binaries and how to install them on common systems
- name expected Maurice credentials and how to request/capture them
- explain where generated local config belongs
- provide a doctor/check command when setup can fail
- avoid storing secrets in generated config; prefer Maurice credentials or OS keyrings
- include validation commands so Maurice can prove the skill is ready

Use `skills.create` with `with_code: true` only when a coded hook is needed.
Otherwise leave code files out.
