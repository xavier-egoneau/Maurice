"""Host-owned auth helpers for login/session based providers."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import http.server
import json
import secrets
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib import parse, request

from maurice.host.credentials import (
    CredentialRecord,
    load_credentials,
    write_credentials,
)


CHATGPT_CREDENTIAL_NAME = "chatgpt"
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CHATGPT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_REDIRECT_URI = "http://localhost:1455/auth/callback"
CHATGPT_SCOPE = "openid profile email offline_access"


def credentials_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "credentials.yaml"


def save_chatgpt_auth(
    workspace_root: str | Path,
    token_data: dict[str, Any],
    *,
    credential_name: str = CHATGPT_CREDENTIAL_NAME,
) -> CredentialRecord:
    store = load_credentials(credentials_path(workspace_root))
    record = CredentialRecord(
        type="token",
        value=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", ""),
        expires=float(token_data.get("expires", time.time() + token_data.get("expires_in", 0))),
        provider="chatgpt_codex",
        obtained_at=token_data.get("obtained_at", datetime.now(UTC).isoformat()),
    )
    store.credentials[credential_name] = record
    write_credentials(credentials_path(workspace_root), store)
    return record


def load_chatgpt_auth(
    workspace_root: str | Path,
    *,
    credential_name: str = CHATGPT_CREDENTIAL_NAME,
) -> CredentialRecord | None:
    store = load_credentials(credentials_path(workspace_root))
    return store.credentials.get(credential_name)


def clear_chatgpt_auth(
    workspace_root: str | Path,
    *,
    credential_name: str = CHATGPT_CREDENTIAL_NAME,
) -> bool:
    path = credentials_path(workspace_root)
    store = load_credentials(path)
    existed = credential_name in store.credentials
    if existed:
        del store.credentials[credential_name]
        write_credentials(path, store)
    return existed


def get_valid_chatgpt_access_token(
    workspace_root: str | Path,
    *,
    credential_name: str = CHATGPT_CREDENTIAL_NAME,
    refresh_transport: Any = None,
) -> str | None:
    record = load_chatgpt_auth(workspace_root, credential_name=credential_name)
    if record is None:
        return None
    expires = float(getattr(record, "expires", 0) or 0)
    if record.value and time.time() < expires - 60:
        return record.value
    refresh_token = getattr(record, "refresh_token", "")
    if not refresh_token:
        return None
    try:
        refreshed = refresh_chatgpt_auth(refresh_token, transport=refresh_transport)
    except Exception:
        return None
    save_chatgpt_auth(
        workspace_root,
        {
            "access_token": refreshed["access_token"],
            "refresh_token": refreshed.get("refresh_token", refresh_token),
            "expires": time.time() + refreshed.get("expires_in", 0),
        },
        credential_name=credential_name,
    )
    return refreshed["access_token"]


class ChatGPTAuthFlow:
    def __init__(
        self,
        *,
        redirect_uri: str = CHATGPT_REDIRECT_URI,
        callback_host: str = "127.0.0.1",
        callback_port: int = 1455,
        timeout_seconds: int = 300,
        token_transport: Any = None,
    ) -> None:
        self.redirect_uri = redirect_uri
        self.callback_host = callback_host
        self.callback_port = callback_port
        self.timeout_seconds = timeout_seconds
        self.token_transport = token_transport

    def run(self, *, on_url: Any = None) -> dict[str, Any]:
        verifier, challenge = generate_pkce()
        state = secrets.token_hex(16)
        auth_url = build_chatgpt_authorize_url(
            code_challenge=challenge,
            state=state,
            redirect_uri=self.redirect_uri,
        )

        _ChatGPTOAuthHandler.expected_state = state
        _ChatGPTOAuthHandler.code = None
        server = http.server.HTTPServer(
            (self.callback_host, self.callback_port),
            _ChatGPTOAuthHandler,
        )
        server.timeout = 1

        if on_url is not None:
            on_url(auth_url)
        else:
            webbrowser.open(auth_url)

        deadline = time.time() + self.timeout_seconds
        try:
            while time.time() < deadline:
                server.handle_request()
                if _ChatGPTOAuthHandler.code:
                    token_data = exchange_chatgpt_code(
                        _ChatGPTOAuthHandler.code,
                        verifier,
                        self.redirect_uri,
                        transport=self.token_transport,
                    )
                    return {
                        "access_token": token_data["access_token"],
                        "refresh_token": token_data.get("refresh_token", ""),
                        "expires": time.time() + token_data.get("expires_in", 0),
                        "obtained_at": datetime.now(UTC).isoformat(),
                    }
        finally:
            server.server_close()

        raise TimeoutError("ChatGPT auth timed out waiting for browser callback.")


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_chatgpt_authorize_url(
    *,
    code_challenge: str,
    state: str,
    redirect_uri: str = CHATGPT_REDIRECT_URI,
) -> str:
    params = {
        "response_type": "code",
        "client_id": CHATGPT_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": CHATGPT_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "maurice",
    }
    return f"{CHATGPT_AUTHORIZE_URL}?{parse.urlencode(params)}"


def refresh_chatgpt_auth(refresh_token: str, *, transport: Any = None) -> dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CHATGPT_CLIENT_ID,
    }
    return _token_request(payload, transport=transport)


def exchange_chatgpt_code(
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": CHATGPT_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    return _token_request(payload, transport=transport)


def _token_request(payload: dict[str, Any], *, transport: Any = None) -> dict[str, Any]:
    if transport is not None:
        return transport(CHATGPT_TOKEN_URL, payload)
    body = parse.urlencode(payload).encode("utf-8")
    req = request.Request(
        CHATGPT_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _ChatGPTOAuthHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    expected_state: str = ""

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        params = parse.parse_qs(parsed.query)
        if parsed.path != "/auth/callback":
            self._respond(404, b"Not found")
            return
        state = params.get("state", [None])[0]
        if state != self.__class__.expected_state:
            self._respond(400, b"<h1>State mismatch</h1>")
            return
        code = params.get("code", [None])[0]
        if not code:
            self._respond(400, b"<h1>Missing code</h1>")
            return
        self.__class__.code = code
        self._respond(200, _SUCCESS_HTML)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


_SUCCESS_HTML = b"""<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px">
<h1 style="color:#10a37f">Authentication successful</h1>
<p>You can close this window and return to Maurice.</p>
</body></html>"""
