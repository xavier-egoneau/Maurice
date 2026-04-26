"""Session history storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import MauriceModel


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_session_id() -> str:
    return f"sess_{uuid4().hex}"


def new_correlation_id(prefix: str = "turn") -> str:
    return f"{prefix}_{uuid4().hex}"


class SessionMessage(MauriceModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    created_at: datetime = Field(default_factory=utc_now)
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(MauriceModel):
    id: str = Field(default_factory=lambda: new_correlation_id("turn"))
    correlation_id: str = Field(default_factory=lambda: new_correlation_id("turn"))
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    status: Literal["running", "completed", "failed"] = "running"
    message_indices: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(MauriceModel):
    id: str
    agent_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    messages: list[SessionMessage] = Field(default_factory=list)
    turns: list[TurnRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionStore:
    """Persist per-agent sessions as JSON documents."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def session_path(self, agent_id: str, session_id: str) -> Path:
        return self.root / agent_id / f"{session_id}.json"

    def create(self, agent_id: str, session_id: str | None = None) -> SessionRecord:
        session = SessionRecord(id=session_id or new_session_id(), agent_id=agent_id)
        self.save(session)
        return session

    def load(self, agent_id: str, session_id: str) -> SessionRecord:
        path = self.session_path(agent_id, session_id)
        if not path.exists():
            raise FileNotFoundError(path)
        return SessionRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, session: SessionRecord) -> SessionRecord:
        session.updated_at = utc_now()
        path = self.session_path(session.agent_id, session.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        return session

    def append_message(
        self,
        agent_id: str,
        session_id: str,
        *,
        role: Literal["system", "user", "assistant", "tool"],
        content: str,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        session = self.load(agent_id, session_id)
        message = SessionMessage(
            role=role,
            content=content,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        session.messages.append(message)
        self.save(session)
        return message

    def start_turn(
        self,
        agent_id: str,
        session_id: str,
        *,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnRecord:
        session = self.load(agent_id, session_id)
        turn = TurnRecord(
            correlation_id=correlation_id or new_correlation_id("turn"),
            metadata=metadata or {},
        )
        session.turns.append(turn)
        self.save(session)
        return turn

    def complete_turn(
        self,
        agent_id: str,
        session_id: str,
        correlation_id: str,
        *,
        status: Literal["completed", "failed"] = "completed",
    ) -> TurnRecord:
        session = self.load(agent_id, session_id)
        for turn in reversed(session.turns):
            if turn.correlation_id == correlation_id:
                turn.status = status
                turn.completed_at = utc_now()
                self.save(session)
                return turn
        raise KeyError(correlation_id)

    def reset(self, agent_id: str, session_id: str) -> SessionRecord:
        existing = self.load(agent_id, session_id)
        reset_session = SessionRecord(
            id=existing.id,
            agent_id=existing.agent_id,
            created_at=existing.created_at,
            metadata=existing.metadata,
        )
        self.save(reset_session)
        return reset_session
