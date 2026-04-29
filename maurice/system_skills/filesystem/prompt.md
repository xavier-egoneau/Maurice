Use filesystem tools only for explicit file tasks inside approved scopes.
Relative paths resolve inside the active project when one is open, otherwise inside the current agent content folder.

When an active project is open, `.` means "the current project folder".
If the user names the active project itself, do not append that name again under the project path.
Example: if the active project is `test`, resolve "le dossier test" to `$project`, not `$project/test`.

Translate filesystem tool results into natural user-facing language.
Do not expose raw English summaries like "Listed 1 entries" or absolute paths unless the user asks for paths.

Use `filesystem.move` when the user asks to move, rename, restore, or put a file/folder back in another place.
