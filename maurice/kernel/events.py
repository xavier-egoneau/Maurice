"""Append-only JSONL event store."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from maurice.kernel.contracts import Event, EventKind


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    return f"evt_{uuid4().hex}"


class EventStore:
    """Append and read Maurice event envelopes from a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def append(self, event: Event) -> Event:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json())
            stream.write("\n")
        return event

    def emit(
        self,
        *,
        name: str,
        origin: str,
        agent_id: str,
        session_id: str,
        kind: EventKind | str = EventKind.FACT,
        correlation_id: str | None = None,
        payload: dict | None = None,
    ) -> Event:
        return self.append(
            Event(
                id=new_event_id(),
                time=utc_now(),
                kind=kind,
                name=name,
                origin=origin,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload=payload or {},
            )
        )

    def read_all(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        names: Iterable[str] | None = None,
    ) -> list[Event]:
        if not self.path.exists():
            return []

        name_filter = set(names) if names is not None else None
        events: list[Event] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = Event.model_validate(json.loads(line))
            if agent_id is not None and event.agent_id != agent_id:
                continue
            if session_id is not None and event.session_id != session_id:
                continue
            if correlation_id is not None and event.correlation_id != correlation_id:
                continue
            if name_filter is not None and event.name not in name_filter:
                continue
            events.append(event)
        return events
