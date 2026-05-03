from __future__ import annotations

import subprocess

from maurice.host.command_registry import CommandContext, CommandRegistry, default_command_registry
from maurice.host.project_registry import record_known_project, record_machine_project, record_seen_project
from maurice.host.workspace import initialize_workspace
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoader, SkillRoot
from maurice.system_skills.self_update.tools import propose
from maurice.system_skills.dev.commands import build_dream_input


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


def test_help_shows_project_commands_in_local_scope(tmp_path) -> None:
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
                "active_project_path": tmp_path,
                "command_registry": commands,
            },
        )
    )

    assert result is not None
    assert "/plan - cadrer" in result.text
    assert "/project - ouvrir" in result.text
    assert "/projects - lister" in result.text


def test_help_hides_project_commands_without_active_project(tmp_path) -> None:
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
    assert "/project - ouvrir" in result.text
    assert "/projects - lister" in result.text
    assert "/plan - cadrer" not in result.text
    assert "/tasks - afficher" not in result.text
    assert "/dev - executer" not in result.text
    assert "/review - relire" not in result.text
    assert "/commit - preparer" not in result.text


def test_project_command_is_refused_without_active_project(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    result = commands.dispatch(
        CommandContext(
            message_text="/plan",
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
    assert "demande un projet actif" in result.text
    assert result.metadata["blocked"] == "missing_active_project"


def test_default_help_hides_host_commands_in_local_scope() -> None:
    text = default_command_registry().help_text(scope="local")

    assert "/help - afficher cette aide" in text
    assert "/setup -" not in text
    assert "/add_agent -" not in text
    assert "/edit_agent -" not in text


def test_command_registry_exports_telegram_bot_commands() -> None:
    exported = default_command_registry().telegram_bot_commands(scope="global")
    exported_for_other_agent = default_command_registry().telegram_bot_commands(
        scope="global",
        agent_id="num2",
    )

    assert {"command": "help", "description": "afficher cette aide"} in exported
    assert {"command": "add_agent", "description": "creer un nouvel agent"} in exported
    assert {"command": "add_agent", "description": "creer un nouvel agent"} not in exported_for_other_agent
    assert {"command": "edit_agent", "description": "modifier un agent (`/edit_agent <agent>`)"} not in exported_for_other_agent
    assert all(not item["command"].startswith("/") for item in exported)


def test_telegram_command_menu_hides_project_commands_without_active_project() -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    exported = commands.telegram_bot_commands(scope="global", has_active_project=False)
    names = {item["command"] for item in exported}

    assert "project" in names
    assert "projects" in names
    assert "plan" not in names
    assert "tasks" not in names
    assert "dev" not in names
    assert "review" not in names
    assert "commit" not in names


def test_setup_command_is_not_exposed_as_runtime_command() -> None:
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

    assert result is None


def test_stop_command_uses_cancel_callback() -> None:
    calls = []
    commands = default_command_registry()

    result = commands.dispatch(
        CommandContext(
            message_text="/stop",
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={
                "cancel_turn": lambda agent_id, session_id: calls.append((agent_id, session_id)) or True,
            },
        )
    )

    assert result is not None
    assert calls == [("main", "web:peer_1")]
    assert result.metadata["cancelled"] is True
    assert "Annulation demandee" in result.text


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


def test_projects_lists_known_external_projects(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    external = tmp_path / "external-app"
    external.mkdir(parents=True)
    record_known_project(agent_workspace, external)

    result = commands.dispatch(
        CommandContext(
            message_text="/projects",
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={
                "scope": "global",
                "workspace": workspace,
                "agent_workspace": agent_workspace,
            },
        )
    )

    assert result is not None
    assert "Projets deja vus" in result.text
    assert "`external-app`" in result.text
    assert str(external.resolve()) in result.text


def test_project_open_can_reopen_known_external_project(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    external = tmp_path / "external-app"
    external.mkdir(parents=True)
    record_known_project(agent_workspace, external)

    result = commands.dispatch(
        CommandContext(
            message_text="/project open external-app",
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={
                "scope": "global",
                "workspace": workspace,
                "agent_workspace": agent_workspace,
            },
        )
    )

    assert result is not None
    assert f"Dossier : `{external.resolve()}`" in result.text
    assert (external / ".maurice" / "PLAN.md").is_file()


def test_self_update_commands_list_show_validate_and_apply(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    (runtime / "hello.txt").write_text("old\n", encoding="utf-8")
    initialize_workspace(workspace, runtime)
    proposal = propose(
        {
            "target_type": "host",
            "target_name": "hello",
            "runtime_path": "$runtime/hello.txt",
            "summary": "Update hello.",
            "patch": "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "risk": "low",
            "test_plan": "$ true",
            "mode": "proposal_only",
        },
        PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime)),
    )
    proposal_id = proposal.data["proposal"]["id"]
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["self_update"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    def ctx(text: str) -> CommandContext:
        return CommandContext(
            message_text=text,
            channel="web",
            peer_id="peer_1",
            agent_id="main",
            session_id="web:peer_1",
            correlation_id="corr_1",
            callbacks={"context_root": workspace, "workspace": workspace},
        )

    listed = commands.dispatch(ctx("/auto_update_list"))
    shown = commands.dispatch(ctx(f"/auto_update_show {proposal_id}"))
    validated = commands.dispatch(ctx(f"/auto_update_validate {proposal_id}"))
    unconfirmed = commands.dispatch(ctx(f"/auto_update_apply {proposal_id}"))
    applied = commands.dispatch(ctx(f"/auto_update_apply {proposal_id} confirm"))

    assert listed is not None
    assert proposal_id in listed.text
    assert "/auto_update_show" not in listed.text
    assert f"/auto_update_apply {proposal_id} confirm" in listed.text
    assert "```diff" in listed.text
    assert "-old" in listed.text
    assert "+new" in listed.text
    assert shown is None
    assert validated is None
    assert unconfirmed is not None
    assert "Application non lancee" in unconfirmed.text
    assert applied is not None
    assert "Application" in applied.text
    assert applied.metadata["applied"] is True
    assert (runtime / "hello.txt").read_text(encoding="utf-8") == "new\n"


def test_self_update_list_command_works_without_global_workspace_config(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["self_update"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)

    result = commands.dispatch(
        CommandContext(
            message_text="/auto_update_list",
            channel="cli",
            peer_id="local",
            agent_id="main",
            session_id="local",
            correlation_id="corr_1",
            callbacks={"context_root": tmp_path, "workspace": tmp_path},
        )
    )

    assert result is not None
    assert "Aucune proposition" in result.text


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


def test_dev_dream_input_reads_known_external_project_memory(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    project = tmp_path / "external-app"
    meta = project / ".maurice"
    meta.mkdir(parents=True)
    (meta / "AGENTS.md").write_text("# Rules\n\n- Prefer boring code.\n", encoding="utf-8")
    (meta / "PLAN.md").write_text(
        "# Plan\n\n- [ ] Finish the registry. [ non parallellisable ]\n"
        "- [x] Write the baseline. [ non parallellisable ]\n",
        encoding="utf-8",
    )
    (meta / "DECISIONS.md").write_text("- 2026-05-03 - Keep project memory local.\n", encoding="utf-8")
    (meta / "dreams.md").write_text("- Watch for stale local/global wording.\n", encoding="utf-8")
    record_seen_project(agent_workspace, project)

    dream_input = build_dream_input(
        PermissionContext(
            workspace_root=str(workspace),
            runtime_root=str(tmp_path),
            agent_workspace_root=str(agent_workspace),
        )
    )

    assert len(dream_input.signals) == 1
    signal = dream_input.signals[0]
    assert signal.type == "dev_project_review"
    assert signal.data["path"] == str(project.resolve())
    assert signal.data["open_tasks"] == ["Finish the registry. [ non parallellisable ]"]
    assert signal.data["done_count"] == 1
    assert "Keep project memory local" in signal.data["excerpts"]["DECISIONS.md"]
    assert "Watch for stale" in signal.data["excerpts"]["dreams.md"]


def test_dev_dream_input_reads_machine_project_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    project = tmp_path / "machine-app"
    meta = project / ".maurice"
    meta.mkdir(parents=True)
    (meta / "PLAN.md").write_text("# Plan\n\n- [ ] Ship the global dream.\n", encoding="utf-8")
    record_machine_project(project)

    dream_input = build_dream_input(
        PermissionContext(
            workspace_root=str(workspace),
            runtime_root=str(tmp_path),
            agent_workspace_root=str(agent_workspace),
        )
    )

    assert len(dream_input.signals) == 1
    assert dream_input.signals[0].data["path"] == str(project.resolve())
    assert dream_input.signals[0].data["open_tasks"] == ["Ship the global dream."]


def test_dev_dream_input_reports_missing_known_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    missing = tmp_path / "missing-app"
    record_seen_project(agent_workspace, missing)

    dream_input = build_dream_input(
        PermissionContext(
            workspace_root=str(workspace),
            runtime_root=str(tmp_path),
            agent_workspace_root=str(agent_workspace),
        )
    )

    assert len(dream_input.signals) == 1
    assert dream_input.signals[0].type == "dev_project_missing"
    assert dream_input.signals[0].data["path"] == str(missing.resolve())


def test_dev_dream_input_respects_signal_limit(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    agent_workspace = workspace / "agents" / "main"
    for index in range(3):
        project = tmp_path / f"app-{index}"
        meta = project / ".maurice"
        meta.mkdir(parents=True)
        (meta / "PLAN.md").write_text(f"# Plan {index}\n", encoding="utf-8")
        record_seen_project(agent_workspace, project)

    dream_input = build_dream_input(
        PermissionContext(
            workspace_root=str(workspace),
            runtime_root=str(tmp_path),
            agent_workspace_root=str(agent_workspace),
        ),
        limit=2,
    )

    assert len(dream_input.signals) == 2


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
    assert "branche dediee `maurice/...`" in str(prompt)
    assert "git merge" in str(prompt)
    assert "demande explicitement l'approbation utilisateur avant tout merge" in str(prompt)
    assert "Si l'utilisateur approuve ensuite le merge" in str(prompt)
    assert "branche de depart" in str(prompt)
    assert "Ne push jamais sans approbation separee" in str(prompt)
    assert "passe de finition proportionnee" in str(prompt)
    assert "supprime le code mort" in str(prompt)
    assert "Mets a jour la documentation" in str(prompt)
    assert "Ajoute ou adapte les tests" in str(prompt)
    assert "credentials, permissions, shell" in str(prompt)
    assert "Ne les ecarte pas comme `deja fait` ou `plan depasse`" in str(prompt)
    assert "Sois concis dans tes messages" in str(prompt)
    assert "3-5 lignes" in result.metadata["autonomy"]["continue_prompt"]
    assert "Lire l'existant" in str(prompt)
    assert result.metadata["autonomy"]["requires_activity"] is True
    assert result.metadata["autonomy"]["continue_without_activity"] is True
    assert result.metadata["autonomy"]["max_continuations"] == 120
    assert result.metadata["autonomy"]["max_seconds"] == 3600
    assert result.metadata["agent_limits"]["max_tool_iterations"] == 80


def test_commit_command_requires_feature_branch_and_merge_approval(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    project = tmp_path / "agents" / "main" / "content" / "app"
    subprocess.run(["git", "-C", str(project), "init"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(project), "branch", "-M", "main"], check=True, capture_output=True, text=True)
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")

    result = commands.dispatch(_context(tmp_path, "/commit"))

    assert result is not None
    assert result.metadata["command"] == "/commit"
    prompt = str(result.metadata["agent_prompt"])
    assert "Branche actuelle : main" in prompt
    assert "Branche de travail Maurice suggeree : maurice/" in prompt
    assert "Branche cible du merge apres approbation : main" in prompt
    assert "switch -c maurice/" in prompt
    assert "Ne lance jamais `git merge`, `git push`" in prompt
    assert "demande explicitement a l'utilisateur s'il approuve le merge" in prompt
    assert "le prochain tour devra verifier que le working tree est propre" in prompt
    assert "merger la branche Maurice vers la branche cible indiquee ci-dessus" in prompt
    assert "ne jamais push sans approbation separee" in prompt


def test_dev_command_records_departure_branch_as_merge_target(tmp_path) -> None:
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["dev"],
    ).load()
    commands = CommandRegistry.from_skill_registry(registry)
    commands.dispatch(_context(tmp_path, "/project open app"))
    project = tmp_path / "agents" / "main" / "content" / "app"
    subprocess.run(["git", "-C", str(project), "init"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(project), "branch", "-M", "main"], check=True, capture_output=True, text=True)
    plan_path = project / ".maurice" / "PLAN.md"
    plan_path.write_text(
        "# Plan\n\n## Taches\n\n- [ ] Ajouter une option. [ non parallellisable ]\n",
        encoding="utf-8",
    )

    result = commands.dispatch(_context(tmp_path, "/dev"))

    assert result is not None
    prompt = str(result.metadata["agent_prompt"])
    assert "Branche actuelle : main" in prompt
    assert "Branche cible du merge apres approbation : main" in prompt
    assert "merge la branche `maurice/...` vers la branche de depart" in prompt


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


def test_dev_command_replaces_existing_plan_when_focus_is_provided(tmp_path) -> None:
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
    assert "Implementer le parcours principal" not in plan
    assert "ajouter une lib d'icones. [ non parallellisable ]" in result.metadata["agent_prompt"]
