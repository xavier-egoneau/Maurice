"""Self-update proposal tools."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from maurice.kernel.contracts import ToolResult
from maurice.kernel.permissions import PermissionContext

TARGET_TYPES = {"kernel", "host", "system_skill", "packaged_defaults", "installer"}
RISKS = {"low", "medium", "high"}
STATUS = "draft"


def self_update_tool_executors(context: PermissionContext) -> dict[str, Any]:
    return {
        "self_update.propose": lambda arguments: propose(arguments, context),
        "maurice.system_skills.self_update.tools.propose": lambda arguments: propose(
            arguments, context
        ),
    }


def propose(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    validation_error = _validate(arguments)
    if validation_error is not None:
        return validation_error

    target_type = arguments["target_type"]
    target_name = arguments["target_name"]
    runtime_path = arguments["runtime_path"]
    summary = arguments["summary"]
    patch = arguments["patch"]
    risk = arguments["risk"]
    test_plan = arguments["test_plan"]
    created_by_agent = arguments.get("created_by_agent", "main")
    requires_restart = bool(arguments.get("requires_restart", False))

    proposal_id = _proposal_id(target_type, target_name, summary)
    proposal_dir = _proposal_root(context) / proposal_id
    suffix = 2
    while proposal_dir.exists():
        proposal_dir = _proposal_root(context) / f"{proposal_id}_{suffix}"
        suffix += 1
    proposal_id = proposal_dir.name
    proposal_dir.mkdir(parents=True)

    patch_hash = _sha256(patch)
    record = {
        "id": proposal_id,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by_agent": created_by_agent,
        "target": {
            "type": target_type,
            "name": target_name,
            "runtime_path": runtime_path,
        },
        "permission": {
            "class": "runtime.write",
            "mode": "proposal_only",
        },
        "summary": summary,
        "risk": risk,
        "requires_restart": requires_restart,
        "patch": "patch.diff",
        "patch_hash": patch_hash,
        "test_plan": "test_plan.md",
        "status": STATUS,
    }

    (proposal_dir / "proposal.yaml").write_text(
        yaml.safe_dump(record, sort_keys=False),
        encoding="utf-8",
    )
    (proposal_dir / "summary.md").write_text(f"# Summary\n\n{summary}\n", encoding="utf-8")
    (proposal_dir / "patch.diff").write_text(patch, encoding="utf-8")
    (proposal_dir / "risk.md").write_text(f"# Risk\n\n{risk}\n", encoding="utf-8")
    (proposal_dir / "test_plan.md").write_text(
        f"# Test Plan\n\n{test_plan}\n",
        encoding="utf-8",
    )

    return ToolResult(
        ok=True,
        summary=f"Runtime proposal created: {proposal_id}",
        data={
            "proposal": record,
            "path": str(proposal_dir),
        },
        trust="skill_generated",
        artifacts=[
            {"type": "file", "path": str(proposal_dir / "proposal.yaml")},
            {"type": "file", "path": str(proposal_dir / "summary.md")},
            {"type": "file", "path": str(proposal_dir / "patch.diff")},
            {"type": "file", "path": str(proposal_dir / "risk.md")},
            {"type": "file", "path": str(proposal_dir / "test_plan.md")},
        ],
        events=[
            {
                "name": "runtime.proposal_created",
                "payload": {
                    "proposal_id": proposal_id,
                    "created_by_agent": created_by_agent,
                    "target_type": target_type,
                    "target_name": target_name,
                    "patch_hash": patch_hash,
                    "status": STATUS,
                },
            }
        ],
        error=None,
    )


def _validate(arguments: dict[str, Any]) -> ToolResult | None:
    required_strings = [
        "target_type",
        "target_name",
        "runtime_path",
        "summary",
        "patch",
        "risk",
        "test_plan",
    ]
    for field in required_strings:
        if not isinstance(arguments.get(field), str) or not arguments[field].strip():
            return _error("invalid_arguments", f"self_update.propose requires {field}.")
    if arguments["target_type"] not in TARGET_TYPES:
        return _error("invalid_target", f"Unknown runtime target type: {arguments['target_type']}")
    if arguments["risk"] not in RISKS:
        return _error("invalid_risk", "risk must be low, medium, or high.")
    mode = arguments.get("mode", "proposal_only")
    if mode != "proposal_only":
        return _error("direct_apply_denied", "self_update only supports proposal_only mode.")
    if "created_by_agent" in arguments and not isinstance(arguments["created_by_agent"], str):
        return _error("invalid_arguments", "created_by_agent must be a string.")
    return None


def _proposal_root(context: PermissionContext) -> Path:
    return Path(context.variables()["$workspace"]) / "proposals" / "runtime"


def _proposal_id(target_type: str, target_name: str, summary: str) -> str:
    date = datetime.now(UTC).strftime("%Y_%m_%d")
    slug_source = f"{target_type}_{target_name}_{summary}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug_source).strip("_").lower()
    return f"runtime_{date}_{slug[:60]}"


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="trusted",
        artifacts=[],
        events=[],
        error={"code": code, "message": message, "retryable": False},
    )
