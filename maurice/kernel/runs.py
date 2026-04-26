"""Disposable subagent run lifecycle primitives."""

from __future__ import annotations

import json
import fnmatch
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import MauriceModel, SubagentRun, SubagentRunState
from maurice.kernel.events import EventStore
from maurice.kernel.session import SessionMessage, SessionRecord


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


def new_coordination_id() -> str:
    return f"coord_{uuid4().hex}"


def new_run_approval_id() -> str:
    return f"runappr_{uuid4().hex}"


class RunResultEnvelope(MauriceModel):
    status: str
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    verification: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    requested_followups: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    safe_to_resume: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class RunReviewEnvelope(MauriceModel):
    status: str
    summary: str
    requested_followups: list[str] = Field(default_factory=list)
    reviewed_at: datetime = Field(default_factory=utc_now)


class MissionDependencyPolicy(MauriceModel):
    can_request_install: bool = False
    allowed_package_managers: list[str] = Field(default_factory=list)
    requires_parent_approval: bool = True


class MissionOutputContract(MauriceModel):
    required_sections: list[str] = Field(
        default_factory=lambda: [
            "status",
            "summary",
            "artifacts",
            "requested_followups",
            "errors",
        ]
    )
    requires_self_check: bool = False


class ExecutionPolicy(MauriceModel):
    mode: str = "continue_until_blocked"
    stop_conditions: list[str] = Field(
        default_factory=lambda: [
            "needs_user_decision",
            "permission_denied",
            "approval_required",
            "tests_failing_after_retry",
            "plan_complete",
            "execution_engine_missing",
        ]
    )
    max_steps: int = Field(default=20, ge=1)
    checkpoint_every_steps: int = Field(default=5, ge=1)


class AutonomousStep(MauriceModel):
    index: int
    description: str
    status: str
    stop_reason: str | None = None
    checkpointed: bool = False


class AutonomousExecutionReport(MauriceModel):
    run_id: str
    policy: ExecutionPolicy
    status: str
    stop_reason: str
    steps: list[AutonomousStep] = Field(default_factory=list)
    checkpoint_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class MissionPacket(MauriceModel):
    run_id: str
    parent_agent_id: str
    task: str
    context_summary: str = ""
    relevant_files: list[dict[str, str]] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    write_scope: dict[str, Any] = Field(default_factory=dict)
    permission_scope: dict[str, Any] = Field(default_factory=dict)
    context_inheritance: str = "current_task"
    base_agent: str | None = None
    base_agent_profile: dict[str, Any] = Field(default_factory=dict)
    dependency_policy: MissionDependencyPolicy = Field(default_factory=MissionDependencyPolicy)
    output_contract: MissionOutputContract = Field(default_factory=MissionOutputContract)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    created_at: datetime = Field(default_factory=utc_now)


class RunStoreFile(MauriceModel):
    runs: list[SubagentRun] = Field(default_factory=list)


class RunCoordinationEvent(MauriceModel):
    id: str
    parent_agent_id: str
    source_run_id: str
    affected_run_ids: list[str] = Field(default_factory=list)
    impact: str
    requested_action: str
    status: str = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None


class RunCoordinationStoreFile(MauriceModel):
    events: list[RunCoordinationEvent] = Field(default_factory=list)


class RunApprovalRequest(MauriceModel):
    id: str
    parent_agent_id: str
    run_id: str
    type: str
    reason: str
    requested_scope: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class RunApprovalStoreFile(MauriceModel):
    approvals: list[RunApprovalRequest] = Field(default_factory=list)


