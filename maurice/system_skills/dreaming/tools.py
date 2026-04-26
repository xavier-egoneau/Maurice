"""Dreaming system skill tools."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maurice.kernel.contracts import DreamInput, DreamReport, ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillRegistry

DreamInputBuilder = Callable[[], DreamInput]


def dreaming_tool_executors(
    context: PermissionContext,
    registry: SkillRegistry,
    *,
    event_store: EventStore | None = None,
    dream_input_builders: dict[str, DreamInputBuilder] | None = None,
) -> dict[str, Any]:
    return {
        "dreaming.run": lambda arguments: run(
            arguments,
            context,
            registry,
            event_store=event_store,
            dream_input_builders=dream_input_builders or {},
        ),
        "maurice.system_skills.dreaming.tools.run": lambda arguments: run(
            arguments,
            context,
            registry,
            event_store=event_store,
            dream_input_builders=dream_input_builders or {},
        ),
    }


def run(
    arguments: dict[str, Any],
    context: PermissionContext,
    registry: SkillRegistry,
    *,
    event_store: EventStore | None = None,
    dream_input_builders: dict[str, DreamInputBuilder] | None = None,
) -> ToolResult:
    dream_id = f"dream_{uuid4().hex}"
    _emit(event_store, "dream.started", dream_id, {"requested": arguments})

    requested_skills = arguments.get("skills")
    if requested_skills is not None and (
        not isinstance(requested_skills, list)
        or not all(isinstance(skill, str) for skill in requested_skills)
    ):
        return _error("invalid_arguments", "dreaming.run skills must be a list of strings.")

    max_signals = arguments.get("max_signals", 20)
    if not isinstance(max_signals, int) or max_signals < 1:
        return _error("invalid_arguments", "dreaming.run max_signals must be positive.")

    selected = set(requested_skills or registry.loaded().keys())
    dream_input_builders = dream_input_builders or {}
    inputs: list[DreamInput] = []
    errors: list[str] = []
    attachments: dict[str, str] = {}

    for name, skill in registry.loaded().items():
        if name not in selected:
            continue
        if skill.dreams:
            attachments[name] = skill.dreams
        builder = dream_input_builders.get(name)
        if builder is None:
            continue
        try:
            dream_input = builder()
            if len(dream_input.signals) > max_signals:
                dream_input.signals = dream_input.signals[:max_signals]
            inputs.append(dream_input)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    total_signals = sum(len(dream_input.signals) for dream_input in inputs)
    report = DreamReport(
        id=dream_id,
        generated_at=datetime.now(UTC),
        status="completed" if not errors else "failed",
        summary=(
            f"Dream reviewed {len(inputs)} skill inputs and {total_signals} signals."
            if inputs
            else "Dream completed with no skill inputs."
        ),
        inputs=inputs,
        proposed_actions=[],
        errors=errors,
    )
    path = _report_path(context, dream_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    _emit(
        event_store,
        "dream.completed",
        dream_id,
        {
            "status": report.status,
            "input_count": len(inputs),
            "signal_count": total_signals,
            "report_path": str(path),
        },
    )

    return ToolResult(
        ok=not errors,
        summary=report.summary,
        data={
            "report": report.model_dump(mode="json"),
            "attachments": attachments,
            "path": str(path),
        },
        trust="skill_generated",
        artifacts=[{"type": "file", "path": str(path)}],
        events=[{"name": "dream.completed", "payload": {"id": dream_id}}],
        error=None
        if not errors
        else {"code": "dream_failed", "message": "; ".join(errors), "retryable": False},
    )


def _report_path(context: PermissionContext, dream_id: str) -> Path:
    return Path(context.variables()["$workspace"]) / "artifacts" / "dreams" / f"{dream_id}.json"


def _emit(
    event_store: EventStore | None, name: str, dream_id: str, payload: dict[str, Any]
) -> None:
    if event_store is None:
        return
    event_store.emit(
        name=name,
        origin="skill:dreaming",
        agent_id="system",
        session_id="dreaming",
        correlation_id=dream_id,
        payload={"dream_id": dream_id, **payload},
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
