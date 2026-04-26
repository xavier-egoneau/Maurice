"""Credential loading.

Credentials are deliberately separate from normal config. The typed records may
reference secrets, but the kernel config loaders do not read this file.
"""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from maurice.host.paths import maurice_home


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


def credentials_path() -> Path:
    return maurice_home() / "credentials.yaml"


def legacy_credentials_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "credentials.yaml"


def load_workspace_credentials(workspace_root: str | Path) -> CredentialsStore:
    return load_credentials(ensure_workspace_credentials_migrated(workspace_root))


def write_workspace_credentials(workspace_root: str | Path, store: CredentialsStore) -> None:
    path = ensure_workspace_credentials_migrated(workspace_root)
    write_credentials(path, store)


def ensure_workspace_credentials_migrated(workspace_root: str | Path) -> Path:
    """Move legacy workspace credentials into the host-owned Maurice home."""
    canonical = credentials_path()
    legacy = legacy_credentials_path(workspace_root)
    if legacy.exists():
        if not canonical.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(canonical))
            canonical.chmod(0o600)
        else:
            canonical_store = load_credentials(canonical)
            legacy_store = load_credentials(legacy)
            changed = False
            for name, record in legacy_store.credentials.items():
                if name not in canonical_store.credentials:
                    canonical_store.credentials[name] = record
                    changed = True
            if changed:
                write_credentials(canonical, canonical_store)
            migrated = legacy.with_name("credentials.yaml.migrated")
            if migrated.exists():
                legacy.unlink()
            else:
                legacy.rename(migrated)
                migrated.chmod(0o600)
    if not canonical.exists():
        write_credentials(canonical, CredentialsStore())
    return canonical


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
