from __future__ import annotations

import pytest

from maurice.kernel.events import EventStore
from maurice.kernel.skills import (
    RequiredSkillError,
    SkillLoadError,
    SkillLoader,
    SkillRoot,
    SkillState,
)


def write_skill(root, name: str, body: str, prompt: str = "", dreams: str = ""):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(body, encoding="utf-8")
    if prompt:
        (skill_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    if dreams:
        (skill_dir / "dreams.md").write_text(dreams, encoding="utf-8")
    return skill_dir


def minimal_manifest(name: str, *, extra: str = "") -> str:
    return f"""
name: {name}
version: 0.1.0
origin: user
mutable: true
description: Test skill.
config_namespace: skills.{name}
requires:
  binaries: []
  credentials: []
dependencies:
  skills: []
  optional_skills: []
permissions: []
tools:
  - name: {name}.echo
    description: Echo input.
    input_schema: {{}}
    permission_class: fs.read
    permission_scope: {{}}
    trust:
      input: local_mutable
      output: local_mutable
    executor: tests.{name}.echo
backend: null
storage: null
dreams:
  attachment: dreams.md
events:
  state_publisher: null
{extra}
"""


def test_loader_registers_skill_commands(tmp_path) -> None:
    write_skill(
        tmp_path,
        "dev",
        minimal_manifest(
            "dev",
            extra="""
commands:
  - name: /plan
    description: Prepare project plan.
    handler: tests.dev.plan
    renderer: markdown
    aliases:
      - /dev
""",
        ),
    )

    registry = SkillLoader(
        [SkillRoot(path=str(tmp_path), origin="user", mutable=True)],
        enabled_skills=["dev"],
    ).load()

    assert registry.skills["dev"].state == SkillState.LOADED
    assert registry.commands["/plan"].owner_skill == "dev"
    assert registry.commands["/plan"].description == "Prepare project plan."
    assert registry.commands["/dev"].name == "/plan"


def test_loader_loads_system_filesystem_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["filesystem"],
    ).load()

    skill = registry.skills["filesystem"]

    assert skill.state == SkillState.LOADED
    assert "filesystem.read" in registry.tools
    assert "filesystem tools" in skill.prompt
    assert registry.tools["filesystem.write"].permission.permission_class == "fs.write"


def test_loader_loads_system_memory_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["memory"],
    ).load()

    skill = registry.skills["memory"]

    assert skill.state == SkillState.LOADED
    assert "memory.remember" in registry.tools
    assert "durable facts" in skill.prompt
    assert registry.tools["memory.remember"].permission.permission_class == "fs.write"


def test_loader_loads_system_dreaming_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["dreaming"],
    ).load()

    skill = registry.skills["dreaming"]

    assert skill.state == SkillState.LOADED
    assert "dreaming.run" in registry.tools
    assert "structured reviews" in skill.prompt
    assert registry.tools["dreaming.run"].permission.permission_class == "fs.write"


def test_loader_loads_system_web_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["web"],
    ).load()

    skill = registry.skills["web"]

    assert skill.state == SkillState.LOADED
    assert "web.fetch" in registry.tools
    assert "external untrusted" in skill.prompt
    assert registry.tools["web.fetch"].permission.permission_class == "network.outbound"


def test_loader_loads_system_host_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["host"],
    ).load()

    skill = registry.skills["host"]

    assert skill.state == SkillState.LOADED
    assert "host.status" in registry.tools
    assert "runtime status" in skill.prompt
    assert "Ask exactly one question at a time" in skill.prompt
    assert "`filesystem` : lire/ecrire des fichiers" in skill.prompt
    assert "`self_update` : proposer des ameliorations" in skill.prompt
    assert "Envoie-moi maintenant le token BotFather" in skill.prompt
    assert "Quels ids Telegram" in skill.prompt
    assert registry.tools["host.status"].permission.permission_class == "host.control"
    assert "/add_agent" in registry.commands
    assert "/edit_agent" in registry.commands
    assert registry.commands["/add_agent"].owner_skill == "host"


def test_loader_loads_system_reminders_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["reminders"],
    ).load()

    skill = registry.skills["reminders"]

    assert skill.state == SkillState.LOADED
    assert "reminders.create" in registry.tools
    assert "scheduled reminders" in skill.prompt
    assert registry.tools["reminders.create"].permission.permission_class == "fs.write"


def test_loader_loads_system_vision_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["vision"],
    ).load()

    skill = registry.skills["vision"]

    assert skill.state == SkillState.LOADED
    assert "vision.inspect" in registry.tools
    assert "image-derived" in skill.prompt
    assert registry.tools["vision.inspect"].permission.permission_class == "fs.read"


def test_loader_loads_system_skill_authoring_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["skills"],
    ).load()

    skill = registry.skills["skills"]

    assert skill.state == SkillState.LOADED
    assert "skills.create" in registry.tools
    assert "workspace" in skill.prompt
    assert registry.tools["skills.create"].permission.permission_class == "fs.write"


