"""Project root resolution and .maurice/ directory management."""

from __future__ import annotations

from pathlib import Path


MAURICE_DIR = ".maurice"
GITIGNORE_CONTENT = "*\n"

_KNOWN_FILES = ["MEMORY.md", "PLAN.md", "DECISIONS.md", "AGENTS.md"]


def find_project_root(cwd: Path) -> Path | None:
    """Walk up from cwd looking for a .git directory. Returns the git root or None."""
    current = cwd.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def resolve_project_root(cwd: Path, *, confirm: bool = True) -> Path | None:
    """Return the project root to use for .maurice/.

    Priority:
    1. Git root if found by walking up from cwd
    2. cwd if .maurice/ already exists there (user already confirmed before)
    3. cwd itself, after user confirmation (when confirm=True) or unconditionally

    Returns None if the user declined.
    """
    git_root = find_project_root(cwd)
    if git_root is not None:
        return git_root
    # .maurice/ already exists — no need to ask again
    if (cwd / MAURICE_DIR).exists():
        return cwd
    if not confirm:
        return cwd
    answer = input(f"No git repository found. Create .maurice/ here?\n  {cwd} [y/N] ").strip().lower()
    if answer in {"y", "yes", "o", "oui"}:
        return cwd
    return None


def ensure_maurice_dir(project_root: Path) -> Path:
    """Create .maurice/ with a blanket .gitignore if it doesn't exist yet."""
    maurice = project_root / MAURICE_DIR
    maurice.mkdir(exist_ok=True)
    gitignore = maurice / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")
    return maurice


def maurice_dir(project_root: Path) -> Path:
    return project_root / MAURICE_DIR


def sessions_dir(project_root: Path) -> Path:
    return maurice_dir(project_root) / "sessions"


def events_path(project_root: Path) -> Path:
    return maurice_dir(project_root) / "events.jsonl"


def approvals_path(project_root: Path) -> Path:
    return maurice_dir(project_root) / "approvals.json"


def config_path(project_root: Path) -> Path:
    return maurice_dir(project_root) / "config.yaml"


def global_config_path() -> Path:
    import os
    return Path(os.environ.get("MAURICE_HOME", Path.home() / ".maurice")) / "config.yaml"


def global_memory_path() -> Path:
    import os
    return Path(os.environ.get("MAURICE_HOME", Path.home() / ".maurice")) / "MEMORY.md"


def ensure_project_scaffold(project_root: Path) -> None:
    """Create .maurice/ scaffold files if they don't exist yet."""
    from maurice.system_skills.dev.commands import _ensure_project_files
    _ensure_project_files(project_root)
