"""Approval lifecycle and replay fingerprints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import (
    MauriceModel,
    PendingApproval,
    PendingApprovalStatus,
    PermissionClass,
)
from maurice.kernel.events import EventStore


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_approval_id() -> str:
    return f"approval_{uuid4().hex}"


def normalized_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def arguments_hash(arguments: dict) -> str:
    digest = hashlib.sha256(normalized_json(arguments).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def replay_fingerprint(
    *,
    permission_class: PermissionClass | str,
    scope: dict,
    tool_name: str,
    arguments_hash_value: str,
) -> str:
    digest = hashlib.sha256(
        normalized_json(
            {
                "permission_class": str(PermissionClass(permission_class)),
                "scope": scope,
                "tool_name": tool_name,
                "arguments_hash": arguments_hash_value,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


class ApprovalEnvelope(MauriceModel):
    approval: PendingApproval
    replay_fingerprint: str


class ApprovalStoreFile(MauriceModel):
    approvals: list[ApprovalEnvelope] = Field(default_factory=list)


class ApprovalStore:
    """Mutable pending approval store.

    The event log remains append-only; this file holds current approval state so
    the runtime can resolve and replay approvals efficiently.
    """

    def __init__(self, path: str | Path, event_store: EventStore | None = None) -> None:
        self.path = Path(path).expanduser()
        self.event_store = event_store

    def request(
        self,
        *,
        agent_id: str,
        session_id: str,
        correlation_id: str,
        tool_name: str,
        permission_class: PermissionClass | str,
        scope: dict,
        arguments: dict,
        summary: str,
        reason: str,
        ttl_seconds: int = 1800,
        rememberable: bool = False,
    ) -> PendingApproval:
        hashed_arguments = arguments_hash(arguments)
        approval = PendingApproval(
            id=new_approval_id(),
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            tool_name=tool_name,
            permission_class=PermissionClass(permission_class),
            scope=scope,
            arguments_hash=hashed_arguments,
            summary=summary,
            reason=reason,
            created_at=utc_now(),
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
            rememberable=rememberable,
            status=PendingApprovalStatus.PENDING,
        )
        envelope = ApprovalEnvelope(
            approval=approval,
            replay_fingerprint=replay_fingerprint(
                permission_class=approval.permission_class,
                scope=approval.scope,
                tool_name=approval.tool_name,
                arguments_hash_value=approval.arguments_hash,
            ),
        )
        state = self._load()
        state.approvals.append(envelope)
        self._save(state)
        self._emit("approval.requested", approval)
        return approval

    def resolve(self, approval_id: str, status: PendingApprovalStatus | str) -> PendingApproval:
        resolved_status = PendingApprovalStatus(status)
        if resolved_status not in (
            PendingApprovalStatus.APPROVED,
            PendingApprovalStatus.DENIED,
            PendingApprovalStatus.EXPIRED,
        ):
            raise ValueError("approval can only resolve to approved, denied, or expired")

        state = self._load()
        for envelope in state.approvals:
            if envelope.approval.id == approval_id:
                envelope.approval.status = resolved_status
                self._save(state)
                self._emit("approval.resolved", envelope.approval)
                return envelope.approval
        raise KeyError(approval_id)

    def approve(self, approval_id: str) -> PendingApproval:
        return self.resolve(approval_id, PendingApprovalStatus.APPROVED)

    def deny(self, approval_id: str) -> PendingApproval:
        return self.resolve(approval_id, PendingApprovalStatus.DENIED)

    def remember(
        self,
        *,
        agent_id: str,
        session_id: str,
        tool_name: str,
        permission_class: PermissionClass | str,
        scope: dict,
        arguments: dict,
        ttl_seconds: int = 600,
    ) -> None:
        """Record an already-approved action so it can be replayed without re-asking."""
        hashed_arguments = arguments_hash(arguments)
        approval = PendingApproval(
            id=new_approval_id(),
            agent_id=agent_id,
            session_id=session_id,
            correlation_id="callback",
            tool_name=tool_name,
            permission_class=PermissionClass(permission_class),
            scope=scope,
            arguments_hash=hashed_arguments,
            summary=f"Approved via callback: {tool_name}",
            reason="User approved interactively.",
            created_at=utc_now(),
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
            rememberable=True,
            status=PendingApprovalStatus.APPROVED,
        )
        envelope = ApprovalEnvelope(
            approval=approval,
            replay_fingerprint=replay_fingerprint(
                permission_class=approval.permission_class,
                scope=approval.scope,
                tool_name=approval.tool_name,
                arguments_hash_value=approval.arguments_hash,
            ),
        )
        state = self._load()
        state.approvals.append(envelope)
        self._save(state)

    def list(self, *, status: PendingApprovalStatus | str | None = None) -> list[PendingApproval]:
        approvals = [envelope.approval for envelope in self._load().approvals]
        if status is None:
            return approvals
        expected = PendingApprovalStatus(status)
        return [approval for approval in approvals if approval.status == expected]

    def approved_for_replay(
        self,
        *,
        permission_class: PermissionClass | str,
        scope: dict,
        tool_name: str,
        arguments: dict,
        now: datetime | None = None,
    ) -> PendingApproval | None:
        checked_at = now or utc_now()
        hashed_arguments = arguments_hash(arguments)
        fingerprint = replay_fingerprint(
            permission_class=permission_class,
            scope=scope,
            tool_name=tool_name,
            arguments_hash_value=hashed_arguments,
        )
        for envelope in self._load().approvals:
            approval = envelope.approval
            if envelope.replay_fingerprint != fingerprint:
                continue
            if approval.status != PendingApprovalStatus.APPROVED:
                continue
            if approval.expires_at <= checked_at:
                continue
            return approval
        return None

    def _load(self) -> ApprovalStoreFile:
        if not self.path.exists():
            return ApprovalStoreFile()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return ApprovalStoreFile.model_validate(data)

    def _save(self, state: ApprovalStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _emit(self, name: str, approval: PendingApproval) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="audit",
            origin="kernel",
            agent_id=approval.agent_id,
            session_id=approval.session_id,
            correlation_id=approval.correlation_id,
            payload={
                "approval_id": approval.id,
                "tool_name": approval.tool_name,
                "permission_class": approval.permission_class,
                "status": approval.status,
            },
        )
