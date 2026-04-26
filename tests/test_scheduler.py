from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maurice.kernel.events import EventStore
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService


def test_job_store_schedules_lists_and_finds_due_jobs(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    due = store.schedule(name="dreaming.run", run_at=now - timedelta(seconds=1))
    later = store.schedule(name="cleanup", run_at=now + timedelta(seconds=60))

    assert [job.id for job in store.list()] == [due.id, later.id]
    assert [job.id for job in store.due(now=now)] == [due.id]


def test_job_store_lifecycle_emits_events(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = JobStore(tmp_path / "jobs.json", event_store=event_store)
    job = store.schedule(
        name="dreaming.run",
        owner="dreaming",
        run_at=datetime(2026, 4, 26, tzinfo=UTC),
        payload={"agent_id": "main", "session_id": "dreaming"},
    )

    store.mark_running(job.id)
    store.complete(job.id)

    assert store.list()[0].status == JobStatus.COMPLETED
    assert [event.name for event in event_store.read_all()] == [
        "job.scheduled",
        "job.started",
        "job.completed",
    ]


def test_recurring_job_is_rescheduled_on_completion(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    job = store.schedule(name="dreaming.run", run_at=now, interval_seconds=300)

    completed = store.complete(job.id, now=now)

    assert completed.status == JobStatus.SCHEDULED
    assert completed.run_at == now + timedelta(seconds=300)


def test_job_store_can_fail_and_cancel_jobs(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    failed = store.schedule(name="dreaming.run", run_at=now)
    cancelled = store.schedule(name="cleanup", run_at=now)

    store.fail(failed.id, "boom")
    store.cancel(cancelled.id)

    jobs = {job.id: job for job in store.list()}
    assert jobs[failed.id].status == JobStatus.FAILED
    assert jobs[failed.id].last_error == "boom"
    assert jobs[cancelled.id].status == JobStatus.CANCELLED


def test_job_runner_executes_due_jobs_with_registered_handler(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    job = store.schedule(name="dreaming.run", run_at=now, payload={"skills": ["memory"]})
    calls = []

    runner = JobRunner(store, {"dreaming.run": lambda scheduled: calls.append(scheduled.payload)})

    results = runner.run_due(now=now)

    assert calls == [{"skills": ["memory"]}]
    assert results[0].id == job.id
    assert results[0].status == JobStatus.COMPLETED


def test_job_runner_fails_jobs_without_handler(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    job = store.schedule(name="missing", run_at=now)

    results = JobRunner(store, {}).run_due(now=now)

    assert results[0].id == job.id
    assert results[0].status == JobStatus.FAILED
    assert "No handler" in results[0].last_error


def test_job_runner_reschedules_recurring_job_after_handler(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    job = store.schedule(name="dreaming.run", run_at=now, interval_seconds=60)

    results = JobRunner(store, {"dreaming.run": lambda _job: None}).run_due(now=now)

    assert results[0].id == job.id
    assert results[0].status == JobStatus.SCHEDULED
    assert results[0].run_at == now + timedelta(seconds=60)


def test_scheduler_service_polls_runner_until_max_iterations(tmp_path) -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    store = JobStore(tmp_path / "jobs.json")
    store.schedule(name="dreaming.run", run_at=now - timedelta(seconds=1))
    calls = []
    sleeps = []
    runner = JobRunner(store, {"dreaming.run": lambda job: calls.append(job.id)})
    service = SchedulerService(
        runner,
        poll_interval_seconds=0.5,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    iterations = service.run_forever(max_iterations=2)

    assert iterations == 2
    assert len(calls) == 1
    assert sleeps == [0.5]


def test_scheduler_service_can_be_stopped_by_sleep_callback(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.json")
    service = SchedulerService(
        JobRunner(store, {}),
        poll_interval_seconds=0.5,
        sleep=lambda _seconds: service.stop(),
    )

    assert service.run_forever() == 1
