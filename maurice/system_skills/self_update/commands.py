"""Self-update command handlers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

from maurice.host.command_registry import CommandContext, CommandResult
from maurice.host.self_update import (
    apply_runtime_proposal,
    list_runtime_proposals,
    validate_runtime_proposal,
)

MAX_DIFF_CHARS = 12000
MAX_LIST_DIFF_CHARS = 1800


def auto_update_list(context: CommandContext) -> CommandResult:
    workspace = _workspace(context)
    proposals = list_runtime_proposals(workspace)
    if not proposals:
        return CommandResult(
            text="Aucune proposition d'auto-update en attente.",
            metadata={"command": "/auto_update_list"},
        )

    lines = ["Propositions d'auto-update :", ""]
    for proposal in proposals:
        proposal_dir = _proposal_dir(workspace, proposal.id)
        record = _read_record(proposal_dir) or {}
        target = _target_label(proposal.target)
        risk = _read_text(proposal_dir / "risk.md").strip()
        test_plan = _read_text(proposal_dir / "test_plan.md").strip()
        diff = _read_text(proposal_dir / record.get("patch", "patch.diff"))
        lines.append(
            f"## `{proposal.id}`\n"
            f"- Statut : `{proposal.status}`\n"
            f"- Risque : `{proposal.risk}`\n"
            f"- Cible : {target}\n"
            f"- Résumé : {proposal.summary}\n"
            f"- Appliquer : `/auto_update_apply {proposal.id} confirm`"
        )
        if risk:
            lines.append(f"- Note risque : {risk}")
        if test_plan:
            lines.append(f"- Plan de test : {test_plan}")
        lines.append(_diff_block(diff, max_chars=MAX_LIST_DIFF_CHARS))
        lines.append("")
    return CommandResult(
        text="\n".join(lines),
        metadata={"command": "/auto_update_list", "count": len(proposals)},
    )


def auto_update_show(context: CommandContext) -> CommandResult:
    workspace = _workspace(context)
    proposal_id = _proposal_id_arg(context)
    if not proposal_id:
        return CommandResult(
            text="Usage : `/auto_update_show <proposal_id>`",
            metadata={"command": "/auto_update_show", "error": "missing_id"},
        )

    proposal_dir = _proposal_dir(workspace, proposal_id)
    record = _read_record(proposal_dir)
    if record is None:
        return _not_found("/auto_update_show", proposal_id)

    validation = validate_runtime_proposal(workspace, proposal_id)
    diff = _read_text(proposal_dir / record.get("patch", "patch.diff"))
    risk = _read_text(proposal_dir / "risk.md").strip()
    test_plan = _read_text(proposal_dir / "test_plan.md").strip()
    lines = [
        f"Auto-update `{proposal_id}`",
        "",
        f"Status : `{record.get('status', 'unknown')}`",
        f"Risk : `{record.get('risk', 'unknown')}`",
        f"Target : {_target_label(record.get('target', {}))}",
        "",
        "Summary :",
        str(record.get("summary") or "").strip() or "-",
        "",
        "Checks :",
        *_check_lines(validation.checks),
    ]
    if risk:
        lines.extend(["", "Risk note :", risk])
    if test_plan:
        lines.extend(["", "Test plan :", test_plan])
    lines.extend(["", "Diff :", _diff_block(diff)])
    lines.extend(
        [
            "",
            "Pour appliquer apres revue :",
            f"`/auto_update_apply {proposal_id} confirm`",
        ]
    )
    return CommandResult(
        text="\n".join(lines),
        metadata={"command": "/auto_update_show", "proposal_id": proposal_id},
    )


def auto_update_validate(context: CommandContext) -> CommandResult:
    workspace = _workspace(context)
    proposal_id = _proposal_id_arg(context)
    if not proposal_id:
        return CommandResult(
            text="Usage : `/auto_update_validate <proposal_id>`",
            metadata={"command": "/auto_update_validate", "error": "missing_id"},
        )
    if _read_record(_proposal_dir(workspace, proposal_id)) is None:
        return _not_found("/auto_update_validate", proposal_id)

    report = validate_runtime_proposal(workspace, proposal_id)
    status = "ok" if report.ok else "failed"
    lines = [
        f"Validation `{proposal_id}` : {status}",
        "",
        *_check_lines(report.checks),
    ]
    return CommandResult(
        text="\n".join(lines),
        metadata={"command": "/auto_update_validate", "proposal_id": proposal_id, "ok": report.ok},
    )


def auto_update_apply(context: CommandContext) -> CommandResult:
    workspace = _workspace(context)
    args = _args(context)
    proposal_id = args[0] if args else ""
    confirmed = len(args) >= 2 and args[1].lower() in {"confirm", "confirmer", "ok", "oui"}
    if not proposal_id:
        return CommandResult(
            text="Usage : `/auto_update_apply <proposal_id> confirm`",
            metadata={"command": "/auto_update_apply", "error": "missing_id"},
        )
    if not confirmed:
        return CommandResult(
            text=(
                "Application non lancee. Relis d'abord la proposition avec "
                "`/auto_update_list`, puis applique avec "
                f"`/auto_update_apply {proposal_id} confirm`."
            ),
            metadata={"command": "/auto_update_apply", "proposal_id": proposal_id, "confirmed": False},
        )
    if _read_record(_proposal_dir(workspace, proposal_id)) is None:
        return _not_found("/auto_update_apply", proposal_id)

    report = apply_runtime_proposal(workspace, proposal_id, confirmed=True)
    lines = [f"Application `{proposal_id}` : {report.status}"]
    if report.errors:
        lines.extend(["", "Erreurs :", *[f"- {error}" for error in report.errors]])
    lines.extend(["", "Validation :", *_check_lines(report.validation.checks)])
    if report.tests is not None:
        if report.tests.commands:
            lines.extend(["", "Tests :"])
            for command in report.tests.commands:
                lines.append(f"- {command['returncode']} `{command['command']}`")
        else:
            lines.extend(["", "Tests : aucun test declare."])
    if report.report_path:
        lines.append(f"\nRapport : `{_relative_to_workspace(workspace, report.report_path)}`")
    if report.rollback_path:
        lines.append(f"Rollback : `{_relative_to_workspace(workspace, report.rollback_path)}`")
    return CommandResult(
        text="\n".join(lines),
        metadata={
            "command": "/auto_update_apply",
            "proposal_id": proposal_id,
            "applied": report.applied,
            "status": report.status,
        },
    )


def _workspace(context: CommandContext) -> Path:
    root = context.callbacks.get("context_root") or context.callbacks.get("workspace") or "."
    return Path(root).expanduser().resolve()


def _args(context: CommandContext) -> list[str]:
    parts = context.message_text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return []
    try:
        return shlex.split(parts[1])
    except ValueError:
        return parts[1].split()


def _proposal_id_arg(context: CommandContext) -> str:
    args = _args(context)
    return args[0] if args else ""


def _proposal_dir(workspace: Path, proposal_id: str) -> Path:
    return workspace / "proposals" / "runtime" / proposal_id


def _read_record(proposal_dir: Path) -> dict[str, Any] | None:
    path = proposal_dir / "proposal.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _target_label(target: dict[str, Any]) -> str:
    target_type = str(target.get("type") or "unknown")
    name = str(target.get("name") or "").strip()
    runtime_path = str(target.get("runtime_path") or "").strip()
    label = f"{target_type}/{name}" if name else target_type
    return f"{label} `{runtime_path}`" if runtime_path else label


def _check_lines(checks: list[Any]) -> list[str]:
    if not checks:
        return ["- aucun check"]
    return [
        f"- {'ok' if check.ok else 'failed'} `{check.name}` : {check.summary}"
        for check in checks
    ]


def _diff_block(diff: str, *, max_chars: int = MAX_DIFF_CHARS) -> str:
    if not diff:
        return "```diff\n# no diff\n```"
    truncated = diff[:max_chars]
    suffix = "\n# diff truncated; open patch.diff for the full diff" if len(diff) > max_chars else ""
    return f"```diff\n{truncated.rstrip()}{suffix}\n```"


def _not_found(command: str, proposal_id: str) -> CommandResult:
    return CommandResult(
        text=f"Proposition introuvable : `{proposal_id}`.",
        metadata={"command": command, "proposal_id": proposal_id, "error": "not_found"},
    )


def _relative_to_workspace(workspace: Path, path: str | Path) -> str:
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(candidate)
