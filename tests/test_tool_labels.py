from __future__ import annotations

from maurice.kernel.tool_labels import tool_action_label


def test_host_agent_create_has_human_label() -> None:
    label = tool_action_label("host.agent_create", {"agent_id": "num2"})

    assert label == "créer un agent `num2`"
    assert "host.agent_create" not in label


def test_unknown_tool_falls_back_without_skill_prefix() -> None:
    assert tool_action_label("custom.my_tool") == "my tool"


def test_host_credentials_has_human_label() -> None:
    assert tool_action_label("host.credentials") == "lister les profils d'authentification"


def test_host_doctor_and_logs_have_human_labels() -> None:
    assert tool_action_label("host.doctor") == "diagnostiquer Maurice"
    assert tool_action_label("host.logs") == "lire les derniers événements Maurice"
