from __future__ import annotations

from maurice.host.command_registry import CommandContext, CommandRegistry
from maurice.kernel.skills import SkillLoader, SkillRoot
from maurice.system_skills.dev.planner import handle_plan_wizard


def _context(tmp_path, text: str) -> CommandContext:
    return CommandContext(
        message_text=text,
        channel="local",
        peer_id="peer_1",
        agent_id="main",
        session_id="local:peer_1",
        correlation_id="corr_1",
        callbacks={
            "workspace": tmp_path,
            "agent_workspace": tmp_path / "agents" / "main",
        },
    )


def test_command_registry_executes_dev_project_commands(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    opened = commands.dispatch(_context(tmp_path, "/project open app"))
    listed = commands.dispatch(_context(tmp_path, "/projects"))
    plan = commands.dispatch(_context(tmp_path, "/plan"))

    project = tmp_path / "agents" / "main" / "content" / "app"
    assert opened is not None
    assert "Projet `app` cree et ouvert" in opened.text
    assert listed is not None
    assert "`app`" in listed.text
    assert plan is not None
    assert "On cadre le plan du projet `app`" in plan.text
    assert (project / ".maurice" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (project / ".maurice" / "AGENTS.md").is_file()
    assert (project / ".maurice" / "PLAN.md").is_file()
    assert (project / ".maurice" / "DECISIONS.md").is_file()


def test_dev_project_open_moves_legacy_memory_files(tmp_path) -> None:
    project = tmp_path / "agents" / "main" / "content" / "app"
    project.mkdir(parents=True)
    legacy_plan = project / "PLAN.md"
    legacy_plan.write_text("# Old plan\n", encoding="utf-8")
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    commands.dispatch(_context(tmp_path, "/project open app"))

    assert not legacy_plan.exists()
    assert (project / ".maurice" / "PLAN.md").read_text(encoding="utf-8") == "# Old plan\n"


def test_dev_plan_wizard_writes_plan_md(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    context = _context(tmp_path, "/project open app")
    commands.dispatch(context)

    start = commands.dispatch(_context(tmp_path, "/plan"))
    store = tmp_path / "agents" / "main" / ".dev_plan_wizards.json"

    assert start is not None
    assert "Pitch le projet" in start.text
    assert "Pour qui" in handle_plan_wizard(
        store_path=store,
        agent_id="main",
        session_id="local:peer_1",
        text="Un chat navigateur pour piloter Maurice",
    )
    assert "Quelles contraintes" in handle_plan_wizard(
        store_path=store,
        agent_id="main",
        session_id="local:peer_1",
        text="Utilisateur non technique",
    )
    assert "Derniere question" in handle_plan_wizard(
        store_path=store,
        agent_id="main",
        session_id="local:peer_1",
        text="Interface simple et markdown lisible",
    )
    proposal = handle_plan_wizard(
        store_path=store,
        agent_id="main",
        session_id="local:peer_1",
        text="On peut discuter, planifier et verifier depuis le navigateur",
    )
    assert proposal is not None
    assert "Voici ma proposition" in proposal
    assert "Critique" in proposal
    done = handle_plan_wizard(
        store_path=store,
        agent_id="main",
        session_id="local:peer_1",
        text="oui",
    )

    plan_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "PLAN.md"
    decisions_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "DECISIONS.md"
    assert done is not None
    assert "Plan cree" in done
    content = plan_path.read_text(encoding="utf-8")
    assert "Un chat navigateur" in content
    assert "Utilisateur non technique" in content
    assert "- [ ]" in content
    assert "[ parallellisable ]" in content
    assert "Un chat navigateur" in decisions_path.read_text(encoding="utf-8")


def test_command_registry_executes_dev_decision_command(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    commands.dispatch(_context(tmp_path, "/project open app"))
    result = commands.dispatch(_context(tmp_path, "/decision garder une UI simple"))

    decisions = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "DECISIONS.md"
    assert result is not None
    assert "Decision ajoutee" in result.text
    assert "garder une UI simple" in decisions.read_text(encoding="utf-8")


def test_dev_projects_are_relative_to_current_agent(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    context = CommandContext(
        message_text="/project open app",
        channel="local",
        peer_id="peer_1",
        agent_id="coding",
        session_id="local:peer_1",
        correlation_id="corr_1",
        callbacks={
            "workspace": tmp_path,
            "agent_workspace_for": lambda agent_id: tmp_path / "agents" / agent_id,
        },
    )

    commands.dispatch(context)

    assert (tmp_path / "agents" / "coding" / "content" / "app").is_dir()
    assert not (tmp_path / "agents" / "main" / "content" / "app").exists()


def test_dev_review_returns_project_summary(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    commands.dispatch(_context(tmp_path, "/project open app"))
    result = commands.dispatch(_context(tmp_path, "/review"))

    assert result is not None
    assert "Review du projet `app`" in result.text
    assert "Taches ouvertes" in result.text
    assert "Etat Git" in result.text
    assert "Avis" in result.text


def test_dev_command_triggers_autonomous_agent_prompt(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    plan_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Taches\n\n"
        "- [ ] Lire l'existant. [ non parallellisable ]\n"
        "- [ ] Ajouter les tests. [ parallellisable ]\n",
        encoding="utf-8",
    )

    result = commands.dispatch(_context(tmp_path, "/dev"))

    assert result is not None
    assert result.metadata["command"] == "/dev"
    prompt = result.metadata["agent_prompt"]
    assert "execute le plan" in str(prompt).lower()
    assert "Tu es en mode developpement" in str(prompt)
    assert "Utilise les outils disponibles quand ils sont utiles" in str(prompt)
    assert "Lire l'existant" in str(prompt)
    assert result.metadata["autonomy"]["requires_activity"] is True
    assert result.metadata["autonomy"]["continue_without_activity"] is True
    assert result.metadata["autonomy"]["max_continuations"] == 120
    assert result.metadata["autonomy"]["max_seconds"] == 3600
    assert result.metadata["agent_limits"]["max_tool_iterations"] == 80