class RunStore:
    def __init__(
        self,
        path: str | Path,
        *,
        workspace_root: str | Path,
        event_store: EventStore | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.event_store = event_store

    def create(
        self,
        *,
        parent_agent_id: str,
        task: str,
        write_scope: dict[str, Any],
        permission_scope: dict[str, Any],
        context_inheritance: str = "current_task",
        base_agent: str | None = None,
        base_agent_profile: dict[str, Any] | None = None,
        context_summary: str = "",
        relevant_files: list[dict[str, str]] | None = None,
        constraints: list[str] | None = None,
        plan: list[str] | None = None,
        dependency_policy: dict[str, Any] | MissionDependencyPolicy | None = None,
        output_contract: dict[str, Any] | MissionOutputContract | None = None,
        execution_policy: dict[str, Any] | ExecutionPolicy | None = None,
        run_id: str | None = None,
    ) -> SubagentRun:
        run_id = run_id or new_run_id()
        run_workspace = self.workspace_root / "runs" / run_id
        run_workspace.mkdir(parents=True, exist_ok=False)
        run = SubagentRun(
            id=run_id,
            parent_agent_id=parent_agent_id,
            task=task,
            workspace=str(run_workspace),
            write_scope=write_scope,
            permission_scope=permission_scope,
            context_inheritance=context_inheritance,
            base_agent=base_agent,
            state=SubagentRunState.CREATED,
            event_stream=str(run_workspace / "events.jsonl"),
            safe_to_resume=False,
        )
        mission = MissionPacket(
            run_id=run.id,
            parent_agent_id=parent_agent_id,
            task=task,
            context_summary=context_summary,
            relevant_files=relevant_files or [],
            constraints=constraints or [],
            plan=plan or [],
            write_scope=write_scope,
            permission_scope=permission_scope,
            context_inheritance=context_inheritance,
            base_agent=base_agent,
            base_agent_profile=base_agent_profile or {},
            dependency_policy=(
                dependency_policy
                if isinstance(dependency_policy, MissionDependencyPolicy)
                else MissionDependencyPolicy.model_validate(dependency_policy or {})
            ),
            output_contract=(
                output_contract
                if isinstance(output_contract, MissionOutputContract)
                else MissionOutputContract.model_validate(output_contract or {})
            ),
            execution_policy=(
                execution_policy
                if isinstance(execution_policy, ExecutionPolicy)
                else ExecutionPolicy.model_validate(execution_policy or {})
            ),
        )
        _write_json(run_workspace / "mission.json", mission)
        _write_json(
            run_workspace / "session.json",
            SessionRecord(
                id=run.id,
                agent_id=parent_agent_id,
                metadata={
                    "run_id": run.id,
                    "parent_agent_id": parent_agent_id,
                    "base_agent": base_agent,
                    "base_agent_profile": base_agent_profile or {},
                    "context_inheritance": context_inheritance,
                },
            ),
        )
        state = self._load()
        state.runs.append(run)
        self._save(state)
        self._emit("subagent_run.created", run)
        return run

    def list(self, *, state: SubagentRunState | str | None = None) -> list[SubagentRun]:
        runs = self._load().runs
        if state is None:
            return runs
        expected = SubagentRunState(state)
        return [run for run in runs if run.state == expected]

    def get(self, run_id: str) -> SubagentRun:
        for run in self._load().runs:
            if run.id == run_id:
                return run
        raise KeyError(run_id)

    def load_mission(self, run_id: str) -> MissionPacket:
        return self._load_mission(self.get(run_id))

    def load_session(self, run_id: str) -> SessionRecord:
        run = self.get(run_id)
        return SessionRecord.model_validate(
            json.loads((Path(run.workspace) / "session.json").read_text(encoding="utf-8"))
        )

    def save_session(self, run_id: str, session: SessionRecord) -> SessionRecord:
        run = self.get(run_id)
        _write_json(Path(run.workspace) / "session.json", session)
        return session

    def mark_running(self, run_id: str) -> SubagentRun:
        return self._update(run_id, state=SubagentRunState.RUNNING, event_name="subagent_run.started")

    def resume(self, run_id: str) -> SubagentRun:
        run = self.get(run_id)
        if not run.safe_to_resume:
            raise ValueError("Run is not marked safe to resume.")
        if run.state not in (SubagentRunState.PAUSED, SubagentRunState.CANCELLED):
            raise ValueError(f"Run cannot be resumed from state: {run.state}")
        return self._update(
            run_id,
            state=SubagentRunState.RUNNING,
            safe_to_resume=False,
            event_name="subagent_run.resumed",
        )

    def checkpoint(
        self,
        run_id: str,
        *,
        summary: str,
        artifacts: list[dict[str, Any]] | None = None,
        requested_followups: list[str] | None = None,
        errors: list[str] | None = None,
        safe_to_resume: bool = True,
    ) -> tuple[SubagentRun, RunResultEnvelope]:
        run = self._update(
            run_id,
            state=SubagentRunState.PAUSED,
            safe_to_resume=safe_to_resume,
            event_name="subagent_run.checkpointed",
        )
        envelope = RunResultEnvelope(
            status="paused",
            summary=summary,
            artifacts=artifacts or [],
            requested_followups=requested_followups or [],
            errors=errors or [],
            safe_to_resume=safe_to_resume,
        )
        _write_envelope(Path(run.workspace) / "checkpoint.json", envelope)
        return run, envelope

    def complete(
        self,
        run_id: str,
        *,
        summary: str,
        changed_files: list[str] | None = None,
        verification: list[dict[str, Any]] | None = None,
        risks: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        requested_followups: list[str] | None = None,
    ) -> tuple[SubagentRun, RunResultEnvelope]:
        current = self.get(run_id)
        mission = self._load_mission(current)
        if mission.output_contract.requires_self_check and not verification:
            raise ValueError("Run requires self-check verification before completion.")
        self._validate_changed_files(changed_files or [], mission.write_scope)
        run = self._update(
            run_id,
            state=SubagentRunState.COMPLETED,
            safe_to_resume=False,
            event_name="subagent_run.completed",
        )
        envelope = RunResultEnvelope(
            status="completed",
            summary=summary,
            changed_files=changed_files or [],
            verification=verification or [],
            risks=risks or [],
            artifacts=artifacts or [],
            requested_followups=requested_followups or [],
            errors=[],
            safe_to_resume=False,
        )
        _write_envelope(Path(run.workspace) / "final.json", envelope)
        return run, envelope

    def review(
        self,
        run_id: str,
        *,
        status: str,
        summary: str,
        requested_followups: list[str] | None = None,
    ) -> tuple[SubagentRun, RunReviewEnvelope]:
        run = self.get(run_id)
        if run.state != SubagentRunState.COMPLETED:
            raise ValueError(f"Run must be completed before parent review: {run.state}")
        if status not in ("accepted", "needs_changes"):
            raise ValueError("Run review status must be accepted or needs_changes.")
        envelope = RunReviewEnvelope(
            status=status,
            summary=summary,
            requested_followups=requested_followups or [],
        )
        _write_json(Path(run.workspace) / "parent_review.json", envelope)
        self._emit_review("subagent_run.reviewed", run, envelope)
        return run, envelope

    def fail(self, run_id: str, *, summary: str, errors: list[str]) -> tuple[SubagentRun, RunResultEnvelope]:
        run = self._update(
            run_id,
            state=SubagentRunState.FAILED,
            safe_to_resume=False,
            event_name="subagent_run.failed",
        )
        envelope = RunResultEnvelope(
            status="failed",
            summary=summary,
            errors=errors,
            safe_to_resume=False,
        )
        _write_envelope(Path(run.workspace) / "final.json", envelope)
        return run, envelope

    def cancel(
        self,
        run_id: str,
        *,
        summary: str = "Run cancellation requested.",
        safe_to_resume: bool = True,
    ) -> tuple[SubagentRun, RunResultEnvelope]:
        run = self._update(
            run_id,
            state=SubagentRunState.CANCELLED,
            safe_to_resume=safe_to_resume,
            event_name="subagent_run.cancelled",
        )
        envelope = RunResultEnvelope(
            status="cancelled",
            summary=summary,
            requested_followups=["Review checkpoint before deleting run artifacts."],
            safe_to_resume=safe_to_resume,
        )
        _write_envelope(Path(run.workspace) / "checkpoint.json", envelope)
        return run, envelope

    def validate_approval_request(
        self,
        run_id: str,
        *,
        type: str,
        requested_scope: dict[str, Any],
    ) -> None:
        run = self.get(run_id)
        mission = self._load_mission(run)
        if type != "dependency":
            return
        policy = mission.dependency_policy
        if not policy.can_request_install:
            raise ValueError("Run mission does not allow dependency installation requests.")
        package_manager = requested_scope.get("package_manager")
        if (
            package_manager
            and policy.allowed_package_managers
            and package_manager not in policy.allowed_package_managers
        ):
            raise ValueError(f"Package manager is outside dependency policy: {package_manager}")

    def _update(
        self,
        run_id: str,
        *,
        state: SubagentRunState,
        event_name: str,
        safe_to_resume: bool | None = None,
    ) -> SubagentRun:
        store_file = self._load()
        for run in store_file.runs:
            if run.id != run_id:
                continue
            run.state = state
            if safe_to_resume is not None:
                run.safe_to_resume = safe_to_resume
            self._save(store_file)
            self._emit(event_name, run)
            return run
        raise KeyError(run_id)

    def _load(self) -> RunStoreFile:
        if not self.path.exists():
            return RunStoreFile()
        return RunStoreFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def _load_mission(self, run: SubagentRun) -> MissionPacket:
        return MissionPacket.model_validate(
            json.loads((Path(run.workspace) / "mission.json").read_text(encoding="utf-8"))
        )

    def _validate_changed_files(self, changed_files: list[str], write_scope: dict[str, Any]) -> None:
        if not changed_files:
            return
        allowed_patterns = write_scope.get("paths", [])
        if not allowed_patterns:
            raise ValueError("Run reported changed files but has an empty write scope.")
        for changed_file in changed_files:
            if not any(
                _path_matches_scope(changed_file, pattern, self.workspace_root)
                for pattern in allowed_patterns
            ):
                raise ValueError(f"Changed file is outside run write scope: {changed_file}")

    def _save(self, state: RunStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _emit(self, name: str, run: SubagentRun) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="progress",
            origin="kernel.runs",
            agent_id=run.parent_agent_id,
            session_id="runs",
            correlation_id=run.id,
            payload=run.model_dump(mode="json"),
        )

    def _emit_review(self, name: str, run: SubagentRun, review: RunReviewEnvelope) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="fact",
            origin="kernel.runs",
            agent_id=run.parent_agent_id,
            session_id="runs",
            correlation_id=run.id,
            payload={
                "run": run.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
            },
        )


