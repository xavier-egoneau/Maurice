from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.scheduler import JobRunner, JobStore
from maurice.system_skills.reminders.tools import (
    ReminderStore,
    cancel,
    create,
    fire_reminder,
    list_reminders,
)


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def test_reminder_create_persists_and_schedules_job(tmp_path) -> None:
    permission_context = context(tmp_path)
    job_store = JobStore(tmp_path / "workspace" / "agents" / "main" / "jobs.json")
    events = EventStore(tmp_path / "events.jsonl")
    run_at = datetime.now(UTC) + timedelta(minutes=10)

    def schedule(payload):
        job = job_store.schedule(
            name="reminders.fire",
            run_at=payload["run_at"],
            owner="skill:reminders",
            payload={"arguments": {"reminder_id": payload["reminder_id"]}},
            interval_seconds=payload.get("interval_seconds"),
        )
        return job.id

    result = create(
        {"text": "Take a break", "run_at": run_at.isoformat()},
        permission_context,
        event_store=events,
        schedule_reminder=schedule,
    )

    reminder = result.data["reminder"]
    jobs = job_store.list()
    assert result.ok is True
    assert reminder["text"] == "Take a break"
    assert reminder["job_id"] == jobs[0].id
    assert result.summary.startswith("Ok, le rappel est prêt pour ")
    assert [event.name for event in events.read_all()] == ["reminder.created"]
    assert (tmp_path / "workspace" / "content" / "reminders" / "reminders.json").is_file()


def test_reminder_list_cancel_and_fire(tmp_path) -> None:
    permission_context = context(tmp_path)
    run_at = datetime.now(UTC) + timedelta(seconds=10)
    created = create(
        {"text": "Ship it", "run_at": run_at.isoformat()},
        permission_context,
    )
    reminder_id = created.data["reminder"]["id"]

    listed = list_reminders({}, permission_context)
    cancelled = cancel({"reminder_id": reminder_id}, permission_context)

    assert listed.data["reminders"][0]["id"] == reminder_id
    assert cancelled.data["reminder"]["status"] == "cancelled"

    second = create(
        {"text": "Stand up", "run_at": run_at.isoformat()},
        permission_context,
    )
    fired = fire_reminder(
        {"reminder_id": second.data["reminder"]["id"]},
        permission_context,
    )

    assert fired.ok is True
    assert fired.summary == "🔔 Stand up"
    assert fired.data["reminder"]["status"] == "delivered"
    assert ReminderStore(tmp_path / "workspace" / "content" / "reminders" / "reminders.json").list(
        status="delivered"
    )[0].text == "Stand up"


def test_reminder_scheduler_handler_can_fire_due_job(tmp_path) -> None:
    permission_context = context(tmp_path)
    event_store = EventStore(tmp_path / "events.jsonl")
    created = create(
        {"text": "Ping", "run_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat()},
        permission_context,
    )
    reminder_id = created.data["reminder"]["id"]
    job_store = JobStore(tmp_path / "jobs.json", event_store=event_store)
    job_store.schedule(
        name="reminders.fire",
        run_at=datetime.now(UTC),
        owner="skill:reminders",
        payload={"arguments": {"reminder_id": reminder_id}},
    )

    def run_reminder(job):
        result = fire_reminder(job.payload["arguments"], permission_context, event_store=event_store)
        if not result.ok:
            raise RuntimeError(result.summary)

    result_jobs = JobRunner(job_store, {"reminders.fire": run_reminder}).run_due()

    assert result_jobs[0].status == "completed"
    assert "reminder.fired" in [event.name for event in event_store.read_all()]


def test_reminder_create_rejects_old_past_date(tmp_path) -> None:
    result = create(
        {"text": "Sleep", "run_at": "2025-01-20T02:13:00+00:00"},
        context(tmp_path),
    )

    assert not result.ok
    assert result.error.code == "invalid_arguments"
    assert "future schedule" in result.summary


def test_reminder_create_accepts_jarvis_like_at_trigger(tmp_path) -> None:
    permission_context = context(tmp_path)
    result = create(
        {"text": "Stretch", "trigger_type": "at", "trigger_value": "10m"},
        permission_context,
    )

    reminder = result.data["reminder"]
    assert result.ok is True
    assert reminder["trigger_type"] == "at"
    assert reminder["trigger_value"] == "10m"
    assert datetime.fromisoformat(reminder["run_at"]) > datetime.now(UTC)


def test_recurring_reminder_uses_interval_and_stays_scheduled_after_fire(tmp_path) -> None:
    permission_context = context(tmp_path)
    job_store = JobStore(tmp_path / "workspace" / "agents" / "main" / "jobs.json")

    def schedule(payload):
        job = job_store.schedule(
            name="reminders.fire",
            run_at=payload["run_at"],
            owner="skill:reminders",
            payload={"arguments": {"reminder_id": payload["reminder_id"]}},
            interval_seconds=payload.get("interval_seconds"),
        )
        return job.id

    created = create(
        {"text": "Drink water", "trigger_type": "every", "trigger_value": "2h"},
        permission_context,
        schedule_reminder=schedule,
    )
    reminder_id = created.data["reminder"]["id"]

    jobs = job_store.list()
    fired = fire_reminder({"reminder_id": reminder_id}, permission_context)

    assert jobs[0].interval_seconds == 7200
    assert created.data["reminder"]["interval_seconds"] == 7200
    assert "puis toutes les 2 heures" in created.summary
    assert fired.data["reminder"]["status"] == "scheduled"
