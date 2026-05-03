"""Host-owned runtime proposal validation and apply flow."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from maurice.host.paths import host_config_path
from maurice.kernel.config import load_workspace_config
from maurice.kernel.contracts import MauriceModel
from maurice.kernel.events import EventStore


class ProposalCheck(MauriceModel):
    name: str
    ok: bool
    summary: str


class ProposalSummary(MauriceModel):
    id: str
    path: str
    status: str
    summary: str
    risk: str
    target: dict[str, Any] = Field(default_factory=dict)


class ProposalValidationReport(MauriceModel):
    proposal_id: str
    ok: bool
    checks: list[ProposalCheck]
    record: dict[str, Any] = Field(default_factory=dict)


class ProposalTestReport(MauriceModel):
    proposal_id: str
    ok: bool
    commands: list[dict[str, Any]] = Field(default_factory=list)


class ProposalApplyReport(MauriceModel):
    proposal_id: str
    status: str
    applied: bool
    validation: ProposalValidationReport
    tests: ProposalTestReport | None = None
    report_path: str | None = None
    rollback_path: str | None = None
    errors: list[str] = Field(default_factory=list)


def list_runtime_proposals(workspace_root: str | Path) -> list[ProposalSummary]:
    root = _proposal_root(workspace_root)
    if not root.is_dir():
        return []
    proposals = []
    for proposal_dir in sorted(item for item in root.iterdir() if item.is_dir()):
        record = _read_record(proposal_dir)
        if record is None:
            continue
        proposals.append(
            ProposalSummary(
                id=record.get("id") or proposal_dir.name,
                path=str(proposal_dir),
                status=record.get("status", "unknown"),
                summary=record.get("summary", ""),
                risk=record.get("risk", "unknown"),
                target=record.get("target", {}),
            )
        )
    return proposals


def validate_runtime_proposal(workspace_root: str | Path, proposal_id: str) -> ProposalValidationReport:
    _workspace, runtime = _host_paths(workspace_root)
    proposal_dir = _proposal_root(workspace_root) / proposal_id
    checks: list[ProposalCheck] = []
    record = _read_record(proposal_dir)
    checks.append(_check("proposal.yaml", record is not None, str(proposal_dir / "proposal.yaml")))
    if record is None:
        return ProposalValidationReport(proposal_id=proposal_id, ok=False, checks=checks)

    patch_path = proposal_dir / record.get("patch", "patch.diff")
    patch = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    expected_hash = record.get("patch_hash")
    checks.append(_check("patch.diff", patch_path.is_file(), str(patch_path)))
    checks.append(_check("patch_hash", expected_hash == _sha256(patch), "patch hash matches proposal.yaml"))
    permission = record.get("permission", {})
    checks.append(
        _check(
            "permission",
            permission == {"class": "runtime.write", "mode": "proposal_only"},
            "permission must remain runtime.write proposal_only",
        )
    )
    target_path = _expand_runtime_path(record.get("target", {}).get("runtime_path"), runtime)
    checks.append(
        _check(
            "target_scope",
            target_path is not None and _is_relative_to(target_path, runtime.resolve()),
            str(target_path) if target_path is not None else "missing runtime_path",
        )
    )
    check_result = _git_apply_check(runtime, patch_path)
    checks.append(_check("patch_check", check_result.returncode == 0, check_result.stderr.strip() or "git apply --check"))
    return ProposalValidationReport(
        proposal_id=proposal_id,
        ok=all(check.ok for check in checks),
        checks=checks,
        record=record,
    )


def run_proposal_tests(workspace_root: str | Path, proposal_id: str) -> ProposalTestReport:
    _workspace, runtime = _host_paths(workspace_root)
    proposal_dir = _proposal_root(workspace_root) / proposal_id
    test_plan = proposal_dir / "test_plan.md"
    commands = _test_commands(test_plan)
    results = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=runtime,
            shell=True,
            text=True,
            capture_output=True,
            timeout=300,
        )
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
    return ProposalTestReport(
        proposal_id=proposal_id,
        ok=all(result["returncode"] == 0 for result in results),
        commands=results,
    )


def apply_runtime_proposal(
    workspace_root: str | Path,
    proposal_id: str,
    *,
    confirmed: bool,
    run_tests: bool = True,
) -> ProposalApplyReport:
    if not confirmed:
        raise PermissionError("Applying a runtime proposal requires --confirm-approval.")
    workspace, runtime = _host_paths(workspace_root)
    proposal_dir = _proposal_root(workspace) / proposal_id
    validation = validate_runtime_proposal(workspace, proposal_id)
    tests = run_proposal_tests(workspace, proposal_id) if run_tests else None
    errors = []
    if not validation.ok:
        errors.append("proposal validation failed")
    if tests is not None and not tests.ok:
        errors.append("proposal tests failed")
    if errors:
        report = ProposalApplyReport(
            proposal_id=proposal_id,
            status="failed",
            applied=False,
            validation=validation,
            tests=tests,
            errors=errors,
        )
        _write_apply_report(proposal_dir, report)
        _mark_status(proposal_dir, "failed")
        _emit_apply_event(workspace, proposal_id, "runtime.proposal_failed", report)
        return report

    patch_path = proposal_dir / validation.record.get("patch", "patch.diff")
    completed = subprocess.run(
        ["git", "apply", str(patch_path)],
        cwd=runtime,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        errors.append(completed.stderr.strip() or "git apply failed")
        report = ProposalApplyReport(
            proposal_id=proposal_id,
            status="failed",
            applied=False,
            validation=validation,
            tests=tests,
            errors=errors,
        )
        _write_apply_report(proposal_dir, report)
        _mark_status(proposal_dir, "failed")
        _emit_apply_event(workspace, proposal_id, "runtime.proposal_failed", report)
        return report

    rollback_path = proposal_dir / "rollback.md"
    rollback_path.write_text(
        "# Rollback\n\n"
        "Run this from the runtime root if the applied patch must be reverted:\n\n"
        f"```bash\ngit apply -R {patch_path}\n```\n",
        encoding="utf-8",
    )
    report = ProposalApplyReport(
        proposal_id=proposal_id,
        status="applied",
        applied=True,
        validation=validation,
        tests=tests,
        rollback_path=str(rollback_path),
    )
    report_path = _write_apply_report(proposal_dir, report)
    report.report_path = str(report_path)
    _write_apply_report(proposal_dir, report)
    _mark_status(proposal_dir, "applied")
    _emit_apply_event(workspace, proposal_id, "runtime.proposal_applied", report)
    return report


def _proposal_root(workspace_root: str | Path) -> Path:
    workspace, _runtime = _host_paths(workspace_root)
    return workspace / "proposals" / "runtime"


def _host_paths(workspace_root: str | Path) -> tuple[Path, Path]:
    root = Path(workspace_root).expanduser().resolve()
    if host_config_path(root).is_file():
        bundle = load_workspace_config(root)
        return (
            Path(bundle.host.workspace_root).expanduser().resolve(),
            Path(bundle.host.runtime_root).expanduser().resolve(),
        )
    return root, Path(__file__).resolve().parents[2]


def _read_record(proposal_dir: Path) -> dict[str, Any] | None:
    path = proposal_dir / "proposal.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else None


def _mark_status(proposal_dir: Path, status: str) -> None:
    record = _read_record(proposal_dir)
    if record is None:
        return
    record["status"] = status
    record["updated_at"] = datetime.now(UTC).isoformat()
    (proposal_dir / "proposal.yaml").write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")


def _write_apply_report(proposal_dir: Path, report: ProposalApplyReport) -> Path:
    path = proposal_dir / "apply_report.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def _emit_apply_event(workspace: Path, proposal_id: str, name: str, report: ProposalApplyReport) -> None:
    EventStore(workspace / "agents" / "main" / "events.jsonl").emit(
        name=name,
        origin="host.self_update",
        agent_id="main",
        session_id="self_update",
        correlation_id=proposal_id,
        payload={"proposal_id": proposal_id, "status": report.status, "applied": report.applied},
    )


def _git_apply_check(runtime: Path, patch_path: Path):
    if not patch_path.is_file():
        return subprocess.CompletedProcess(["git", "apply", "--check"], 1, "", "patch file missing")
    return subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=runtime,
        text=True,
        capture_output=True,
    )


def _test_commands(test_plan: Path) -> list[str]:
    if not test_plan.is_file():
        return []
    commands = []
    for line in test_plan.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("$ "):
            commands.append(stripped[2:].strip())
    return commands


def _expand_runtime_path(value: Any, runtime: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    expanded = value.replace("$runtime", str(runtime))
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = runtime / path
    return path.resolve()


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _check(name: str, ok: bool, summary: str) -> ProposalCheck:
    return ProposalCheck(name=name, ok=ok, summary=summary)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
