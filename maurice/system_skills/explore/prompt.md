When asked to understand, explore, or take stock of a project:

1. Call `explore.summary` first — it reads key files and returns the project type,
   structure, dependencies, and `.maurice/` state in one call. Do not use
   `filesystem.list` + multiple `filesystem.read` calls when `explore.summary`
   covers the same ground faster.

2. Use `explore.tree` to visualize directory structure at any depth. Prefer it
   over `filesystem.list` when you need a multi-level view.

3. Use `explore.grep` to locate a symbol, function, class, or any text pattern
   across the codebase. Prefer it over manually reading files one by one.

When you receive `explore.summary` output:
- Report what you found: project type, main dependencies, structure.
- Note which `.maurice/` files exist and which don't.
- If PLAN.md is missing, suggest `/plan` to initialize project memory.
- Do not list raw file names — synthesize and explain.
