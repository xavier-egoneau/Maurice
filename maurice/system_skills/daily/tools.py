"""Daily digest system skill tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maurice.host.delivery import _build_daily_digest, _latest_dream_report
from maurice.kernel.contracts import ToolResult
from maurice.kernel.skills import SkillRegistry


def build_executors(ctx: Any) -> dict[str, Any]:
    registry: SkillRegistry = ctx.registry
    return daily_tool_executors(
        ctx.permission_context,
        registry,
        agent_id=ctx.agent_id,
    )


def daily_tool_executors(context: Any, registry: SkillRegistry, *, agent_id: str = "main") -> dict[str, Any]:
    return {
        "daily.digest": lambda arguments: digest(arguments, context, registry, agent_id=agent_id),
        "maurice.system_skills.daily.tools.digest": lambda arguments: digest(
            arguments,
            context,
            registry,
            agent_id=agent_id,
        ),
    }


def digest(
    arguments: dict[str, Any],
    context: Any,
    registry: SkillRegistry,
    *,
    agent_id: str = "main",
) -> ToolResult:
    del arguments
    workspace = Path(context.variables()["$workspace"])
    daily_attachments = {
        name: skill.daily
        for name, skill in registry.loaded().items()
        if skill.daily
    }
    report = _latest_dream_report(workspace, agent_id)
    text = _build_daily_digest(workspace, agent_id, daily_attachments=daily_attachments)
    return ToolResult(
        ok=True,
        summary=text,
        data={
            "agent_id": agent_id,
            "daily_attachments": daily_attachments,
            "report": report,
        },
        trust="skill_generated",
        artifacts=[],
        events=[{"name": "daily.digest.created", "payload": {"agent_id": agent_id}}],
        error=None,
    )