class RunCoordinationStore:
    def __init__(self, path: str | Path, event_store: EventStore | None = None) -> None:
        self.path = Path(path).expanduser()
        self.event_store = event_store

    def request(
        self,
        *,
        parent_agent_id: str,
        source_run_id: str,
        affected_run_ids: list[str],
        impact: str,
        requested_action: str,
    ) -> RunCoordinationEvent:
        event = RunCoordinationEvent(
            id=new_coordination_id(),
            parent_agent_id=parent_agent_id,
            source_run_id=source_run_id,
            affected_run_ids=affected_run_ids,
            impact=impact,
            requested_action=requested_action,
        )
        state = self._load()
        state.events.append(event)
        self._save(state)
        self._emit("subagent_run.coordination_requested", event)
        return event

    def list(self, *, status: str | None = None) -> list[RunCoordinationEvent]:
        events = self._load().events
        if status is None:
            return events
        return [event for event in events if event.status == status]

    def acknowledge(self, coordination_id: str) -> RunCoordinationEvent:
        return self._update(
            coordination_id,
            status="acknowledged",
            event_name="subagent_run.coordination_acknowledged",
        )

    def resolve(self, coordination_id: str) -> RunCoordinationEvent:
        return self._update(
            coordination_id,
            status="resolved",
            event_name="subagent_run.coordination_resolved",
        )

    def _update(
        self,
        coordination_id: str,
        *,
        status: str,
        event_name: str,
    ) -> RunCoordinationEvent:
        state = self._load()
        now = utc_now()
        for event in state.events:
            if event.id != coordination_id:
                continue
            event.status = status
            if status == "acknowledged":
                event.acknowledged_at = now
            if status == "resolved":
                event.resolved_at = now
            self._save(state)
            self._emit(event_name, event)
            return event
        raise KeyError(coordination_id)

    def _load(self) -> RunCoordinationStoreFile:
        if not self.path.exists():
            return RunCoordinationStoreFile()
        return RunCoordinationStoreFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, state: RunCoordinationStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _emit(self, name: str, event: RunCoordinationEvent) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="progress",
            origin="kernel.runs",
            agent_id=event.parent_agent_id,
            session_id="runs",
            correlation_id=event.id,
            payload=event.model_dump(mode="json"),
        )


