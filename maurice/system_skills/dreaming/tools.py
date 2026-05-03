"""Dreaming system skill tools."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maurice.kernel.contracts import DreamInput, DreamReport, ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillRegistry, import_skill_module

DreamInputBuilder = Callable[[], DreamInput]


def build_executors(ctx: Any) -> dict[str, Any]:
    registry: SkillRegistry = ctx.registry
    builders = _discover_dream_input_builders(
        registry,
        ctx.permission_context,
        all_skill_configs=ctx.all_skill_configs or {},
    )
    return dreaming_tool_executors(
        ctx.permission_context,
        registry,
        event_store=ctx.event_store,
        dream_input_builders=builders,
        agent_id=ctx.agent_id,
    )


def _discover_dream_input_builders(
    registry: SkillRegistry,
    context: PermissionContext,
    *,
    all_skill_configs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, DreamInputBuilder]:
    builders: dict[str, DreamInputBuilder] = {}
    all_skill_configs = all_skill_configs or {}
    for name, skill in registry.loaded().items():
        if not skill.manifest or not skill.manifest.dreams:
            continue
        input_builder_path = skill.manifest.dreams.input_builder
        if not input_builder_path:
            continue
        try:
            module_path, fn_name = input_builder_path.rsplit(".", 1)
            mod = import_skill_module(skill, module_path)
            fn = getattr(mod, fn_name)
            skill_config = all_skill_configs.get(name, {})
            builders[name] = lambda ctx=context, f=fn, cfg=skill_config: _call_input_builder(
                f,
                ctx,
                config=cfg,
                all_skill_configs=all_skill_configs,
            )
        except Exception:
            continue
    return builders


def _call_input_builder(
    fn: Any,
    context: PermissionContext,
    *,
    config: dict[str, Any],
    all_skill_configs: dict[str, dict[str, Any]],
) -> DreamInput:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(context)
    parameters = signature.parameters
    kwargs: dict[str, Any] = {}
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if accepts_kwargs or "config" in parameters:
        kwargs["config"] = config
    if accepts_kwargs or "all_skill_configs" in parameters:
        kwargs["all_skill_configs"] = all_skill_configs
    return fn(context, **kwargs)


def dreaming_tool_executors(
    context: PermissionContext,
    registry: SkillRegistry,
    *,
    event_store: EventStore | None = None,
    dream_input_builders: dict[str, DreamInputBuilder] | None = None,
    agent_id: str = "main",
) -> dict[str, Any]:
    return {
        "dreaming.run": lambda arguments: run(
            arguments,
            context,
            registry,
            event_store=event_store,
            dream_input_builders=dream_input_builders or {},
            agent_id=agent_id,
        ),
        "maurice.system_skills.dreaming.tools.run": lambda arguments: run(
            arguments,
            context,
            registry,
            event_store=event_store,
            dream_input_builders=dream_input_builders or {},
            agent_id=agent_id,
        ),
    }


def run(
    arguments: dict[str, Any],
    context: PermissionContext,
    registry: SkillRegistry,
    *,
    event_store: EventStore | None = None,
    dream_input_builders: dict[str, DreamInputBuilder] | None = None,
    agent_id: str = "main",
) -> ToolResult:
    dream_id = f"dream_{uuid4().hex}"
    _emit(event_store, "dream.started", dream_id, {"requested": arguments}, agent_id=agent_id)

    requested_skills = arguments.get("skills")
    if requested_skills is not None and (
        not isinstance(requested_skills, list)
        or not all(isinstance(skill, str) for skill in requested_skills)
    ):
        return _error("invalid_arguments", "dreaming.run skills must be a list of strings.")

    max_signals = arguments.get("max_signals", 20)
    if not isinstance(max_signals, int) or max_signals < 1:
        return _error("invalid_arguments", "dreaming.run max_signals must be positive.")

    include_memory = arguments.get("include_memory", True)
    if not isinstance(include_memory, bool):
        return _error("invalid_arguments", "dreaming.run include_memory must be a boolean.")

    selected = set(requested_skills or registry.loaded().keys())
    if include_memory and "memory" in registry.loaded():
        selected.add("memory")
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
        agent_id=agent_id,
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
    return Path(context.variables()["$agent_workspace"]) / "dreams" / f"{dream_id}.json"


def _emit(
    event_store: EventStore | None,
    name: str,
    dream_id: str,
    payload: dict[str, Any],
    *,
    agent_id: str,
) -> None:
    if event_store is None:
        return
    event_store.emit(
        name=name,
        origin="skill:dreaming",
        agent_id=agent_id,
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
