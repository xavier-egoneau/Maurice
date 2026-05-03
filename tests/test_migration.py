from __future__ import annotations

import json

from maurice.host.migration import inspect_jarvis_workspace, migrate_jarvis_workspace
from maurice.host.workspace import initialize_workspace
from maurice.system_skills.memory.tools import search
from maurice.kernel.permissions import PermissionContext


def make_jarvis_workspace(tmp_path):
    jarvis = tmp_path / "jarvis"
    skill = jarvis / "skills" / "weather"
    skill.mkdir(parents=True)
    (skill / "skill.yaml").write_text(
        """
name: weather
version: 0.1.0
origin: user
mutable: true
description: Weather skill.
config_namespace: skills.weather
requires:
  binaries: []
  credentials: []
dependencies:
  skills: []
  optional_skills: []
permissions: []
tools: []
backend: null
storage: null
dreams:
  attachment: dreams.md
events:
  state_publisher: null
""",
        encoding="utf-8",
    )
    (jarvis / "skills" / "broken").mkdir()
    (jarvis / "memory_export.json").write_text(
        json.dumps({"memories": [{"content": "Jarvis memory", "tags": ["legacy"]}]}),
        encoding="utf-8",
    )
    (jarvis / "artifacts").mkdir()
    (jarvis / "artifacts" / "note.txt").write_text("artifact", encoding="utf-8")
    (jarvis / "credentials.yaml").write_text("token: secret", encoding="utf-8")
    (jarvis / "config").mkdir()
    (jarvis / "sessions").mkdir()
    return jarvis


def test_inspect_jarvis_workspace_reports_candidates_and_exclusions(tmp_path) -> None:
    jarvis = make_jarvis_workspace(tmp_path)

    report = inspect_jarvis_workspace(jarvis)

    assert any(item.kind == "skill" and item.status == "candidate" for item in report.items)
    assert any(item.kind == "skill" and item.status == "skipped" for item in report.items)
    assert any(item.kind == "memory" and item.status == "candidate" for item in report.items)
    assert any(item.kind == "credential" and item.status == "skipped" for item in report.items)
    assert any(item.kind == "config" and item.status == "excluded" for item in report.items)
    assert any(item.kind == "session" and item.status == "excluded" for item in report.items)


def test_migration_dry_run_does_not_write(tmp_path) -> None:
    jarvis = make_jarvis_workspace(tmp_path)
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)

    report = migrate_jarvis_workspace(jarvis, workspace, dry_run=True, include_artifacts=True)

    assert any(item.status == "dry_run" for item in report.items)
    assert not (workspace / "skills" / "weather").exists()
    assert not (workspace / "agents" / "main" / "content" / "jarvis" / "note.txt").exists()
    assert not (
        workspace / "agents" / "main" / "content" / "migrations" / "jarvis_migration_report.json"
    ).exists()


def test_migration_copies_compatible_data_with_provenance(tmp_path) -> None:
    jarvis = make_jarvis_workspace(tmp_path)
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)

    report = migrate_jarvis_workspace(jarvis, workspace, dry_run=False, include_artifacts=True)

    assert report.migrated_count >= 3
    assert (workspace / "skills" / "weather" / "skill.yaml").is_file()
    assert (workspace / "skills" / "weather" / ".maurice_migration.json").is_file()
    assert (workspace / "agents" / "main" / "content" / "jarvis" / "note.txt").is_file()
    assert (
        workspace / "agents" / "main" / "content" / "migrations" / "jarvis_migration_report.json"
    ).is_file()

    context = PermissionContext(
        workspace_root=str(workspace),
        runtime_root=str(runtime),
        agent_workspace_root=str(workspace / "agents" / "main"),
    )
    memories = search({"query": "Jarvis"}, context)
    assert memories.data["memories"][0]["content"] == "Jarvis memory"
    assert "jarvis_migration" in memories.data["memories"][0]["tags"]