class RunApprovalStore:
    def __init__(self, path: str | Path, event_store: EventStore | None = None) -> None:
        self.path = Path(path).expanduser()
        self.event_store = event_store

    def request(
        self,
        *,
        parent_agent_id: str,
        run_id: str,
        type: str,
        reason: str,
        requested_scope: dict[str, Any] | None = None,
    ) -> RunApprovalRequest:
        approval = RunApprovalRequest(
            id=new_run_approval_id(),
            parent_agent_id=parent_agent_id,
            run_id=run_id,
            type=type,
            reason=reason,
            requested_scope=requested_scope or {},
        )
        state = self._load()
        state.approvals.append(approval)
        self._save(state)
        self._emit("subagent_run.approval_requested", approval)
        return approval

    def list(self, *, status: str | None = None) -> list[RunApprovalRequest]:
        approvals = self._load().approvals
        if status is None:
            return approvals
        return [approval for approval in approvals if approval.status == status]

    def approve(self, approval_id: str) -> RunApprovalRequest:
        return self._resolve(
            approval_id,
            status="approved",
            event_name="subagent_run.approval_approved",
        )

    def deny(self, approval_id: str) -> RunApprovalRequest:
        return self._resolve(
            approval_id,
            status="denied",
            event_name="subagent_run.approval_denied",
        )

    def _resolve(self, approval_id: str, *, status: str, event_name: str) -> RunApprovalRequest:
        state = self._load()
        for approval in state.approvals:
            if approval.id != approval_id:
                continue
            approval.status = status
            approval.resolved_at = utc_now()
            self._save(state)
            self._emit(event_name, approval)
            return approval
        raise KeyError(approval_id)

    def _load(self) -> RunApprovalStoreFile:
        if not self.path.exists():
            return RunApprovalStoreFile()
        return RunApprovalStoreFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, state: RunApprovalStoreFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _emit(self, name: str, approval: RunApprovalRequest) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="progress",
            origin="kernel.runs",
            agent_id=approval.parent_agent_id,
            session_id="runs",
            correlation_id=approval.id,
            payload=approval.model_dump(mode="json"),
        )


