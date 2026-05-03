"""Explore system skill — tree, grep, summary."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from maurice.kernel.contracts import ToolResult, TrustLabel


# ---------------------------------------------------------------------------
# noise filters

_IGNORED_DIRS = {
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".pnp",
    ".venv", "venv", "env", ".env",
    "dist", "build", "_build", "target",
    ".next", ".nuxt", ".svelte-kit",
    "coverage", ".coverage",
    ".idea", ".vscode",
}

_IGNORED_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib",
    ".egg-info", ".dist-info",
    ".lock",  # show lockfiles but not their content
}

_KEY_FILES = [
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "requirements-dev.txt", "Pipfile",
    # JS/TS
    "package.json", "tsconfig.json", "vite.config.ts", "vite.config.js",
    "next.config.js", "next.config.ts",
    # Rust
    "Cargo.toml",
    # Go
    "go.mod",
    # Ruby
    "Gemfile",
    # General
    "README.md", "README.rst", "README.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example",
]

_PROJECT_MARKERS: list[tuple[str, str]] = [
    ("pyproject.toml",        "Python"),
    ("setup.py",              "Python"),
    ("requirements.txt",      "Python"),
    ("package.json",          "JavaScript/TypeScript"),
    ("Cargo.toml",            "Rust"),
    ("go.mod",                "Go"),
    ("Gemfile",               "Ruby"),
    ("pom.xml",               "Java/Maven"),
    ("build.gradle",          "Java/Gradle"),
    ("composer.json",         "PHP"),
    ("*.csproj",              "C#"),
    ("CMakeLists.txt",        "C/C++"),
]


# ---------------------------------------------------------------------------
# build_executors

def build_executors(ctx: Any) -> dict[str, Any]:
    perm = ctx.permission_context
    return {
        "explore.tree":    lambda args: _tree(args, perm),
        "explore.grep":    lambda args: _grep(args, perm),
        "explore.summary": lambda args: _summary(args, perm),
    }


# ---------------------------------------------------------------------------
# explore.tree

def _tree(args: dict[str, Any], perm: Any) -> ToolResult:
    raw_path = str(args.get("path") or ".")
    depth = min(int(args.get("depth") or 3), 6)
    include_hidden = bool(args.get("include_hidden", False))

    root = _resolve(raw_path, perm)
    if root is None:
        return _err(f"Path not accessible: {raw_path}")
    if not root.exists():
        return _err(f"Path does not exist: {root}")
    if not root.is_dir():
        return _err(f"Not a directory: {root}")

    lines: list[str] = [_display_path(root, perm)]
    _walk_tree(root, lines, prefix="", depth=depth, current=0,
               include_hidden=include_hidden)
    text = "\n".join(lines)
    return ToolResult(
        ok=True,
        summary=text,
        data={"tree": text, "root": str(root)},
        trust=TrustLabel.LOCAL_MUTABLE,
        artifacts=[], events=[], error=None,
    )


def _walk_tree(
    directory: Path,
    lines: list[str],
    prefix: str,
    depth: int,
    current: int,
    include_hidden: bool,
) -> None:
    if current >= depth:
        return
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return

    entries = [
        e for e in entries
        if (include_hidden or not e.name.startswith("."))
        and e.name not in _IGNORED_DIRS
        and e.suffix not in _IGNORED_EXTENSIONS
    ]

    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        label = entry.name + ("/" if entry.is_dir() else "")
        lines.append(f"{prefix}{connector}{label}")
        if entry.is_dir():
            extension = "    " if is_last else "│   "
            _walk_tree(entry, lines, prefix + extension, depth, current + 1, include_hidden)


# ---------------------------------------------------------------------------
# explore.grep

def _grep(args: dict[str, Any], perm: Any) -> ToolResult:
    pattern = str(args.get("pattern") or "")
    raw_path = str(args.get("path") or ".")
    file_glob = str(args.get("file_pattern") or "*")
    max_results = min(int(args.get("max_results") or 40), 200)

    if not pattern:
        return _err("pattern is required")

    root = _resolve(raw_path, perm)
    if root is None:
        return _err(f"Path not accessible: {raw_path}")

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return _err(f"Invalid regex: {exc}")

    matches: list[dict[str, Any]] = []
    files_searched = 0

    candidates = [root] if root.is_file() else _iter_files(root, file_glob)
    for filepath in candidates:
        if len(matches) >= max_results:
            break
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_searched += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append({
                    "file": _display_path(filepath, perm),
                    "absolute_file": str(filepath),
                    "line": lineno,
                    "text": line.rstrip(),
                })
                if len(matches) >= max_results:
                    break

    if not matches:
        return ToolResult(
            ok=True,
            summary=f"No matches for '{pattern}' in {files_searched} files.",
            data={"matches": [], "files_searched": files_searched},
            trust=TrustLabel.LOCAL_MUTABLE,
            artifacts=[], events=[], error=None,
        )

    lines = [f"{m['file']}:{m['line']}: {m['text']}" for m in matches]
    full = "\n".join(lines)
    return ToolResult(
        ok=True,
        summary=full,
        data={"matches": matches, "files_searched": files_searched},
        trust=TrustLabel.LOCAL_MUTABLE,
        artifacts=[], events=[], error=None,
    )


def _iter_files(root: Path, glob: str) -> list[Path]:
    result = []
    for path in root.rglob(glob):
        if path.is_file():
            parts = set(path.parts)
            if parts & _IGNORED_DIRS:
                continue
            if path.suffix in _IGNORED_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue
            result.append(path)
    return sorted(result)


# ---------------------------------------------------------------------------
# explore.summary

def _summary(args: dict[str, Any], perm: Any) -> ToolResult:
    raw_path = str(args.get("path") or ".")
    include_project_memory = bool(args.get("include_project_memory", False))
    root = _resolve(raw_path, perm)
    if root is None:
        return _err(f"Path not accessible: {raw_path}")
    if not root.is_dir():
        return _err(f"Not a directory: {root}")

    sections: list[str] = [f"# Project: {root.name}", f"Path: {root}"]

    # --- project type detection ---
    project_type = _detect_type(root)
    if project_type:
        sections.append(f"Type: {project_type}")

    # --- top-level structure (1 level) ---
    try:
        top = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        top = [e for e in top if not e.name.startswith(".") and e.name not in _IGNORED_DIRS]
        sections.append("\n## Structure")
        for e in top[:30]:
            sections.append(f"  {'📁' if e.is_dir() else '📄'} {e.name}")
    except PermissionError:
        pass

    # --- key file contents ---
    sections.append("\n## Key files")
    found_any = False
    for name in _KEY_FILES:
        candidate = root / name
        if candidate.exists() and candidate.is_file():
            content = _read_truncated(candidate, max_chars=2000)
            sections.append(f"\n### {name}\n```\n{content}\n```")
            found_any = True
    if not found_any:
        sections.append("No standard config files found.")

    # --- .maurice/ context ---
    maurice = root / ".maurice"
    if maurice.is_dir():
        sections.append("\n## .maurice/ (project memory)")
        for name in ["AGENTS.md", "PLAN.md", "DECISIONS.md"]:
            f = maurice / name
            if f.exists():
                if include_project_memory:
                    content = _read_truncated(f, max_chars=1000)
                    sections.append(f"\n### {name}\n{content}")
                else:
                    sections.append(f"\n### {name}\n{_project_memory_brief(name, f)}")
            else:
                sections.append(f"\n### {name}: not created yet — run /plan to initialize.")

    text = "\n".join(sections)
    return ToolResult(
        ok=True,
        summary=text,
        data={"summary": text, "project_type": project_type, "root": str(root)},
        trust=TrustLabel.LOCAL_MUTABLE,
        artifacts=[], events=[], error=None,
    )


def _detect_type(root: Path) -> str | None:
    for marker, label in _PROJECT_MARKERS:
        if "*" in marker:
            if any(root.glob(marker)):
                return label
        elif (root / marker).exists():
            return label
    return None


def _read_truncated(path: Path, max_chars: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n... ({len(text) - max_chars} chars truncated)"
        return text
    except OSError:
        return "(unreadable)"


def _project_memory_brief(name: str, path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "exists, unreadable"
    if name == "PLAN.md":
        open_tasks = len(re.findall(r"(?m)^\s*-\s*\[\s\]\s+", text))
        done_tasks = len(re.findall(r"(?m)^\s*-\s*\[[xX]\]\s+", text))
        return (
            f"exists ({open_tasks} open task{'s' if open_tasks != 1 else ''}, "
            f"{done_tasks} done). Not included in general summaries; use `/plan show`, `/tasks`, or `/dev` for plan details."
        )
    if name == "DECISIONS.md":
        decisions = len(re.findall(r"(?m)^\s*-\s+\d{4}-\d{2}-\d{2}\s+-", text))
        return f"exists ({decisions} recorded decision{'s' if decisions != 1 else ''})."
    if name == "AGENTS.md":
        rules = len(re.findall(r"(?m)^\s*-\s+", text))
        return f"exists ({rules} local rule{'s' if rules != 1 else ''})."
    return "exists"


# ---------------------------------------------------------------------------
# helpers

def _resolve(raw: str, perm: Any) -> Path | None:
    """Resolve path relative to workspace/project root."""
    variables = perm.variables() if hasattr(perm, "variables") else {}
    p = Path(raw).expanduser()
    if not p.is_absolute():
        base = Path(variables.get("$project") or variables.get("$workspace") or ".")
        p = (base / p).resolve()
    else:
        p = p.resolve()
    allowed_roots = [
        Path(value).expanduser().resolve()
        for value in (variables.get("$workspace"), variables.get("$project"))
        if value
    ]
    return p if any(_is_relative_to(p, root) for root in allowed_roots) else None


def _display_path(path: Path, perm: Any) -> str:
    variables = perm.variables() if hasattr(perm, "variables") else {}
    roots = [
        Path(value).expanduser().resolve()
        for value in (variables.get("$project"), variables.get("$workspace"))
        if value
    ]
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        value = relative.as_posix()
        return value or "."
    return resolved.name if resolved.name else str(resolved)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _err(msg: str) -> ToolResult:
    from maurice.kernel.contracts import ToolError
    return ToolResult(
        ok=False,
        summary=msg,
        data=None,
        trust=TrustLabel.LOCAL_MUTABLE,
        artifacts=[], events=[],
        error=ToolError(code="explore_error", message=msg, retryable=False),
    )
