from __future__ import annotations

from datetime import UTC, datetime

from maurice.kernel.contracts import DreamInput
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillContext, SkillLoader, SkillRoot
from maurice.system_skills.dreaming.tools import build_executors, run
from maurice.system_skills.memory.tools import build_dream_input, remember


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def registry(enabled_skills):
    return SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=enabled_skills,
    ).load()


def test_dreaming_run_degrades_without_memory_input(tmp_path) -> None:
    permission_context = context(tmp_path)
    result = run({}, permission_context, registry(["dreaming"]))

    assert result.ok
    assert result.data["report"]["inputs"] == []
    assert result.data["report"]["summary"] == "Dream completed with no skill inputs."
    assert (tmp_path / "workspace" / "agents" / "main" / "dreams").is_dir()


def test_dreaming_run_consumes_memory_dream_input(tmp_path) -> None:
    permission_context = context(tmp_path)
    remembered = remember(
        {"content": "Review project roadmap later.", "tags": ["project"]},
        permission_context,
    )
    events = EventStore(tmp_path / "events.jsonl")

    result = run(
        {"skills": ["memory"], "max_signals": 5},
        permission_context,
        registry(["dreaming", "memory"]),
        event_store=events,
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    report = result.data["report"]

    assert result.ok
    assert report["inputs"][0]["skill"] == "memory"
    assert report["inputs"][0]["signals"][0]["data"]["memory"]["id"] == remembered.data["id"]
    assert result.data["attachments"]["memory"]
    assert [event.name for event in events.read_all()] == [
        "dream.started",
        "dream.completed",
    ]


def test_dreaming_discovery_passes_runtime_skill_config(tmp_path, monkeypatch) -> None:
    permission_context = context(tmp_path)
    skill_registry = registry(["dreaming", "veille", "web"])

    def fake_veille_input(context_arg, *, config=None, all_skill_configs=None):
        assert context_arg == permission_context
        assert config == {}
        assert all_skill_configs["web"]["base_url"] == "http://search.test"
        return DreamInput(
            skill="veille",
            trust="local_mutable",
            freshness={"generated_at": datetime.now(UTC), "expires_at": None},
            signals=[],
        )

    monkeypatch.setattr("maurice.system_skills.veille.tools.build_dream_input", fake_veille_input)
    executors = build_executors(
        SkillContext(
            permission_context=permission_context,
            registry=skill_registry,
            all_skill_configs={"web": {"base_url": "http://search.test"}},
        )
    )

    result = executors["dreaming.run"]({"skills": ["veille"]})

    assert result.ok
    assert result.data["report"]["inputs"][0]["skill"] == "veille"


def test_dreaming_discovers_user_skill_relative_input_builder(tmp_path) -> None:
    permission_context = context(tmp_path)
    skill_dir = tmp_path / "workspace" / "skills" / "calendar"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: calendar
version: 0.1.0
origin: user
mutable: true
description: Calendar test skill.
config_namespace: skills.calendar
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
  input_builder: tools.build_dream_input
daily:
  attachment: daily.md
events:
  state_publisher: null
""",
        encoding="utf-8",
    )
    (skill_dir / "dreams.md").write_text("Calendar dream instructions.\n", encoding="utf-8")
    (skill_dir / "daily.md").write_text("Calendar daily instructions.\n", encoding="utf-8")
    (skill_dir / "tools.py").write_text(
        """
from datetime import UTC, datetime
from maurice.kernel.contracts import DreamInput

def build_dream_input(context, *, config=None, all_skill_configs=None):
    return DreamInput(
        skill="calendar",
        trust="local_mutable",
        freshness={"generated_at": datetime.now(UTC), "expires_at": None},
        signals=[{"id": "calendar_today", "type": "calendar", "summary": "One event today.", "data": {}}],
    )
""",
        encoding="utf-8",
    )
    skill_registry = SkillLoader(
        [
            SkillRoot(path="maurice/system_skills", origin="system", mutable=False),
            SkillRoot(path=str(tmp_path / "workspace" / "skills"), origin="user", mutable=True),
        ],
        enabled_skills=["dreaming", "calendar"],
    ).load()

    executors = build_executors(SkillContext(permission_context=permission_context, registry=skill_registry))
    result = executors["dreaming.run"]({"skills": ["calendar"]})

    assert result.ok
    assert result.data["attachments"]["calendar"] == "Calendar dream instructions.\n"
    assert result.data["report"]["inputs"][0]["signals"][0]["summary"] == "One event today."


def test_dreaming_run_includes_agent_memory_by_default(tmp_path) -> None:
    permission_context = context(tmp_path)
    remembered = remember(
        {"content": "Agent memory must stay in dream context.", "tags": ["dreaming"]},
        permission_context,
    )

    result = run(
        {"skills": ["reminders"], "max_signals": 5},
        permission_context,
        registry(["dreaming", "memory", "reminders"]),
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    inputs = result.data["report"]["inputs"]
    assert result.ok
    assert [item["skill"] for item in inputs] == ["memory"]
    assert inputs[0]["signals"][0]["data"]["memory"]["id"] == remembered.data["id"]


def test_dreaming_run_can_explicitly_skip_agent_memory(tmp_path) -> None:
    permission_context = context(tmp_path)
    remember(
        {"content": "Diagnostic run can skip memory.", "tags": ["dreaming"]},
        permission_context,
    )

    result = run(
        {"skills": ["reminders"], "include_memory": False},
        permission_context,
        registry(["dreaming", "memory", "reminders"]),
        dream_input_builders={"memory": lambda: build_dream_input(permission_context)},
    )

    assert result.ok
    assert result.data["report"]["inputs"] == []


def test_dreaming_run_validates_arguments(tmp_path) -> None:
    result = run(
        {"skills": "memory"},
        context(tmp_path),
        registry(["dreaming", "memory"]),
    )

    assert not result.ok
    assert result.error.code == "invalid_arguments"


def test_dreaming_run_validates_include_memory(tmp_path) -> None:
    result = run(
        {"include_memory": "yes"},
        context(tmp_path),
        registry(["dreaming", "memory"]),
    )

    assert not result.ok
    assert result.error.code == "invalid_arguments"
