# Shell execution

Use `shell.exec` when a task needs a local command and no narrower skill tool
fits: tests, build commands, package manager diagnostics, project scripts,
calendar maintenance commands, and simple inspection commands.

Keep commands scoped to the active project or the Maurice workspace. Set `cwd`
to the active project when working on a project; omit it only when the project
root is the intended directory. Do not use shell commands to read secrets, edit
operating system files, bypass Maurice permissions, or send local data over the
network. Prefer specific skills such as filesystem, web, calendar, or host when
they exist.

If a command is destructive, touches credentials, installs software, uses
privilege escalation, modifies system locations, or uploads data, explain the
reason and expect an approval request or a policy block.
