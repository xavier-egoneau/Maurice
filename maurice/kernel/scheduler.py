"""Generic background job scheduling primitives."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import MauriceModel
from maurice.kernel.events import EventStore


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_job_id() -> str:
    return f"job_{uuid4().hex}"


class JobStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduledJob(MauriceModel):
    id: str
    name: str
    owner: str = "kernel"
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.SCHEDULED
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_error: str | None = None

    @property
    def recurring(self) -> bool:
        return self.interval_seconds is not None


class JobStoreFile(MauriceModel):
    jobs: list[ScheduledJob] = Field(default_factory=list)


class JobStore:
    def __init__(self, path: str | Path, event_store: EventStore | None = None) -> None:
        self.path = Path(path).expanduser()
        self.event_store = event_store

    def schedule(
        self,
        *,
        name: str,
        run_at: datetime,
        owner: str = "kernel",
        payload: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        job = ScheduledJob(
            id=new_job_id(),
            name=name,
            owner=owner,
            payload=payload or {},
            run_at=run_at,
            interval_seconds=interval_seconds,
        )
        state = self._load()
        state.jobs.append(job)
        self._save(state)
        self._emit("job.scheduled", job)
        return job

    def list(self, *, status: JobStatus | str | None = None) -> list[ScheduledJob]:
        jobs = self._load().jobs
        if status is None:
            return jobs
        expected = JobStatus(status)
        return [job for job in jobs if job.status == expected]

    def due(self, *, now: datetime | None = None) -> list[ScheduledJob]:
        checked_at = now or utc_now()
        return [
            job
            for job in self._load().jobs
            if job.status == JobStatus.SCHEDULED and job.run_at <= checked_at
        ]

    def mark_running(self, job_id: str) -> ScheduledJob:
        return self._update(job_id, status=JobStatus.RUNNING, event_name="job.started")

    def complete(self, job_id: str, *, now: datetime | None = None) -> ScheduledJob:
        checked_at = now or utc_now()
        state = self._load()
        for job in state.jobs:
            if job.id != job_id:
                continue
            if job.recurring:
                job.status = JobStatus.SCHEDULED
                job.run_at = checked_at + timedelta(seconds=job.interval_seconds or 0)
            else:
                job.status = JobStatus.COMPLETED
            job.last_error = None
            job.updated_at = checked_at
            self._save(state)
            self._emit("job.completed", job)
            return job
        raise KeyError(job_id)

    def fail(self, job_id: str, error: str) -> ScheduledJob:
        return self._update(
            job_id,
            status=JobStatus.FAILED,
            error=error,
            event_name="job.failed",
        )

    def cancel(self, job_id: str) -> ScheduledJob:
        return self._update(job_id, status=JobStatus.CANCELLED, event_name="job.cancelled")

    def _update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        event_name: str,
        error: str | None = None,
    ) -> ScheduledJob:
        state = self._load()
        for job in state.jobs:
            if job.id == job_id:
                job.status = status
                job.last_error = error
                job.updated_at = utc_now()
                self._save(state)
                self._emit(event_name, job)
                return job
        raise KeyError(job_id)

    def _load(self) -> JobStoreFile:
        if not self.path.exists():
            return JobStoreFile()
        return JobStoreFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, state: JobStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _emit(self, name: str, job: ScheduledJob) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="progress",
            origin="kernel.scheduler",
            agent_id=job.payload.get("agent_id", "system"),
            session_id=job.payload.get("session_id", "scheduler"),
            correlation_id=job.id,
            payload={
                "job_id": job.id,
                "job_name": job.name,
                "owner": job.owner,
                "status": job.status,
                "run_at": job.run_at.isoformat(),
                "interval_seconds": job.interval_seconds,
                "last_error": job.last_error,
            },
        )


class JobHandler(Protocol):
    def __call__(self, job: ScheduledJob) -> Any:
        pass


class JobRunner:
    """Run due jobs through explicit handlers.

    The scheduler owns timing and lifecycle. Job meaning stays in handlers owned
    by skills or host code.
    """

    def __init__(self, store: JobStore, handlers: dict[str, JobHandler]) -> None:
        self.store = store
        self.handlers = handlers

    def run_due(self, *, now: datetime | None = None, limit: int | None = None) -> list[ScheduledJob]:
        due_jobs = self.store.due(now=now)
        if limit is not None:
            due_jobs = due_jobs[:limit]
        results: list[ScheduledJob] = []
        for job in due_jobs:
            handler = self.handlers.get(job.name)
            if handler is None:
                results.append(self.store.fail(job.id, f"No handler registered for {job.name}."))
                continue
            self.store.mark_running(job.id)
            try:
                handler(job)
            except Exception as exc:
                results.append(self.store.fail(job.id, str(exc)))
                continue
            results.append(self.store.complete(job.id, now=now))
        return results


class SchedulerService:
    """Poll the job store and run due jobs until stopped."""

    def __init__(
        self,
        runner: JobRunner,
        *,
        poll_interval_seconds: float = 5.0,
        sleep: Any = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.runner = runner
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep = sleep or time.sleep
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run_forever(self, *, max_iterations: int | None = None) -> int:
        iterations = 0
        while not self._stopped:
            self.runner.run_due()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            self.sleep(self.poll_interval_seconds)
        return iterations
