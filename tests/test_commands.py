from __future__ import annotations

from maurice.host.command_registry import CommandContext, CommandRegistry, default_command_registry
from maurice.kernel.skills import SkillLoader, SkillRoot


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


def test_dev_commands_use_explicit_active_project_path(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    project = tmp_path / "current"
    project.mkdir()
    context = CommandContext(
        message_text="/projects",
        channel="local",
        peer_id="peer_1",
        agent_id="main",
        session_id="local:peer_1",
        correlation_id="corr_1",
        callbacks={
            "scope": "local",
            "content_root": project,
            "active_project_path": project,
            "workspace": project,
        },
    )

    listed = commands.dispatch(context)
    plan_context = context.__class__(
        **{**context.__dict__, "message_text": "/plan"}
    )
    opened_context = context.__class__(
        **{**context.__dict__, "message_text": "/project open nested"}
    )
    plan = commands.dispatch(plan_context)
    opened = commands.dispatch(opened_context)

    assert listed is not None
    assert f"`{project}`" in listed.text
    assert plan is not None
    assert (project / ".maurice" / "PLAN.md").is_file()
    assert not (project / "nested").exists()
    assert opened is not None
    assert "deja centre" in opened.text


def test_help_hides_global_project_picker_in_local_scope(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    result = commands.dispatch(
        CommandContext(
            message_text="/help",
            channel="local",
            peer_id="peer_1",
            agent_id="main",
            session_id="local:peer_1",
            correlation_id="corr_1",
            callbacks={
                "scope": "local",
                "workspace": tmp_path,
                "command_registry": commands,
            },
        )
    )

    assert result is not None
    assert "/plan - cadrer" in result.text
    assert "/project -" not in result.text
    assert "/projects -" not in result.text


def test_default_help_hides_host_commands_in_local_scope() -> None:
    text = default_command_registry().help_text(scope="local")

    assert "/help - afficher cette aide" in text
    assert "/setup - configurer Maurice ou passer en assistant de bureau" in text
    assert "/add_agent -" not in text
    assert "/edit_agent -" not in text


def test_setup_command_points_to_unified_setup() -> None:
    commands = default_command_registry()

    result = commands.dispatch(
        CommandContext(
            message_text="/setup",
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={"scope": "local", "command_registry": commands},
        )
    )

    assert result is not None
    assert "maurice setup" in result.text
    assert "assistant de bureau" in result.text


def test_help_shows_project_picker_in_global_scope(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    result = commands.dispatch(
        CommandContext(
            message_text="/help",
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={
                "scope": "global",
                "workspace": tmp_path,
                "command_registry": commands,
            },
        )
    )

    assert result is not None
    assert "/project - ouvrir ou afficher le projet actif" in result.text
    assert "/projects - lister les projets de l'agent courant" in result.text


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
    assert "app" in plan.text
    assert "cadrage du plan" in plan.text
    assert "agent_prompt" in plan.metadata
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


def test_dev_plan_returns_model_planning_prompt(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))

    result = commands.dispatch(_context(tmp_path, "/plan"))

    assert result is not None
    assert "app" in result.text
    assert "cadrage du plan" in result.text
    prompt = result.metadata["agent_prompt"]
    assert "Aucune demande fournie" in prompt
    assert "pose une seule question courte" in prompt
    assert "Structure exacte attendue dans `PLAN.md`" in prompt


def test_dev_plan_with_pitch_delegates_framing_to_model(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))

    result = commands.dispatch(_context(tmp_path, "/plan ajouter un dashboard"))

    assert result is not None
    assert "cadrage du plan" in result.text
    prompt = result.metadata["agent_prompt"]
    assert 'Demande utilisateur : "ajouter un dashboard"' in prompt
    assert "pose 1 a 3 questions de cadrage utiles" in prompt
    assert "## Critique" in prompt
    assert "## Taches" in prompt


def test_dev_plan_prompt_keeps_user_feature_in_expected_tasks(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))

    result = commands.dispatch(_context(tmp_path, "/plan ajouter une lib d'icones"))

    assert result is not None
    prompt = result.metadata["agent_prompt"]
    assert "ajouter une lib d'icones" in prompt
    assert "taches ordonnees, concretes, verifiables, centrees sur la demande utilisateur" in prompt
    assert "- [ ] Description. [ non parallellisable ]" in prompt


def test_dev_plan_prompt_warns_before_overwriting_existing_tasks(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    plan_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Taches\n\n"
        "- [ ] Ancienne tache. [ non parallellisable ]\n",
        encoding="utf-8",
    )

    result = commands.dispatch(_context(tmp_path, "/plan ajouter une lib d'icones"))

    assert result is not None
    prompt = result.metadata["agent_prompt"]
    assert "Plan existant" in prompt
    assert "validation remplacera les taches existantes" in prompt
    assert "- [ ] Ancienne tache. [ non parallellisable ]" in prompt


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
    assert "Ne les ecarte pas comme `deja fait` ou `plan depasse`" in str(prompt)
    assert "Sois concis dans tes messages" in str(prompt)
    assert "3-5 lignes" in result.metadata["autonomy"]["continue_prompt"]
    assert "Lire l'existant" in str(prompt)
    assert result.metadata["autonomy"]["requires_activity"] is True
    assert result.metadata["autonomy"]["continue_without_activity"] is True
    assert result.metadata["autonomy"]["max_continuations"] == 120
    assert result.metadata["autonomy"]["max_seconds"] == 3600
    assert result.metadata["agent_limits"]["max_tool_iterations"] == 80


def test_dev_command_can_focus_user_request(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    plan_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Taches\n\n"
        "- [ ] Ajouter une bibliotheque d'icones. [ non parallellisable ]\n",
        encoding="utf-8",
    )

    result = commands.dispatch(_context(tmp_path, "/dev ajouter une lib d'icones"))

    assert result is not None
    prompt = result.metadata["agent_prompt"]
    assert "Demande utilisateur prioritaire pour ce passage : ajouter une lib d'icones" in str(prompt)


def test_dev_command_injects_focus_into_existing_generic_plan(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    plan_path = tmp_path / "agents" / "main" / "content" / "app" / ".maurice" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Taches\n\n"
        "- [x] Lire l'existant. [ non parallellisable ]\n"
        "- [ ] Implementer le parcours principal de bout en bout. [ non parallellisable ]\n",
        encoding="utf-8",
    )

    result = commands.dispatch(_context(tmp_path, "/dev ajouter une lib d'icones"))

    plan = plan_path.read_text(encoding="utf-8")
    assert result is not None
    assert "- [ ] ajouter une lib d'icones. [ non parallellisable ]" in plan
    assert plan.index("ajouter une lib d'icones") < plan.index("Implementer le parcours principal")
    assert "ajouter une lib d'icones. [ non parallellisable ]" in result.metadata["agent_prompt"]
