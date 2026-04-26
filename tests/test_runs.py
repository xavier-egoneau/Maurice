from __future__ import annotations

import json

from maurice.kernel.events import EventStore
from maurice.kernel.runs import RunApprovalStore, RunCoordinationStore, RunExecutor, RunStore


def test_run_store_creates_workspace_and_lists_runs(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = RunStore(
        tmp_path / "agents" / "main" / "runs.json",
        workspace_root=tmp_path,
        event_store=event_store,
    )

    run = store.create(
        parent_agent_id="main",
        task="Run tests.",
        write_scope={"paths": ["$workspace/runs/run_1/**"]},
        permission_scope={"classes": ["fs.read"]},
        context_inheritance="current_task",
        base_agent="coding",
        base_agent_profile={
            "id": "coding",
            "skills": ["filesystem"],
            "permission_profile": "safe",
        },
        context_summary="Repo has a run lifecycle.",
        relevant_files=[{"path": "maurice/kernel/runs.py", "reason": "Run store"}],
        constraints=["Use apply_patch."],
        plan=["Add tests."],
        dependency_policy={
            "can_request_install": True,
            "allowed_package_managers": ["pip"],
            "requires_parent_approval": True,
        },
        output_contract={"requires_self_check": True},
        run_id="run_test",
    )

    assert run.id == "run_test"
    assert run.state == "created"
    assert (tmp_path / "runs" / "run_test").is_dir()
    session = json.loads((tmp_path / "runs" / "run_test" / "session.json").read_text(encoding="utf-8"))
    assert session["id"] == "run_test"
    assert session["metadata"]["base_agent"] == "coding"
    mission = json.loads((tmp_path / "runs" / "run_test" / "mission.json").read_text(encoding="utf-8"))
    assert mission["context_summary"] == "Repo has a run lifecycle."
    assert mission["base_agent_profile"]["id"] == "coding"
    assert mission["base_agent_profile"]["skills"] == ["filesystem"]
    assert mission["relevant_files"][0]["path"] == "maurice/kernel/runs.py"
    assert mission["constraints"] == ["Use apply_patch."]
    assert mission["plan"] == ["Add tests."]
    assert mission["dependency_policy"]["can_request_install"] is True
    assert mission["output_contract"]["requires_self_check"] is True
    assert store.list()[0].id == "run_test"
    assert event_store.read_all()[0].name == "subagent_run.created"


def test_run_store_checkpoint_complete_and_cancel_write_envelopes(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path, event_store=event_store)
    run = store.create(
        parent_agent_id="main",
        task="Investigate.",
        write_scope={"paths": ["maurice/kernel/**"]},
        permission_scope={"classes": ["fs.read"]},
        run_id="run_test",
    )

    checkpointed, checkpoint = store.checkpoint(
        run.id,
        summary="Half done.",
        artifacts=[{"type": "file", "path": "notes.md"}],
        safe_to_resume=True,
    )

    assert checkpointed.state == "paused"
    assert checkpoint.safe_to_resume is True
    assert (tmp_path / "runs" / "run_test" / "checkpoint.json").is_file()

    completed, final = store.complete(
        run.id,
        summary="Done.",
        changed_files=["maurice/kernel/runs.py"],
        verification=[
            {
                "command": "pytest tests/test_runs.py",
                "status": "passed",
                "output_summary": "3 passed",
            }
        ],
        risks=["Executor integration is still pending."],
    )

    assert completed.state == "completed"
    assert final.status == "completed"
    assert final.changed_files == ["maurice/kernel/runs.py"]
    assert final.verification[0]["status"] == "passed"
    assert final.risks == ["Executor integration is still pending."]
    assert (tmp_path / "runs" / "run_test" / "final.json").is_file()

    reviewed, review = store.review(
        run.id,
        status="accepted",
        summary="Parent review passed.",
    )

    assert reviewed.id == run.id
    assert review.status == "accepted"
    assert (tmp_path / "runs" / "run_test" / "parent_review.json").is_file()
    assert event_store.read_all()[-1].name == "subagent_run.reviewed"


def test_run_store_review_requires_completed_run(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    run = store.create(
        parent_agent_id="main",
        task="Review later.",
        write_scope={},
        permission_scope={},
        run_id="run_test",
    )

    try:
        store.review(run.id, status="accepted", summary="Too early.")
    except ValueError as exc:
        assert "must be completed" in str(exc)
    else:
        raise AssertionError("Expected parent review to require completion.")


def test_run_store_rejects_changed_files_outside_write_scope(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    run = store.create(
        parent_agent_id="main",
        task="Investigate.",
        write_scope={"paths": ["maurice/kernel/**"]},
        permission_scope={"classes": ["fs.read"]},
        run_id="run_test",
    )

    try:
        store.complete(run.id, summary="Done.", changed_files=["README.md"])
    except ValueError as exc:
        assert "outside run write scope" in str(exc)
    else:
        raise AssertionError("Expected write scope enforcement.")


def test_run_store_requires_self_check_when_mission_demands_it(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    run = store.create(
        parent_agent_id="main",
        task="Implement.",
        write_scope={"paths": ["$workspace/runs/run_test/**"]},
        permission_scope={"classes": ["fs.read"]},
        output_contract={"requires_self_check": True},
        run_id="run_test",
    )

    try:
        store.complete(run.id, summary="Done.")
    except ValueError as exc:
        assert "self-check" in str(exc)
    else:
        raise AssertionError("Expected self-check enforcement.")

    completed, final = store.complete(
        run.id,
        summary="Done.",
        verification=[{"command": "pytest", "status": "passed", "output_summary": "ok"}],
    )

    assert completed.state == "completed"
    assert final.verification[0]["command"] == "pytest"


def test_run_store_cancel_produces_resumable_checkpoint(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    run = store.create(
        parent_agent_id="main",
        task="Long task.",
        write_scope={"paths": ["$workspace/runs/run_test/**"]},
        permission_scope={"classes": ["fs.read"]},
        run_id="run_test",
    )

    cancelled, envelope = store.cancel(run.id)

    assert cancelled.state == "cancelled"
    assert cancelled.safe_to_resume is True
    assert envelope.status == "cancelled"
    assert (tmp_path / "runs" / "run_test" / "checkpoint.json").is_file()


def test_run_executor_prepares_session_and_checkpoint(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    run = store.create(
        parent_agent_id="main",
        task="Implement executor.",
        write_scope={},
        permission_scope={},
        context_summary="Executor skeleton.",
        constraints=["Stay scoped."],
        plan=["Load mission.", "Prepare session."],
        run_id="run_test",
    )

    prepared, checkpoint = RunExecutor(store).prepare(run.id)

    session = json.loads((tmp_path / "runs" / "run_test" / "session.json").read_text(encoding="utf-8"))
    assert prepared.state == "paused"
    assert prepared.safe_to_resume is True
    assert checkpoint.status == "paused"
    assert session["messages"][0]["role"] == "system"
    assert session["messages"][1]["content"] == "Implement executor."
    assert (tmp_path / "runs" / "run_test" / "checkpoint.json").is_file()


def test_run_executor_autonomy_stops_only_when_blocked(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path, event_store=event_store)
    run = store.create(
        parent_agent_id="main",
        task="Implement feature.",
        write_scope={},
        permission_scope={},
        plan=["Read files.", "Edit code.", "Run tests."],
        execution_policy={
            "mode": "continue_until_blocked",
            "max_steps": 10,
            "checkpoint_every_steps": 2,
            "stop_conditions": ["execution_engine_missing", "approval_required", "plan_complete"],
        },
        run_id="run_auto",
    )

    paused, report = RunExecutor(store).execute_autonomous(run.id)

    assert paused.state == "paused"
    assert paused.safe_to_resume is True
    assert report.policy.mode == "continue_until_blocked"
    assert report.stop_reason == "execution_engine_missing"
    assert report.steps[0].description == "Read files."
    assert (tmp_path / "runs" / "run_auto" / "autonomy_report.json").is_file()
    assert (tmp_path / "runs" / "run_auto" / "checkpoint.json").is_file()
    assert "subagent_run.autonomy_stopped" in [event.name for event in event_store.read_all()]


def test_run_store_resume_requires_safe_to_resume(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    safe = store.create(
        parent_agent_id="main",
        task="Safe task.",
        write_scope={},
        permission_scope={},
        run_id="run_safe",
    )
    unsafe = store.create(
        parent_agent_id="main",
        task="Unsafe task.",
        write_scope={},
        permission_scope={},
        run_id="run_unsafe",
    )
    store.checkpoint(safe.id, summary="Paused safely.", safe_to_resume=True)
    store.checkpoint(unsafe.id, summary="Paused unsafely.", safe_to_resume=False)

    resumed = store.resume(safe.id)

    assert resumed.state == "running"
    assert resumed.safe_to_resume is False
    try:
        store.resume(unsafe.id)
    except ValueError as exc:
        assert "safe to resume" in str(exc)
    else:
        raise AssertionError("Expected unsafe resume to fail.")


def test_run_coordination_store_requests_acknowledges_and_resolves(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = RunCoordinationStore(tmp_path / "coordination.json", event_store=event_store)

    event = store.request(
        parent_agent_id="main",
        source_run_id="run_a",
        affected_run_ids=["run_b"],
        impact="Schema changed.",
        requested_action="Notify run_b.",
    )

    assert event.status == "pending"
    assert store.list(status="pending")[0].id == event.id

    acknowledged = store.acknowledge(event.id)
    resolved = store.resolve(event.id)

    assert acknowledged.status == "acknowledged"
    assert acknowledged.acknowledged_at is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None
    assert [event.name for event in event_store.read_all()] == [
        "subagent_run.coordination_requested",
        "subagent_run.coordination_acknowledged",
        "subagent_run.coordination_resolved",
    ]


def test_run_approval_store_requests_approves_and_denies(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    store = RunApprovalStore(tmp_path / "run_approvals.json", event_store=event_store)

    first = store.request(
        parent_agent_id="main",
        run_id="run_a",
        type="dependency",
        reason="Need a dev package.",
        requested_scope={"package_manager": "pip"},
    )
    second = store.request(
        parent_agent_id="main",
        run_id="run_b",
        type="permission",
        reason="Need read access.",
        requested_scope={"permission": "fs.read"},
    )

    approved = store.approve(first.id)
    denied = store.deny(second.id)

    assert approved.status == "approved"
    assert denied.status == "denied"
    assert store.list(status="approved")[0].id == first.id
    assert [event.name for event in event_store.read_all()] == [
        "subagent_run.approval_requested",
        "subagent_run.approval_requested",
        "subagent_run.approval_approved",
        "subagent_run.approval_denied",
    ]


def test_run_store_validates_dependency_approval_policy(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.json", workspace_root=tmp_path)
    blocked = store.create(
        parent_agent_id="main",
        task="Blocked.",
        write_scope={},
        permission_scope={},
        run_id="run_blocked",
    )
    allowed = store.create(
        parent_agent_id="main",
        task="Allowed.",
        write_scope={},
        permission_scope={},
        dependency_policy={
            "can_request_install": True,
            "allowed_package_managers": ["pip"],
            "requires_parent_approval": True,
        },
        run_id="run_allowed",
    )

    try:
        store.validate_approval_request(
            blocked.id,
            type="dependency",
            requested_scope={"package_manager": "pip"},
        )
    except ValueError as exc:
        assert "does not allow" in str(exc)
    else:
        raise AssertionError("Expected dependency policy rejection.")

    try:
        store.validate_approval_request(
            allowed.id,
            type="dependency",
            requested_scope={"package_manager": "uv"},
        )
    except ValueError as exc:
        assert "outside dependency policy" in str(exc)
    else:
        raise AssertionError("Expected package manager rejection.")

    store.validate_approval_request(
        allowed.id,
        type="dependency",
        requested_scope={"package_manager": "pip"},
    )
