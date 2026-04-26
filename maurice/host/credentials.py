"""Credential loading.

Credentials are deliberately separate from normal config. The typed records may
reference secrets, but the kernel config loaders do not read this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["api_key", "token", "url", "password", "opaque"]
    value: str = Field(default="", repr=False)
    base_url: str | None = None


class CredentialsStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credentials: dict[str, CredentialRecord] = Field(default_factory=dict)

    def visible_to(self, allowed_names: list[str]) -> "CredentialsStore":
        if "*" in allowed_names:
            return self
        return CredentialsStore(
            credentials={
                name: record
                for name, record in self.credentials.items()
                if name in set(allowed_names)
            }
        )


def load_credentials(path: str | Path) -> CredentialsStore:
    credential_path = Path(path).expanduser()
    if not credential_path.exists():
        return CredentialsStore()
    data = yaml.safe_load(credential_path.read_text(encoding="utf-8")) or {}
    return CredentialsStore.model_validate(data)


def write_credentials(path: str | Path, store: CredentialsStore) -> None:
    credential_path = Path(path).expanduser()
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    credential_path.write_text(
        yaml.safe_dump(store.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    credential_path.chmod(0o600)
