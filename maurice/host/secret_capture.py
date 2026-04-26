"""Host-owned secret capture for conversational channels."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from maurice.host.credentials import CredentialRecord, load_credentials, write_credentials


class SecretCaptureRequest(BaseModel):
    session_id: str
    agent_id: str
    credential: str
    provider: str
    type: str = "token"
    prompt: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SecretCaptureStore(BaseModel):
    requests: list[SecretCaptureRequest] = Field(default_factory=list)


def request_secret_capture(
    workspace_root: str | Path,
    *,
    agent_id: str,
    session_id: str,
    credential: str,
    provider: str,
    secret_type: str = "token",
    prompt: str = "",
) -> SecretCaptureRequest:
    workspace = Path(workspace_root).expanduser().resolve()
    store = _load_store(workspace)
    request = SecretCaptureRequest(
        session_id=session_id,
        agent_id=agent_id,
        credential=credential,
        provider=provider,
        type=secret_type,
        prompt=prompt,
    )
    store.requests = [
        item
        for item in store.requests
        if not (item.session_id == session_id and item.agent_id == agent_id)
    ]
    store.requests.append(request)
    _write_store(workspace, store)
    return request


def capture_pending_secret(
    workspace_root: str | Path,
    *,
    agent_id: str,
    session_id: str,
    value: str,
) -> SecretCaptureRequest | None:
    workspace = Path(workspace_root).expanduser().resolve()
    store = _load_store(workspace)
    request = next(
        (
            item
            for item in store.requests
            if item.session_id == session_id and item.agent_id == agent_id
        ),
        None,
    )
    if request is None:
        return None

    credentials_path = workspace / "credentials.yaml"
    credentials = load_credentials(credentials_path)
    credentials.credentials[request.credential] = CredentialRecord(
        type=request.type, value=value.strip(), provider=request.provider
    )
    write_credentials(credentials_path, credentials)

    store.requests = [
        item
        for item in store.requests
        if not (item.session_id == session_id and item.agent_id == agent_id)
    ]
    _write_store(workspace, store)
    return request


def list_secret_captures(workspace_root: str | Path) -> list[SecretCaptureRequest]:
    return _load_store(Path(workspace_root).expanduser().resolve()).requests


def _store_path(workspace: Path) -> Path:
    return workspace / "agents" / ".secret_capture.json"


def _load_store(workspace: Path) -> SecretCaptureStore:
    path = _store_path(workspace)
    if not path.exists():
        return SecretCaptureStore()
    try:
        import yaml

        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return SecretCaptureStore.model_validate(data)
    except Exception:
        return SecretCaptureStore()


def _write_store(workspace: Path, store: SecretCaptureStore) -> None:
    import yaml

    path = _store_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(store.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
