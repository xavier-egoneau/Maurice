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
SEVERITIES = {"low", "medium", "high"}
STATUS = "draft"


def build_executors(ctx: Any) -> dict[str, Any]:
    return self_update_tool_executors(ctx.permission_context)


def self_update_tool_executors(context: PermissionContext) -> dict[str, Any]:
    return {
        "self_update.propose": lambda arguments: propose(arguments, context),
        "self_update.report_bug": lambda arguments: report_bug(arguments, context),
        "maurice.system_skills.self_update.tools.propose": lambda arguments: propose(
            arguments, context
        ),
        "maurice.system_skills.self_update.tools.report_bug": lambda arguments: report_bug(
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


def report_bug(arguments: dict[str, Any], context: PermissionContext) -> ToolResult:
    validation_error = _validate_bug_report(arguments)
    if validation_error is not None:
        return validation_error

    target_type = arguments["target_type"]
    target_name = arguments.get("target_name", "")
    title = arguments["title"].strip()
    summary = arguments["summary"].strip()
    observed = arguments["observed"].strip()
    expected = arguments["expected"].strip()
    severity = arguments["severity"].strip()
    reproduction = _optional_text(arguments.get("reproduction"))
    workaround = _optional_text(arguments.get("workaround"))
    evidence = _string_list(arguments.get("evidence"))
    created_by_agent = arguments.get("created_by_agent", "main")

    report_id = _bug_report_id(target_type, target_name, title)
    report_dir = _bug_report_root(context) / report_id
    suffix = 2
    while report_dir.exists():
        report_dir = _bug_report_root(context) / f"{report_id}_{suffix}"
        suffix += 1
    report_id = report_dir.name
    report_dir.mkdir(parents=True)

    record = {
        "id": report_id,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by_agent": created_by_agent,
        "target": {
            "type": target_type,
            "name": target_name,
        },
        "title": title,
        "summary": summary,
        "severity": severity,
        "observed": observed,
        "expected": expected,
        "reproduction": reproduction,
        "workaround": workaround,
        "evidence": evidence,
        "status": "open",
    }

    (report_dir / "bug.yaml").write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (report_dir / "summary.md").write_text(
        _bug_summary_markdown(record),
        encoding="utf-8",
    )

    return ToolResult(
        ok=True,
        summary=f"Runtime bug reported: {report_id}",
        data={
            "bug_report": record,
            "path": str(report_dir),
        },
        trust="skill_generated",
        artifacts=[
            {"type": "file", "path": str(report_dir / "bug.yaml")},
            {"type": "file", "path": str(report_dir / "summary.md")},
        ],
        events=[
            {
                "name": "runtime.bug_reported",
                "payload": {
                    "bug_report_id": report_id,
                    "created_by_agent": created_by_agent,
                    "target_type": target_type,
                    "target_name": target_name,
                    "severity": severity,
                    "status": "open",
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


def _validate_bug_report(arguments: dict[str, Any]) -> ToolResult | None:
    required_strings = [
        "target_type",
        "title",
        "summary",
        "observed",
        "expected",
        "severity",
    ]
    for field in required_strings:
        if not isinstance(arguments.get(field), str) or not arguments[field].strip():
            return _error("invalid_arguments", f"self_update.report_bug requires {field}.")
    if arguments["target_type"] not in TARGET_TYPES:
        return _error("invalid_target", f"Unknown runtime target type: {arguments['target_type']}")
    if arguments["severity"] not in SEVERITIES:
        return _error("invalid_severity", "severity must be low, medium, or high.")
    if "target_name" in arguments and not isinstance(arguments["target_name"], str):
        return _error("invalid_arguments", "target_name must be a string.")
    if "created_by_agent" in arguments and not isinstance(arguments["created_by_agent"], str):
        return _error("invalid_arguments", "created_by_agent must be a string.")
    if "evidence" in arguments:
        try:
            _string_list(arguments["evidence"])
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
    return None


def _proposal_root(context: PermissionContext) -> Path:
    return Path(context.variables()["$workspace"]) / "proposals" / "runtime"


def _bug_report_root(context: PermissionContext) -> Path:
    return Path(context.variables()["$workspace"]) / "reports" / "bugs"


def _proposal_id(target_type: str, target_name: str, summary: str) -> str:
    date = datetime.now(UTC).strftime("%Y_%m_%d")
    slug_source = f"{target_type}_{target_name}_{summary}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug_source).strip("_").lower()
    return f"runtime_{date}_{slug[:60]}"


def _bug_report_id(target_type: str, target_name: str, title: str) -> str:
    date = datetime.now(UTC).strftime("%Y_%m_%d")
    slug_source = f"{target_type}_{target_name}_{title}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug_source).strip("_").lower()
    return f"bug_{date}_{slug[:60]}"


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("evidence must be a list of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("evidence must be a list of strings.")
        if item.strip():
            result.append(item.strip())
    return result


def _bug_summary_markdown(record: dict[str, Any]) -> str:
    evidence = record.get("evidence") or []
    evidence_lines = "\n".join(f"- {item}" for item in evidence) if evidence else "- none"
    return (
        f"# {record['title']}\n\n"
        f"Severity: {record['severity']}\n"
        f"Target: {record['target']['type']} {record['target'].get('name') or ''}\n"
        f"Status: {record['status']}\n\n"
        "## Summary\n\n"
        f"{record['summary']}\n\n"
        "## Observed\n\n"
        f"{record['observed']}\n\n"
        "## Expected\n\n"
        f"{record['expected']}\n\n"
        "## Reproduction\n\n"
        f"{record.get('reproduction') or 'not provided'}\n\n"
        "## Workaround\n\n"
        f"{record.get('workaround') or 'not provided'}\n\n"
        "## Evidence\n\n"
        f"{evidence_lines}\n"
    )


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
