"""Generic monitoring snapshots for host/UI consumers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.config import load_workspace_config
from maurice.kernel.contracts import Event, MauriceModel
from maurice.kernel.events import EventStore
from maurice.kernel.runs import RunStore
from maurice.kernel.scheduler import JobStore
from maurice.kernel.skills import SkillLoader


class RuntimeSnapshot(MauriceModel):
    workspace_root: str
    runtime_root: str
    scheduler_enabled: bool
    gateway: dict[str, Any]
    channels: dict[str, Any] = Field(default_factory=dict)


class AgentSnapshot(MauriceModel):
    id: str
    status: str
    default: bool
    permission_profile: str
    skills: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class SkillHealthSnapshot(MauriceModel):
    name: str
    state: str
    errors: list[str] = Field(default_factory=list)
    suggested_fixes: list[str] = Field(default_factory=list)


class CountSnapshot(MauriceModel):
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)


class MonitoringSnapshot(MauriceModel):
    runtime: RuntimeSnapshot
    agents: list[AgentSnapshot]
    skills: list[SkillHealthSnapshot]
    approvals: CountSnapshot
    jobs: CountSnapshot
    runs: CountSnapshot
    events: list[Event]


def build_monitoring_snapshot(
    workspace_root: str | Path,
    *,
    agent_id: str | None = None,
    event_limit: int = 20,
) -> MonitoringSnapshot:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    event_stream = _event_stream(workspace, agent)
    event_store = EventStore(event_stream)
    registry = SkillLoader(
        bundle.host.skill_roots,
        enabled_skills=agent.skills or bundle.kernel.skills,
        agent_id=agent.id,
        session_id="monitoring",
    ).load()

    approvals = ApprovalStore(workspace / "agents" / agent.id / "approvals.json").list()
    jobs = JobStore(workspace / "agents" / agent.id / "jobs.json").list()
    runs = RunStore(
        workspace / "agents" / agent.id / "runs.json",
        workspace_root=workspace,
    ).list()

    return MonitoringSnapshot(
        runtime=RuntimeSnapshot(
            workspace_root=bundle.host.workspace_root,
            runtime_root=bundle.host.runtime_root,
            scheduler_enabled=bundle.kernel.scheduler.enabled,
            gateway=bundle.host.gateway.model_dump(mode="json"),
            channels=bundle.host.channels,
        ),
        agents=[
            AgentSnapshot(
                id=config.id,
                status=config.status,
                default=config.default,
                permission_profile=config.permission_profile,
                skills=config.skills,
                credentials=config.credentials,
                channels=config.channels,
            )
            for config in bundle.agents.agents.values()
        ],
        skills=[
            SkillHealthSnapshot(
                name=skill.name,
                state=skill.state,
                errors=skill.errors,
                suggested_fixes=skill.suggested_fixes,
            )
            for skill in registry.skills.values()
        ],
        approvals=_count_by_status([approval.status for approval in approvals]),
        jobs=_count_by_status([job.status for job in jobs]),
        runs=_count_by_status([run.state for run in runs]),
        events=event_store.read_all()[-event_limit:],
    )


def read_event_tail(
    workspace_root: str | Path,
    *,
    agent_id: str | None = None,
    limit: int = 20,
) -> list[Event]:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    agent = _resolve_agent(bundle, agent_id)
    return EventStore(_event_stream(workspace, agent)).read_all()[-limit:]


def _count_by_status(statuses) -> CountSnapshot:
    counts: dict[str, int] = {}
    for status in statuses:
        key = str(status)
        counts[key] = counts.get(key, 0) + 1
    return CountSnapshot(total=sum(counts.values()), by_status=counts)


def _resolve_agent(bundle, agent_id: str | None):
    if agent_id:
        try:
            return bundle.agents.agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"unknown agent: {agent_id}") from exc
    for agent in bundle.agents.agents.values():
        if agent.default and agent.status == "active":
            return agent
    if "main" in bundle.agents.agents:
        return bundle.agents.agents["main"]
    raise ValueError("no default agent configured")


def _event_stream(workspace: Path, agent) -> Path:
    return (
        Path(agent.event_stream).expanduser()
        if agent.event_stream
        else workspace / "agents" / agent.id / "events.jsonl"
    )
