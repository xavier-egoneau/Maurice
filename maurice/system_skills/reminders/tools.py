"""Reminder system skill tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import MauriceModel, ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext

ScheduleReminder = Callable[[dict[str, Any]], str]
CancelReminderJob = Callable[[str], None]


class Reminder(MauriceModel):
    id: str
    text: str
    run_at: datetime
    status: str = "scheduled"
    job_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None


class ReminderStoreFile(MauriceModel):
    reminders: list[Reminder] = Field(default_factory=list)


class ReminderStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def create(self, *, text: str, run_at: datetime, job_id: str | None = None) -> Reminder:
        reminder = Reminder(
            id=f"reminder_{uuid4().hex}",
            text=text,
            run_at=run_at,
            job_id=job_id,
        )
        state = self._load()
        state.reminders.append(reminder)
        self._save(state)
        return reminder

    def list(self, *, status: str | None = None) -> list[Reminder]:
        reminders = self._load().reminders
        if status is None:
            return reminders
        return [reminder for reminder in reminders if reminder.status == status]

    def update_job(self, reminder_id: str, job_id: str) -> Reminder:
        state = self._load()
        for reminder in state.reminders:
            if reminder.id == reminder_id:
                reminder.job_id = job_id
                self._save(state)
                return reminder
        raise KeyError(reminder_id)

    def cancel(self, reminder_id: str) -> Reminder:
        return self._update(
            reminder_id,
            status="cancelled",
            cancelled_at=datetime.now(UTC),
        )

    def deliver(self, reminder_id: str) -> Reminder:
        return self._update(
            reminder_id,
            status="delivered",
            delivered_at=datetime.now(UTC),
        )

    def _update(self, reminder_id: str, **updates: Any) -> Reminder:
        state = self._load()
        for reminder in state.reminders:
            if reminder.id == reminder_id:
                for key, value in updates.items():
                    setattr(reminder, key, value)
                self._save(state)
                return reminder
        raise KeyError(reminder_id)

    def _load(self) -> ReminderStoreFile:
        if not self.path.exists():
            return ReminderStoreFile()
        return ReminderStoreFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, state: ReminderStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def reminders_tool_executors(
    context: PermissionContext,
    *,
    event_store: EventStore | None = None,
    schedule_reminder: ScheduleReminder | None = None,
    cancel_job: CancelReminderJob | None = None,
) -> dict[str, Any]:
    return {
        "reminders.create": lambda arguments: create(
            arguments,
            context,
            event_store=event_store,
            schedule_reminder=schedule_reminder,
        ),
        "reminders.list": lambda arguments: list_reminders(arguments, context),
        "reminders.cancel": lambda arguments: cancel(
            arguments,
            context,
            event_store=event_store,
            cancel_job=cancel_job,
        ),
        "maurice.system_skills.reminders.tools.create": lambda arguments: create(
            arguments,
            context,
            event_store=event_store,
            schedule_reminder=schedule_reminder,
        ),
        "maurice.system_skills.reminders.tools.list_reminders": lambda arguments: list_reminders(arguments, context),
        "maurice.system_skills.reminders.tools.cancel": lambda arguments: cancel(
            arguments,
            context,
            event_store=event_store,
            cancel_job=cancel_job,
        ),
    }


def create(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    event_store: EventStore | None = None,
    schedule_reminder: ScheduleReminder | None = None,
) -> ToolResult:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("invalid_arguments", "reminders.create requires non-empty text.")
    try:
        run_at = _parse_datetime(arguments.get("run_at"))
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))

    store = ReminderStore(_store_path(context))
    reminder = store.create(text=text.strip(), run_at=run_at)
    if schedule_reminder is not None:
        job_id = schedule_reminder(
            {
                "reminder_id": reminder.id,
                "text": reminder.text,
                "run_at": reminder.run_at,
            }
        )
        reminder = store.update_job(reminder.id, job_id)
    _emit(event_store, "reminder.created", reminder)
    return ToolResult(
        ok=True,
        summary=f"Reminder scheduled: {reminder.text}",
        data={"reminder": reminder.model_dump(mode="json")},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "reminder.created", "payload": {"reminder_id": reminder.id}}],
        error=None,
    )


def list_reminders(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    status = arguments.get("status")
    if status is not None and not isinstance(status, str):
        return _error("invalid_arguments", "reminders.list status must be a string.")
    reminders = ReminderStore(_store_path(context)).list(status=status)
    return ToolResult(
        ok=True,
        summary=f"Found {len(reminders)} reminder(s).",
        data={"reminders": [reminder.model_dump(mode="json") for reminder in reminders]},
        trust="local_mutable",
        artifacts=[],
        events=[],
        error=None,
    )


def cancel(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    event_store: EventStore | None = None,
    cancel_job: CancelReminderJob | None = None,
) -> ToolResult:
    reminder_id = arguments.get("reminder_id")
    if not isinstance(reminder_id, str) or not reminder_id:
        return _error("invalid_arguments", "reminders.cancel requires reminder_id.")
    store = ReminderStore(_store_path(context))
    try:
        reminder = store.cancel(reminder_id)
    except KeyError:
        return _error("not_found", f"Unknown reminder: {reminder_id}")
    if cancel_job is not None and reminder.job_id:
        cancel_job(reminder.job_id)
    _emit(event_store, "reminder.cancelled", reminder)
    return ToolResult(
        ok=True,
        summary=f"Reminder cancelled: {reminder.id}",
        data={"reminder": reminder.model_dump(mode="json")},
        trust="local_mutable",
        artifacts=[{"type": "file", "path": str(_store_path(context))}],
        events=[{"name": "reminder.cancelled", "payload": {"reminder_id": reminder.id}}],
        error=None,
    )


def fire_reminder(
    arguments: dict[str, Any],
    context: PermissionContext,
    *,
    event_store: EventStore | None = None,
) -> ToolResult:
    reminder_id = arguments.get("reminder_id")
    if not isinstance(reminder_id, str) or not reminder_id:
        return _error("invalid_arguments", "reminders.fire requires reminder_id.")
    try:
        reminder = ReminderStore(_store_path(context)).deliver(reminder_id)
    except KeyError:
        return _error("not_found", f"Unknown reminder: {reminder_id}")
    _emit(event_store, "reminder.fired", reminder)
    return ToolResult(
        ok=True,
        summary=f"Reminder: {reminder.text}",
        data={"reminder": reminder.model_dump(mode="json")},
        trust="local_mutable",
        artifacts=[],
        events=[{"name": "reminder.fired", "payload": {"reminder_id": reminder.id}}],
        error=None,
    )


def _store_path(context: PermissionContext) -> Path:
    return Path(context.variables()["$workspace"]) / "content" / "reminders" / "reminders.json"


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_at must be an ISO datetime string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("run_at must be an ISO datetime string.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _emit(event_store: EventStore | None, name: str, reminder: Reminder) -> None:
    if event_store is None:
        return
    event_store.emit(
        name=name,
        origin="skill:reminders",
        agent_id="system",
        session_id="reminders",
        correlation_id=reminder.id,
        payload={
            "reminder_id": reminder.id,
            "status": reminder.status,
            "job_id": reminder.job_id,
            "run_at": reminder.run_at.isoformat(),
        },
    )


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data={},
        trust="local_mutable",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": False},
    )
