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
    assert [event.name for event in events.read_all()] == ["reminder.created"]
    assert (tmp_path / "workspace" / "artifacts" / "reminders" / "reminders.json").is_file()


def test_reminder_list_cancel_and_fire(tmp_path) -> None:
    permission_context = context(tmp_path)
    run_at = datetime.now(UTC)
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
    assert fired.data["reminder"]["status"] == "delivered"
    assert ReminderStore(tmp_path / "workspace" / "artifacts" / "reminders" / "reminders.json").list(
        status="delivered"
    )[0].text == "Stand up"


def test_reminder_scheduler_handler_can_fire_due_job(tmp_path) -> None:
    permission_context = context(tmp_path)
    event_store = EventStore(tmp_path / "events.jsonl")
    created = create(
        {"text": "Ping", "run_at": datetime.now(UTC).isoformat()},
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