def test_loader_loads_system_self_update_skill() -> None:
    registry = SkillLoader(
        [
            SkillRoot(
                path="maurice/system_skills",
                origin="system",
                mutable=False,
            )
        ],
        enabled_skills=["self_update"],
    ).load()

    skill = registry.skills["self_update"]

    assert skill.state == SkillState.LOADED
    assert "self_update.propose" in registry.tools
    assert "proposals" in skill.prompt
    assert registry.tools["self_update.propose"].permission.permission_class == "runtime.write"


def test_loader_reads_prompt_and_dream_fragments(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "echo",
        minimal_manifest("echo"),
        prompt="Prompt fragment",
        dreams="Dream fragment",
    )

    registry = SkillLoader([SkillRoot(path=str(root), origin="user", mutable=True)]).load()

    assert registry.skills["echo"].prompt == "Prompt fragment"
    assert registry.skills["echo"].dreams == "Dream fragment"
    assert registry.tools["echo.echo"].executor == "tests.echo.echo"


def test_loader_rejects_name_collisions(tmp_path) -> None:
    system_root = tmp_path / "system"
    user_root = tmp_path / "user"
    write_skill(system_root, "memory", minimal_manifest("memory"))
    write_skill(user_root, "memory", minimal_manifest("memory"))

    loader = SkillLoader(
        [
            SkillRoot(path=str(system_root), origin="system", mutable=False),
            SkillRoot(path=str(user_root), origin="user", mutable=True),
        ]
    )

    with pytest.raises(SkillLoadError, match="collision"):
        loader.load()


def test_missing_required_dependency_marks_optional_skill_missing(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "needs_memory",
        minimal_manifest(
            "needs_memory",
            extra="""
dependencies:
  skills: ["memory"]
  optional_skills: []
""",
        ),
    )

    registry = SkillLoader([SkillRoot(path=str(root), origin="user", mutable=True)]).load()

    assert registry.skills["needs_memory"].state == SkillState.MISSING_DEPENDENCY
    assert "memory" in registry.skills["needs_memory"].missing_dependencies
    assert registry.tools == {}


def test_missing_required_skill_dependency_blocks_startup(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "needs_memory",
        minimal_manifest(
            "needs_memory",
            extra="""
required: true
dependencies:
  skills: ["memory"]
  optional_skills: []
""",
        ),
    )

    with pytest.raises(RequiredSkillError):
        SkillLoader([SkillRoot(path=str(root), origin="user", mutable=True)]).load()


def test_broken_optional_skill_becomes_disabled_with_error(tmp_path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("name: broken\n", encoding="utf-8")
    events = EventStore(tmp_path / "events.jsonl")

    registry = SkillLoader(
        [SkillRoot(path=str(root), origin="user", mutable=True)],
        event_store=events,
    ).load()

    assert registry.skills["broken"].state == SkillState.DISABLED_WITH_ERROR
    assert registry.skills["broken"].suggested_fixes
    assert registry.tools == {}
    event = events.read_all()[0]
    assert event.name == "skill.failed"
    assert event.payload["suggested_fixes"]


def test_broken_skill_with_extra_fields_gets_autocorrection_suggestion(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "too_strict",
        minimal_manifest(
            "too_strict",
            extra="""
unexpected_field: true
""",
        ),
    )

    registry = SkillLoader([SkillRoot(path=str(root), origin="user", mutable=True)]).load()

    skill = registry.skills["too_strict"]
    assert skill.state == SkillState.DISABLED_WITH_ERROR
    assert "Remove unsupported field `unexpected_field` from skill.yaml." in skill.suggested_fixes
    assert registry.tools == {}


def test_tool_collision_disables_only_colliding_optional_skill(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "first", minimal_manifest("first"))
    write_skill(
        root,
        "second",
        """
name: second
version: 0.1.0
origin: user
mutable: true
description: Test skill.
config_namespace: skills.second
requires:
  binaries: []
  credentials: []
dependencies:
  skills: []
  optional_skills: []
permissions: []
tools:
  - name: first.echo
    description: Duplicate tool.
    input_schema: {}
    permission_class: fs.read
    permission_scope: {}
    trust:
      input: local_mutable
      output: local_mutable
    executor: tests.second.echo
backend: null
storage: null
dreams:
  attachment: dreams.md
events:
  state_publisher: null
""",
    )

    registry = SkillLoader([SkillRoot(path=str(root), origin="user", mutable=True)]).load()

    assert registry.skills["first"].state == SkillState.LOADED
    assert registry.skills["second"].state == SkillState.DISABLED_WITH_ERROR
    assert registry.tools.keys() == {"first.echo"}
    assert registry.skills["second"].suggested_fixes


def test_disabled_skill_does_not_register_tools(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "echo", minimal_manifest("echo"))

    registry = SkillLoader(
        [SkillRoot(path=str(root), origin="user", mutable=True)],
        enabled_skills=[],
    ).load()

    assert registry.skills["echo"].state == SkillState.DISABLED
    assert registry.tools == {}


def test_loader_emits_skill_health_events(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "echo", minimal_manifest("echo"))
    events = EventStore(tmp_path / "events.jsonl")

    SkillLoader(
        [SkillRoot(path=str(root), origin="user", mutable=True)],
        event_store=events,
        agent_id="main",
        session_id="sess_1",
    ).load()

    assert events.read_all(names=["skill.loaded"])[0].payload["skill"] == "echo"
