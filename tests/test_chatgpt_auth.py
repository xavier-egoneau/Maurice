from __future__ import annotations

import time

from maurice.host.auth import (
    build_chatgpt_authorize_url,
    clear_chatgpt_auth,
    generate_pkce,
    get_valid_chatgpt_access_token,
    load_chatgpt_auth,
    save_chatgpt_auth,
)
from maurice.host.credentials import load_credentials


def test_chatgpt_auth_is_stored_in_credentials_yaml(tmp_path) -> None:
    save_chatgpt_auth(
        tmp_path,
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires": time.time() + 3600,
        },
    )

    store = load_credentials(tmp_path / "credentials.yaml")
    record = store.credentials["chatgpt"]

    assert record.type == "token"
    assert record.value == "access"
    assert record.refresh_token == "refresh"
    assert record.provider == "chatgpt_codex"


def test_chatgpt_auth_refreshes_expired_token(tmp_path) -> None:
    save_chatgpt_auth(
        tmp_path,
        {
            "access_token": "expired",
            "refresh_token": "refresh",
            "expires": time.time() - 10,
        },
    )

    def refresh_transport(_url, payload):
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "refresh"
        return {"access_token": "fresh", "refresh_token": "new-refresh", "expires_in": 3600}

    token = get_valid_chatgpt_access_token(tmp_path, refresh_transport=refresh_transport)
    record = load_chatgpt_auth(tmp_path)

    assert token == "fresh"
    assert record.value == "fresh"
    assert record.refresh_token == "new-refresh"


def test_chatgpt_auth_can_be_cleared(tmp_path) -> None:
    save_chatgpt_auth(
        tmp_path,
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires": time.time() + 3600,
        },
    )

    assert clear_chatgpt_auth(tmp_path)
    assert load_chatgpt_auth(tmp_path) is None
    assert not clear_chatgpt_auth(tmp_path)


def test_chatgpt_authorize_url_uses_pkce_and_state() -> None:
    verifier, challenge = generate_pkce()
    url = build_chatgpt_authorize_url(
        code_challenge=challenge,
        state="state_123",
        redirect_uri="http://localhost:1455/auth/callback",
    )

    assert verifier
    assert challenge
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=state_123" in url
    assert "originator=maurice" in url
