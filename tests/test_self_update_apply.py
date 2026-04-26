from __future__ import annotations

import yaml

from maurice.host.self_update import (
    apply_runtime_proposal,
    list_runtime_proposals,
    run_proposal_tests,
    validate_runtime_proposal,
)
from maurice.host.workspace import initialize_workspace
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.self_update.tools import propose


def create_workspace_and_proposal(tmp_path):
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    (runtime / "hello.txt").write_text("old\n", encoding="utf-8")
    initialize_workspace(workspace, runtime)
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""
    result = propose(
        {
            "target_type": "host",
            "target_name": "hello",
            "runtime_path": "$runtime/hello.txt",
            "summary": "Update hello text.",
            "patch": patch,
            "risk": "low",
            "test_plan": '$ python3 -c "open(\'hello.txt\').read()"',
            "mode": "proposal_only",
        },
        context,
    )
    return workspace, runtime, result.data["proposal"]["id"]


def test_self_update_lists_validates_and_tests_proposal(tmp_path) -> None:
    workspace, _runtime, proposal_id = create_workspace_and_proposal(tmp_path)

    proposals = list_runtime_proposals(workspace)
    validation = validate_runtime_proposal(workspace, proposal_id)
    tests = run_proposal_tests(workspace, proposal_id)

    assert proposals[0].id == proposal_id
    assert validation.ok is True
    assert tests.ok is True
    assert tests.commands[0]["returncode"] == 0


def test_self_update_apply_requires_confirmation(tmp_path) -> None:
    workspace, _runtime, proposal_id = create_workspace_and_proposal(tmp_path)

    try:
        apply_runtime_proposal(workspace, proposal_id, confirmed=False)
    except PermissionError as exc:
        assert "requires --confirm-approval" in str(exc)
    else:
        raise AssertionError("expected PermissionError")


def test_self_update_apply_updates_runtime_and_writes_report(tmp_path) -> None:
    workspace, runtime, proposal_id = create_workspace_and_proposal(tmp_path)

    report = apply_runtime_proposal(workspace, proposal_id, confirmed=True)

    assert report.applied is True
    assert (runtime / "hello.txt").read_text(encoding="utf-8") == "new\n"
    proposal_dir = workspace / "proposals" / "runtime" / proposal_id
    record = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))
    assert record["status"] == "applied"
    assert (proposal_dir / "apply_report.json").is_file()
    assert (proposal_dir / "rollback.md").is_file()
    assert (workspace / "agents" / "main" / "events.jsonl").is_file()


def test_self_update_apply_fails_without_corrupting_runtime(tmp_path) -> None:
    workspace, runtime, proposal_id = create_workspace_and_proposal(tmp_path)
    patch_path = workspace / "proposals" / "runtime" / proposal_id / "patch.diff"
    patch_path.write_text("not a patch\n", encoding="utf-8")

    report = apply_runtime_proposal(workspace, proposal_id, confirmed=True)

    assert report.applied is False
    assert (runtime / "hello.txt").read_text(encoding="utf-8") == "old\n"
    assert "proposal validation failed" in report.errors
