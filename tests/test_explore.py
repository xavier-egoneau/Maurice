"""Tests for system_skills/explore."""

from __future__ import annotations

from pathlib import Path

import pytest

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.explore.tools import (
    build_executors,
    _detect_type,
)


def _ctx(tmp_path: Path) -> PermissionContext:
    return PermissionContext(
        workspace_root=str(tmp_path),
        runtime_root=str(tmp_path),
        agent_workspace_root=str(tmp_path),
        active_project_root=str(tmp_path),
    )


def _exec(tmp_path: Path) -> dict:
    from maurice.kernel.skills import SkillContext
    ctx = SkillContext(
        permission_context=_ctx(tmp_path),
        agent_id="main",
    )
    return build_executors(ctx)


class TestExploreTree:
    def test_basic_tree(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x=1")
        (tmp_path / "README.md").write_text("hello")
        ex = _exec(tmp_path)
        result = ex["explore.tree"]({"path": str(tmp_path)})
        assert result.ok
        assert "src" in result.data["tree"]
        assert "main.py" in result.data["tree"]

    def test_ignores_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_text("")
        (tmp_path / "app.py").write_text("")
        ex = _exec(tmp_path)
        result = ex["explore.tree"]({"path": str(tmp_path)})
        assert "__pycache__" not in result.data["tree"]
        assert "app.py" in result.data["tree"]

    def test_depth_limit(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("")
        ex = _exec(tmp_path)
        result = ex["explore.tree"]({"path": str(tmp_path), "depth": 2})
        assert "deep.py" not in result.data["tree"]

    def test_nonexistent_path(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.tree"]({"path": str(tmp_path / "nope")})
        assert not result.ok

    def test_outside_workspace_blocked(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.tree"]({"path": "/etc"})
        assert not result.ok


class TestExploreGrep:
    def test_finds_pattern(self, tmp_path):
        (tmp_path / "app.py").write_text("def hello():\n    return 42\n")
        ex = _exec(tmp_path)
        result = ex["explore.grep"]({"pattern": "def hello", "path": str(tmp_path)})
        assert result.ok
        assert result.data["matches"]
        assert "app.py" in result.data["matches"][0]["file"]

    def test_no_match(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1")
        ex = _exec(tmp_path)
        result = ex["explore.grep"]({"pattern": "zzz_not_found", "path": str(tmp_path)})
        assert result.ok
        assert result.data["matches"] == []

    def test_file_pattern_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("target = 1")
        (tmp_path / "b.txt").write_text("target = 2")
        ex = _exec(tmp_path)
        result = ex["explore.grep"]({"pattern": "target", "path": str(tmp_path), "file_pattern": "*.py"})
        assert result.ok
        files = [m["file"] for m in result.data["matches"]]
        assert all(f.endswith(".py") for f in files)

    def test_invalid_regex(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.grep"]({"pattern": "[invalid", "path": str(tmp_path)})
        assert not result.ok

    def test_outside_workspace_blocked(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.grep"]({"pattern": "root", "path": "/etc"})
        assert not result.ok


class TestExploreSummary:
    def test_detects_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'myapp'\n")
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert result.data["project_type"] == "Python"
        assert "pyproject.toml" in result.data["summary"]

    def test_detects_node_project(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "myapp", "version": "1.0.0"}')
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert result.data["project_type"] == "JavaScript/TypeScript"

    def test_includes_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project\nA great project.")
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert "My Project" in result.data["summary"]

    def test_reports_missing_maurice_files(self, tmp_path):
        (tmp_path / ".maurice").mkdir()
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert "not created yet" in result.data["summary"]

    def test_reports_existing_plan(self, tmp_path):
        (tmp_path / ".maurice").mkdir()
        (tmp_path / ".maurice" / "PLAN.md").write_text("# Plan\n- [ ] Do something")
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert "Do something" in result.data["summary"]

    def test_unknown_type(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": str(tmp_path)})
        assert result.ok
        assert result.data["project_type"] is None

    def test_outside_workspace_blocked(self, tmp_path):
        ex = _exec(tmp_path)
        result = ex["explore.summary"]({"path": "/etc"})
        assert not result.ok


class TestDetectType:
    def test_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert _detect_type(tmp_path) == "Python"

    def test_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert _detect_type(tmp_path) == "JavaScript/TypeScript"

    def test_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")
        assert _detect_type(tmp_path) == "Rust"

    def test_unknown(self, tmp_path):
        assert _detect_type(tmp_path) is None
