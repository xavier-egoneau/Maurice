from __future__ import annotations

import subprocess

from maurice.host.git_status import git_changes, git_diff


def _git(path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def test_git_changes_reports_modified_files(tmp_path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    readme = tmp_path / "README.md"
    readme.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "init")

    readme.write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("draft\n", encoding="utf-8")

    changes = git_changes(tmp_path)

    assert changes["available"] is True
    assert changes["total_files"] == 2
    assert changes["insertions"] >= 1
    paths = {file["path"] for file in changes["files"]}
    assert paths == {"README.md", "notes.md"}


def test_git_diff_returns_file_diff(tmp_path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    readme = tmp_path / "README.md"
    readme.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "init")
    readme.write_text("one\ntwo\n", encoding="utf-8")

    result = git_diff(tmp_path, "README.md")

    assert result["ok"] is True
    assert result["path"] == "README.md"
    assert "+two" in result["diff"]


def test_git_diff_rejects_paths_outside_repo(tmp_path) -> None:
    result = git_diff(tmp_path, "../secret.txt")

    assert result["ok"] is False
    assert result["error"] in {"not_git_repository", "invalid_path"}