class RunExecutor:
    """Prepare and hand off a run to a future subagent execution engine."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def prepare(self, run_id: str) -> tuple[SubagentRun, RunResultEnvelope]:
        running = self.store.mark_running(run_id)
        mission = self.store.load_mission(run_id)
        session = self.store.load_session(run_id)
        session.messages.append(
            SessionMessage(
                role="system",
                content="Subagent mission packet loaded.",
                metadata={"mission_path": str(Path(running.workspace) / "mission.json")},
            )
        )
        session.messages.append(
            SessionMessage(
                role="user",
                content=mission.task,
                metadata={
                    "run_id": run_id,
                    "context_summary": mission.context_summary,
                    "constraints": mission.constraints,
                    "plan": mission.plan,
                },
            )
        )
        self.store.save_session(run_id, session)
        return self.store.checkpoint(
            run_id,
            summary="Run execution prepared; no subagent execution engine is registered yet.",
            requested_followups=["Register a run executor to continue from this prepared checkpoint."],
            safe_to_resume=True,
        )

    def execute_autonomous(self, run_id: str) -> tuple[SubagentRun, AutonomousExecutionReport]:
        running = self.store.mark_running(run_id)
        mission = self.store.load_mission(run_id)
        session = self.store.load_session(run_id)
        if not session.messages:
            session.messages.append(
                SessionMessage(
                    role="system",
                    content="Subagent mission packet loaded.",
                    metadata={"mission_path": str(Path(running.workspace) / "mission.json")},
                )
            )
            session.messages.append(
                SessionMessage(
                    role="user",
                    content=mission.task,
                    metadata={
                        "run_id": run_id,
                        "context_summary": mission.context_summary,
                        "constraints": mission.constraints,
                        "plan": mission.plan,
                        "execution_policy": mission.execution_policy.model_dump(mode="json"),
                    },
                )
            )
            self.store.save_session(run_id, session)

        planned_steps = mission.plan or [mission.task]
        steps: list[AutonomousStep] = []
        stop_reason = "plan_complete"
        checkpoint_path: str | None = None
        for index, description in enumerate(planned_steps[: mission.execution_policy.max_steps], start=1):
            stop_reason = "execution_engine_missing"
            should_checkpoint = index % mission.execution_policy.checkpoint_every_steps == 0
            steps.append(
                AutonomousStep(
                    index=index,
                    description=description,
                    status="blocked",
                    stop_reason=stop_reason,
                    checkpointed=should_checkpoint,
                )
            )
            if should_checkpoint:
                _run, envelope = self.store.checkpoint(
                    run_id,
                    summary=f"Autonomous execution checkpoint after {index} step(s).",
                    requested_followups=["Register a run execution engine to continue autonomous work."],
                    safe_to_resume=True,
                )
                checkpoint_path = str(Path(running.workspace) / "checkpoint.json")
                if envelope.status == "paused":
                    running = _run
            break

        if not steps:
            stop_reason = "plan_complete"
        if checkpoint_path is None:
            running, _envelope = self.store.checkpoint(
                run_id,
                summary=f"Autonomous execution stopped: {stop_reason}.",
                requested_followups=["Register a run execution engine to continue autonomous work."]
                if stop_reason == "execution_engine_missing"
                else [],
                safe_to_resume=True,
            )
            checkpoint_path = str(Path(running.workspace) / "checkpoint.json")

        report = AutonomousExecutionReport(
            run_id=run_id,
            policy=mission.execution_policy,
            status="paused" if stop_reason != "plan_complete" else "completed",
            stop_reason=stop_reason,
            steps=steps,
            checkpoint_path=checkpoint_path,
        )
        _write_json(Path(running.workspace) / "autonomy_report.json", report)
        if self.store.event_store is not None:
            self.store.event_store.emit(
                name="subagent_run.autonomy_stopped",
                kind="progress",
                origin="kernel.runs",
                agent_id=running.parent_agent_id,
                session_id="runs",
                correlation_id=running.id,
                payload=report.model_dump(mode="json"),
            )
        return running, report


def _write_envelope(path: Path, envelope: RunResultEnvelope) -> None:
    _write_json(path, envelope)


def _write_json(path: Path, model: MauriceModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def _path_matches_scope(path: str, pattern: str, workspace_root: Path) -> bool:
    candidate = _scope_path(path, workspace_root)
    scope_pattern = _scope_pattern(pattern, workspace_root)
    return fnmatch.fnmatch(candidate, scope_pattern) or candidate == scope_pattern.rstrip("/**")


def _scope_path(path: str, workspace_root: Path) -> str:
    expanded = path.replace("$workspace", str(workspace_root))
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return str(candidate.resolve())


def _scope_pattern(pattern: str, workspace_root: Path) -> str:
    expanded = pattern.replace("$workspace", str(workspace_root))
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return str(candidate)
