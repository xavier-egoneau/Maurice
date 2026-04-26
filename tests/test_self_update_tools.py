from __future__ import annotations

import yaml

from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.self_update.tools import propose


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def proposal_args(**overrides):
    args = {
        "target_type": "system_skill",
        "target_name": "memory",
        "runtime_path": "$runtime/maurice/system_skills/memory",
        "summary": "Improve memory search ranking.",
        "patch": "diff --git a/file b/file\n",
        "risk": "low",
        "test_plan": "Run pytest.",
        "requires_restart": False,
        "created_by_agent": "main",
        "mode": "proposal_only",
    }
    args.update(overrides)
    return args


def test_self_update_propose_creates_workspace_proposal(tmp_path) -> None:
    permission_context = context(tmp_path)

    result = propose(proposal_args(), permission_context)
    proposal_dir = tmp_path / "workspace" / "proposals" / "runtime" / result.data["proposal"]["id"]
    record = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))

    assert result.ok
    assert proposal_dir.is_dir()
    assert (proposal_dir / "patch.diff").read_text(encoding="utf-8").startswith("diff")
    assert record["permission"] == {"class": "runtime.write", "mode": "proposal_only"}
    assert record["target"]["name"] == "memory"
    assert record["status"] == "draft"


def test_self_update_propose_rejects_direct_apply(tmp_path) -> None:
    result = propose(proposal_args(mode="apply"), context(tmp_path))

    assert not result.ok
    assert result.error.code == "direct_apply_denied"


def test_self_update_propose_rejects_invalid_target(tmp_path) -> None:
    result = propose(proposal_args(target_type="database"), context(tmp_path))

    assert not result.ok
    assert result.error.code == "invalid_target"
