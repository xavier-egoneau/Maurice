from __future__ import annotations

from maurice.host.agent_wizard import handle_agent_creation_wizard
from maurice.host.agents import create_agent
from maurice.host.credentials import CredentialRecord, load_workspace_credentials, write_workspace_credentials
from maurice.host.paths import host_config_path, kernel_config_path
from maurice.host.secret_capture import capture_pending_secret, clear_secret_capture, list_secret_captures
from maurice.host.workspace import initialize_workspace
from maurice.kernel.config import load_workspace_config, read_yaml_file, write_yaml_file


def _send(workspace, text: str, *, agent_id: str = "main", session_id: str = "telegram:123") -> str:
    result = handle_agent_creation_wizard(
        workspace,
        agent_id=agent_id,
        session_id=session_id,
        text=text,
    )
    assert result is not None
    return result


def test_agent_wizard_asks_one_explicit_question_at_a_time(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    assert "Quel nom unique" in _send(workspace, "créons un nouvel agent")
    name_response = _send(workspace, "Maurice-polo")
    assert "maurice_polo" in name_response
    assert "mission principale" in name_response
    permission_question = _send(workspace, "organisation, aide aux devoirs")
    assert "`safe`" in permission_question
    assert "`limited`" in permission_question
    assert "`power`" in permission_question
    skills_question = _send(workspace, "limited")
    assert "`filesystem`" in skills_question
    assert "`explore`" in skills_question
    assert "`self_update`" in skills_question
    assert "1. `filesystem`" in skills_question
    assert "`tous`" in skills_question
    assert "Reponds par des numeros" in skills_question
    assert "Quel modele" in _send(workspace, "ok")
    telegram_question = _send(workspace, "defaut")
    assert "`aucun`" in telegram_question
    assert "`telegram`" in telegram_question


def test_agent_wizard_starts_from_add_agent_command(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    first = _send(workspace, "/add_agent")

    assert "Quel nom unique" in first


def test_agent_wizard_refuses_agent_admin_commands_outside_main(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    first = _send(workspace, "/add_agent", agent_id="num2")

    assert "uniquement depuis l'agent `main`" in first


def test_agent_wizard_accepts_all_and_numbered_skills(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    _send(workspace, "nouvel agent")
    _send(workspace, "agent tous")
    _send(workspace, "organisation")
    _send(workspace, "limited")
    model_question = _send(workspace, "tous")
    assert "Quel modele" in model_question
    _send(workspace, "defaut")
    _send(workspace, "aucun")
    _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert bundle.agents.agents["agent_tous"].skills == [
        "filesystem",
        "exec",
        "memory",
        "web",
        "explore",
        "reminders",
        "vision",
        "dreaming",
        "workspace_dreaming",
        "veille",
        "skills",
        "host",
        "self_update",
        "dev",
        "daily",
    ]


def test_agent_wizard_lists_user_skills_from_workspace_roots(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    write_yaml_file(
        workspace / "skills" / "translation" / "skill.yaml",
        {
            "name": "translation",
            "version": "0.1.0",
            "origin": "user",
            "mutable": True,
            "description": "Translate and adapt project text.",
            "config_namespace": "skills.translation",
            "requires": {"binaries": [], "credentials": []},
            "dependencies": {"skills": [], "optional_skills": []},
            "permissions": [],
            "tools": [],
        },
    )

    _send(workspace, "nouvel agent")
    _send(workspace, "agent trad")
    _send(workspace, "traduction")
    skills_question = _send(workspace, "limited")

    assert "`translation`" in skills_question


def test_agent_wizard_lists_declarative_user_skills_from_workspace_roots(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    skill_dir = workspace / "skills" / "calendar_notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\n"
        "name: calendar_notes\n"
        "description: Read local calendar notes.\n"
        "---\n"
        "\n"
        "# Calendar Notes\n",
        encoding="utf-8",
    )
    (skill_dir / "dreams.md").write_text("Notice calendar events.\n", encoding="utf-8")
    (skill_dir / "daily.md").write_text("Surface today's events.\n", encoding="utf-8")

    _send(workspace, "nouvel agent")
    _send(workspace, "agent calendar")
    _send(workspace, "agenda")
    skills_question = _send(workspace, "limited")

    assert "`calendar_notes`" in skills_question


def test_agent_wizard_lists_models_from_current_provider(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    kernel_data["kernel"]["model"] = {
        "provider": "auth",
        "protocol": "chatgpt_codex",
        "name": "gpt-5",
        "base_url": None,
        "credential": "chatgpt_codex",
    }
    write_yaml_file(kernel_config_path(workspace), kernel_data)
    monkeypatch.setattr(
        "maurice.host.agent_wizard.chatgpt_model_choices",
        lambda: [("gpt-5.4-mini", "GPT 5.4 Mini")],
    )

    _send(workspace, "nouvel agent")
    _send(workspace, "agent modele")
    _send(workspace, "organisation")
    _send(workspace, "limited")
    model_question = _send(workspace, "1,2")

    assert "0. `defaut`" in model_question
    assert "1. `gpt-5.4-mini`" in model_question
    _send(workspace, "1")
    _send(workspace, "aucun")
    _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    agent = bundle.agents.agents["agent_modele"]
    assert agent.model_chain == ["auth_gpt_5_4_mini"]
    assert bundle.kernel.models.entries["auth_gpt_5_4_mini"].name == "gpt-5.4-mini"
    assert bundle.agents.agents["agent_modele"].skills == ["filesystem", "exec"]
    assert bundle.agents.agents["agent_modele"].credentials == ["chatgpt_codex"]


def test_agent_wizard_creates_agent_without_telegram(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    _send(workspace, "nouvel agent")
    _send(workspace, "assistant devoirs")
    _send(workspace, "organisation")
    _send(workspace, "limited")
    _send(workspace, "filesystem, memory")
    _send(workspace, "defaut")
    summary = _send(workspace, "aucun")
    assert "Resume avant creation" in summary

    done = _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert "assistant_devoirs" in bundle.agents.agents
    assert bundle.agents.agents["assistant_devoirs"].skills == ["filesystem", "memory"]
    assert "Agent `assistant_devoirs` cree" in done


def test_agent_wizard_captures_missing_telegram_token_then_binds_ids(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")

    _send(workspace, "nouvel agent")
    _send(workspace, "telegram agent")
    _send(workspace, "bot telegram")
    _send(workspace, "limited")
    _send(workspace, "filesystem")
    _send(workspace, "defaut")
    token_prompt = _send(workspace, "telegram")

    assert "token BotFather de ce nouveau bot" in token_prompt
    assert list_secret_captures(workspace)[0].credential == "telegram_bot_telegram_agent"

    captured = capture_pending_secret(
        workspace,
        agent_id="main",
        session_id="telegram:123",
        value="123:secret",
    )
    assert captured is not None
    ids_question = _send(workspace, "7910016787")
    assert "Resume avant creation" in ids_question
    _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert bundle.host.channels["telegram_telegram_agent"]["agent"] == "telegram_agent"
    assert bundle.host.channels["telegram_telegram_agent"]["credential"] == "telegram_bot_telegram_agent"
    assert bundle.host.channels["telegram_telegram_agent"]["allowed_users"] == [7910016787]
    assert bundle.host.channels["telegram_telegram_agent"]["allowed_chats"] == [7910016787]
    assert bundle.agents.agents["telegram_agent"].channels == ["telegram"]


def test_agent_wizard_ignores_existing_main_telegram_credential(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    credentials = load_workspace_credentials(workspace)
    credentials.credentials["telegram_bot"] = CredentialRecord(
        type="token",
        value="123:secret",
        provider="telegram_bot",
    )
    write_workspace_credentials(workspace, credentials)

    _send(workspace, "nouvel agent")
    _send(workspace, "telegram agent")
    _send(workspace, "bot telegram")
    _send(workspace, "limited")
    _send(workspace, "filesystem")
    _send(workspace, "defaut")
    token_prompt = _send(workspace, "telegram")

    assert "token BotFather de ce nouveau bot" in token_prompt
    assert list_secret_captures(workspace)[0].credential == "telegram_bot_telegram_agent"


def test_agent_wizard_can_configure_new_telegram_bot_when_one_exists(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    credentials = load_workspace_credentials(workspace)
    credentials.credentials["telegram_bot"] = CredentialRecord(
        type="token",
        value="old-token",
        provider="telegram_bot",
    )
    write_workspace_credentials(workspace, credentials)

    _send(workspace, "nouvel agent")
    _send(workspace, "telegram agent")
    _send(workspace, "bot telegram")
    _send(workspace, "limited")
    _send(workspace, "filesystem")
    _send(workspace, "defaut")
    token_prompt = _send(workspace, "telegram")

    assert "token BotFather de ce nouveau bot" in token_prompt
    assert list_secret_captures(workspace)[0].credential == "telegram_bot_telegram_agent"


def test_agent_wizard_edits_telegram_line_by_line_and_captures_token(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")
    host_data = read_yaml_file(host_config_path(workspace))
    host_data.setdefault("host", {}).setdefault("channels", {})["telegram"] = {
        "adapter": "telegram",
        "enabled": True,
        "agent": "main",
        "credential": "telegram_bot",
        "allowed_users": [7910016787],
        "allowed_chats": [],
        "status": "configured_pending_adapter",
    }
    write_yaml_file(host_config_path(workspace), host_data)

    first = _send(workspace, "modifier le bot")
    assert "Agent concerne" in first
    assert "maurice_polo" in first
    agent_question = _send(workspace, "oui")
    assert "Quel agent" in agent_question
    token_question = _send(workspace, "maurice_polo")
    assert "Token du bot" in token_question
    token_prompt = _send(workspace, "oui")
    assert "telegram_bot_maurice_polo" in token_prompt
    assert list_secret_captures(workspace)[0].credential == "telegram_bot_maurice_polo"
    captured = capture_pending_secret(
        workspace,
        agent_id="main",
        session_id="telegram:123",
        value="new-token",
    )
    assert captured is not None
    users_question = _send(workspace, "ok")
    assert "IDs utilisateurs" in users_question
    _send(workspace, "oui")
    chats_question = _send(workspace, "8744612002, 7910016787")
    assert "IDs utilisateurs enregistres : 8744612002, 7910016787" in chats_question
    assert "IDs chats prives/groupes" in chats_question
    summary = _send(workspace, "non")
    assert "Resume de la modification Telegram" in summary
    done = _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert "mise a jour" in done
    assert bundle.host.channels["telegram"]["agent"] == "main"
    assert bundle.host.channels["telegram"]["credential"] == "telegram_bot"
    assert bundle.host.channels["telegram_maurice_polo"]["agent"] == "maurice_polo"
    assert bundle.host.channels["telegram_maurice_polo"]["credential"] == "telegram_bot_maurice_polo"
    assert bundle.host.channels["telegram_maurice_polo"]["allowed_users"] == [8744612002, 7910016787]
    assert bundle.host.channels["telegram_maurice_polo"]["allowed_chats"] == [8744612002, 7910016787]
    assert bundle.agents.agents["maurice_polo"].channels == ["telegram"]
    assert "telegram" not in bundle.agents.agents["main"].channels


def test_agent_wizard_starts_telegram_edit_from_natural_phrase_with_agent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")

    first = _send(workspace, "modifie le bot maurice polo")

    assert "Agent concerne : `maurice_polo`" in first
    token_question = _send(workspace, "non")
    assert "`telegram_bot_maurice_polo`" in token_question


def test_agent_wizard_starts_full_agent_edit_from_edit_agent_command(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")

    first = _send(workspace, "/edit_agent maurice polo")

    assert "Agent concerne : `maurice_polo`" in first
    assert "Permissions actuelles" in first
    skills_question = _send(workspace, "non")
    assert "Competences actuelles" in skills_question
    model_question = _send(workspace, "non")
    assert "Modele actuel" in model_question
    telegram_question = _send(workspace, "non")
    assert "Telegram actuel" in telegram_question
    summary = _send(workspace, "non")
    assert "Resume avant modification" in summary
    done = _send(workspace, "oui")
    assert "Agent `maurice_polo` mis a jour" in done


def test_agent_wizard_full_agent_edit_can_change_only_telegram_token(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")

    _send(workspace, "/edit_agent maurice_polo")
    _send(workspace, "non")
    _send(workspace, "non")
    _send(workspace, "non")
    token_question = _send(workspace, "oui")
    assert "Token du bot" in token_question
    token_prompt = _send(workspace, "oui")
    assert "telegram_bot_maurice_polo" in token_prompt
    capture_pending_secret(
        workspace,
        agent_id="main",
        session_id="telegram:123",
        value="new-token",
    )
    users_question = _send(workspace, "ok")
    assert "IDs utilisateurs" in users_question
    _send(workspace, "non")
    _send(workspace, "non")
    summary = _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert "mis a jour" in summary
    assert bundle.host.channels["telegram_maurice_polo"]["agent"] == "maurice_polo"
    assert bundle.host.channels["telegram_maurice_polo"]["credential"] == "telegram_bot_maurice_polo"


def test_agent_wizard_full_agent_edit_can_jump_directly_to_skills(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(
        workspace,
        agent_id="maurice_polo",
        permission_profile="limited",
        skills=["filesystem", "memory"],
    )

    _send(workspace, "/edit_agent maurice_polo")
    skills_question = _send(workspace, "je veux changer les skills")

    assert "Choisis les competences a activer" in skills_question
    assert "1. `filesystem` (actuelle)" in skills_question
    assert "3. `memory` (actuelle)" in skills_question
    assert "`explore`" in skills_question
    assert "Reponds par des numeros" in skills_question

    model_question = _send(workspace, "1,5,14")
    assert "Modele actuel" in model_question
    _send(workspace, "non")
    _send(workspace, "non")
    done = _send(workspace, "oui")

    bundle = load_workspace_config(workspace)
    assert "Agent `maurice_polo` mis a jour" in done
    assert bundle.agents.agents["maurice_polo"].skills == ["filesystem", "explore", "dev"]


def test_agent_wizard_full_agent_edit_can_jump_back_to_skills_later(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")

    _send(workspace, "/edit_agent maurice_polo")
    _send(workspace, "non")
    model_question = _send(workspace, "non")
    assert "Modele actuel" in model_question
    skills_question = _send(workspace, "je veux changer les skills")

    assert "Choisis les competences a activer" in skills_question
    assert "`explore`" in skills_question


def test_clear_secret_capture_can_remove_pending_request(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    initialize_workspace(workspace, runtime)
    _send(workspace, "nouvel agent")
    _send(workspace, "telegram agent")
    _send(workspace, "bot")
    _send(workspace, "limited")
    _send(workspace, "filesystem")
    _send(workspace, "defaut")
    _send(workspace, "telegram")

    assert list_secret_captures(workspace)
    assert clear_secret_capture(workspace) == 1
    assert list_secret_captures(workspace) == []
