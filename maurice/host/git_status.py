"""Read-only Git status helpers for host UI surfaces."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def git_changes(root: str | Path, *, max_files: int = 200) -> dict[str, Any]:
    """Return a compact read-only view of the working tree."""

    base = Path(root).expanduser().resolve()
    git_root = _git_root(base)
    if git_root is None:
        return {
            "ok": True,
            "available": False,
            "root": str(base),
            "files": [],
            "total_files": 0,
            "insertions": 0,
            "deletions": 0,
            "summary": "No Git repository detected.",
        }

    status = _run_git(git_root, "status", "--porcelain=v1", "-z")
    if status.returncode != 0:
        return _error(git_root, status.stderr.strip() or "git status failed")

    stat = _run_git(git_root, "diff", "--numstat", "HEAD", "--")
    stat_by_path = _parse_numstat(stat.stdout if stat.returncode == 0 else "")
    files = _parse_porcelain_z(status.stdout, stat_by_path)
    total_insertions = sum(file["insertions"] for file in files)
    total_deletions = sum(file["deletions"] for file in files)
    return {
        "ok": True,
        "available": True,
        "root": str(git_root),
        "files": files[:max_files],
        "total_files": len(files),
        "insertions": total_insertions,
        "deletions": total_deletions,
        "truncated": len(files) > max_files,
        "summary": _summary(len(files), total_insertions, total_deletions),
    }


def git_diff(root: str | Path, file_path: str, *, max_chars: int = 40_000) -> dict[str, Any]:
    """Return a bounded diff for one changed file."""

    base = Path(root).expanduser().resolve()
    git_root = _git_root(base)
    if git_root is None:
        return {"ok": False, "error": "not_git_repository", "diff": ""}
    path = _safe_relative_path(file_path)
    if path is None:
        return {"ok": False, "error": "invalid_path", "diff": ""}
    result = _run_git(git_root, "diff", "--", path)
    diff = result.stdout if result.returncode == 0 else ""
    if not diff:
        staged = _run_git(git_root, "diff", "--cached", "--", path)
        diff = staged.stdout if staged.returncode == 0 else ""
    if not diff and (git_root / path).is_file():
        content = (git_root / path).read_text(encoding="utf-8", errors="replace")
        diff = "\n".join(f"+{line}" for line in content.splitlines())
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars].rstrip() + "\n... diff truncated ..."
    return {
        "ok": True,
        "root": str(git_root),
        "path": path,
        "diff": diff,
        "truncated": truncated,
    }


def _git_root(path: Path) -> Path | None:
    result = _run_git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _parse_numstat(output: str) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        insertions = 0 if parts[0] == "-" else int(parts[0] or 0)
        deletions = 0 if parts[1] == "-" else int(parts[1] or 0)
        path = parts[-1]
        stats[path] = (insertions, deletions)
    return stats


def _parse_porcelain_z(output: str, stats: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    entries = [entry for entry in output.split("\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        code = entry[:2]
        path = entry[3:]
        if code.startswith("R") or code.startswith("C"):
            index += 1
            if index < len(entries):
                path = entries[index]
        insertions, deletions = stats.get(path, (0, 0))
        files.append(
            {
                "path": path,
                "status": code,
                "label": _status_label(code),
                "insertions": insertions,
                "deletions": deletions,
            }
        )
        index += 1
    return files


def _status_label(code: str) -> str:
    if code == "??":
        return "untracked"
    if "D" in code:
        return "deleted"
    if "R" in code:
        return "renamed"
    if "A" in code:
        return "added"
    if "M" in code:
        return "modified"
    return code.strip() or "changed"


def _safe_relative_path(value: str) -> str | None:
    path = str(value or "").strip()
    if not path or path.startswith("/") or "\\" in path:
        return None
    if any(part == ".." for part in Path(path).parts):
        return None
    if re.match(r"^[A-Za-z]:", path):
        return None
    return path


def _summary(total: int, insertions: int, deletions: int) -> str:
    if total == 0:
        return "No Git changes."
    return f"{total} file{'s' if total != 1 else ''} changed +{insertions} -{deletions}"


def _error(root: Path, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "available": False,
        "root": str(root),
        "files": [],
        "total_files": 0,
        "insertions": 0,
        "deletions": 0,
        "summary": "Git status unavailable.",
        "error": message,
    }
