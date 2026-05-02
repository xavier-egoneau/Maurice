"""Reminder system skill tools."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    trigger_type: str = "at"
    trigger_value: str | None = None
    interval_seconds: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    last_delivered_at: datetime | None = None
    cancelled_at: datetime | None = None


class ReminderStoreFile(MauriceModel):
    reminders: list[Reminder] = Field(default_factory=list)


class ReminderStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def create(
        self,
        *,
        text: str,
        run_at: datetime,
        job_id: str | None = None,
        trigger_type: str = "at",
        trigger_value: str | None = None,
        interval_seconds: int | None = None,
    ) -> Reminder:
        reminder = Reminder(
            id=f"reminder_{uuid4().hex}",
            text=text,
            run_at=run_at,
            job_id=job_id,
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            interval_seconds=interval_seconds,
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
        state = self._load()
        delivered_at = datetime.now(UTC)
        for reminder in state.reminders:
            if reminder.id != reminder_id:
                continue
            if reminder.interval_seconds:
                reminder.status = "scheduled"
                reminder.last_delivered_at = delivered_at
                reminder.delivered_at = delivered_at
            else:
                reminder.status = "delivered"
                reminder.delivered_at = delivered_at
                reminder.last_delivered_at = delivered_at
            self._save(state)
            return reminder
        raise KeyError(reminder_id)

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


def build_executors(ctx: Any) -> dict[str, Any]:
    return reminders_tool_executors(
        ctx.permission_context,
        event_store=ctx.event_store,
        schedule_reminder=ctx.hooks.schedule_reminder,
        cancel_job=ctx.hooks.cancel_job,
    )


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
        run_at, trigger_type, trigger_value, interval_seconds = _resolve_schedule(arguments)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if run_at < datetime.now(UTC) - timedelta(seconds=60):
        return _error(
            "invalid_arguments",
            "reminders.create requires a future schedule. Use the current date and local timezone when the user gives a time.",
        )

    store = ReminderStore(_store_path(context))
    reminder = store.create(
        text=text.strip(),
        run_at=run_at,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        interval_seconds=interval_seconds,
    )
    if schedule_reminder is not None:
        job_id = schedule_reminder(
            {
                "reminder_id": reminder.id,
                "text": reminder.text,
                "run_at": reminder.run_at,
                "interval_seconds": reminder.interval_seconds,
            }
        )
        reminder = store.update_job(reminder.id, job_id)
    _emit(event_store, "reminder.created", reminder)
    return ToolResult(
        ok=True,
        summary=_confirmation_message(reminder),
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
        summary=f"🔔 {reminder.text}",
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
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(UTC)


def _resolve_schedule(arguments: dict[str, Any]) -> tuple[datetime, str, str | None, int | None]:
    trigger_type = arguments.get("trigger_type")
    trigger_value = arguments.get("trigger_value")
    interval_seconds = _optional_positive_int(arguments.get("interval_seconds"), field="interval_seconds")

    if isinstance(trigger_type, str) or isinstance(trigger_value, str):
        if not isinstance(trigger_type, str) or not isinstance(trigger_value, str):
            raise ValueError("reminders.create requires both trigger_type and trigger_value.")
        trigger_type = trigger_type.strip().lower()
        trigger_value = trigger_value.strip()
        if trigger_type == "at":
            run_at = _parse_at(trigger_value)
            if run_at is None:
                raise ValueError("trigger_value must be a relative delay, wall-clock time, or ISO datetime.")
            return run_at, "at", trigger_value, interval_seconds
        if trigger_type == "every":
            seconds = _parse_interval(trigger_value)
            if seconds is None:
                raise ValueError("trigger_value for every must look like 20m, 2h, or 1d.")
            return datetime.now(UTC) + timedelta(seconds=seconds), "every", trigger_value, seconds
        raise ValueError("trigger_type must be 'at' or 'every'.")

    run_at = _parse_datetime(arguments.get("run_at"))
    return run_at, "at", str(arguments.get("run_at")), interval_seconds


def _parse_at(value: str) -> datetime | None:
    raw = value.strip()
    seconds = _parse_interval(raw)
    if seconds:
        return datetime.now(UTC) + timedelta(seconds=seconds)
    wall_clock = _parse_wall_clock_at(raw)
    if wall_clock is not None:
        return wall_clock.astimezone(UTC)
    try:
        return _parse_datetime(raw)
    except ValueError:
        return None


def _parse_wall_clock_at(value: str) -> datetime | None:
    colon = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    compact = re.fullmatch(r"(\d{1,2})h(?:(\d{2}))?", value, re.IGNORECASE)
    match = colon or compact
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    now_local = datetime.now().astimezone()
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def _parse_interval(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)(m|h|d)", value.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    return amount * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer.")
    return value


def _confirmation_message(reminder: Reminder) -> str:
    local_time = _human_local_time(reminder.run_at.astimezone())
    if reminder.interval_seconds:
        return f"Ok, le rappel est prêt pour {local_time}, puis toutes les {_format_interval_fr(reminder.interval_seconds)}."
    return f"Ok, le rappel est prêt pour {local_time}."


def _human_local_time(value: datetime) -> str:
    now = datetime.now().astimezone()
    if value.date() == now.date():
        return value.strftime("%H:%M")
    if value.date() == (now + timedelta(days=1)).date():
        return f"demain à {value:%H:%M}"
    month = _MONTHS_FR[value.month - 1]
    if value.year == now.year:
        return f"le {value.day} {month} à {value:%H:%M}"
    return f"le {value.day} {month} {value.year} à {value:%H:%M}"


def _format_interval_fr(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} jour" if days == 1 else f"{days} jours"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} heure" if hours == 1 else f"{hours} heures"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} secondes"


_MONTHS_FR = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]


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
