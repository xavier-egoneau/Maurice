from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maurice.host import project_registry
from maurice.host.project_registry import (
    list_known_projects,
    list_machine_projects,
    record_seen_project,
)


def test_record_seen_project_writes_machine_and_agent_registries(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    agent_workspace = tmp_path / "workspace" / "agents" / "main"
    project = tmp_path / "app"
    project.mkdir()

    record_seen_project(agent_workspace, project)

    assert list_known_projects(agent_workspace)[0]["path"] == str(project.resolve())
    assert list_machine_projects()[0]["path"] == str(project.resolve())


def test_project_registry_dedupes_canonical_paths_and_keeps_recent_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    agent_workspace = tmp_path / "workspace" / "agents" / "main"

    class FakeDateTime:
        counter = 0

        @classmethod
        def now(cls, tz):
            cls.counter += 1
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=cls.counter)

    monkeypatch.setattr(project_registry, "datetime", FakeDateTime)

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    record_seen_project(agent_workspace, duplicate)
    record_seen_project(agent_workspace, duplicate / ".")

    projects = list_known_projects(agent_workspace)
    assert len(projects) == 1
    assert projects[0]["path"] == str(duplicate.resolve())

    for index in range(55):
        project = tmp_path / f"project-{index:02d}"
        project.mkdir()
        record_seen_project(agent_workspace, project)

    projects = list_known_projects(agent_workspace)
    assert len(projects) == 50
    assert projects[0]["name"] == "project-54"
    assert projects[-1]["name"] == "project-05"
