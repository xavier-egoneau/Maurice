"""Dashboard view models and generic row builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from maurice.host.monitoring import build_monitoring_snapshot
from maurice.kernel.config import ConfigBundle, load_workspace_config
from maurice.kernel.contracts import Event, MauriceModel
from maurice.kernel.events import EventStore
from maurice.kernel.runs import RunStore
from maurice.kernel.scheduler import JobStore
from maurice.kernel.session import SessionRecord
from maurice.kernel.skills import SkillLoader


ACTIVE_EVENT_GRACE = timedelta(seconds=12)
ACTIVE_STALE_THRESHOLD = timedelta(minutes=10)


class DashboardStatus(MauriceModel):
    service: str
    automatismes: str
    telegram: str
    modele: str


class AgentDashboardRow(MauriceModel):
    agent_id: str
    label: str
    model: str
    status: Literal["actif", "inactif", "occupe", "desactive", "archive"]
    permission: str
    access: str
    activity: bool = False


class AutomationDashboardRow(MauriceModel):
    job_id: str
    name: str
    owner_agent: str
    status: str
    enabled: bool
    next_run: str
    recurrence: str
    last_problem: str = "-"


class SessionDashboardRow(MauriceModel):
    session_id: str
    agent_id: str
    origin: str
    status: str
    last_event: str
    updated_at: str


class ModelDashboardRow(MauriceModel):
    agent_id: str
    provider: str
    model: str
    auth_state: str


class PermissionDashboardRow(MauriceModel):
    agent_id: str
    current_profile: str
    global_maximum: str
    escalation: str


class SkillDashboardRow(MauriceModel):
    agent_id: str
    name: str
    source: Literal["system", "user"]
    enabled: bool
    state: str
    issues: str = "-"


class LogDashboardRow(MauriceModel):
    time: str
    level: Literal["info", "warning", "error"]
    source: str
    agent_id: str
    session_id: str
    message: str


class DashboardSnapshot(MauriceModel):
    status: DashboardStatus
    agents: list[AgentDashboardRow] = Field(default_factory=list)
    automations: list[AutomationDashboardRow] = Field(default_factory=list)
    sessions: list[SessionDashboardRow] = Field(default_factory=list)
    models: list[ModelDashboardRow] = Field(default_factory=list)
    permissions: list[PermissionDashboardRow] = Field(default_factory=list)
    skills: list[SkillDashboardRow] = Field(default_factory=list)
    logs: list[LogDashboardRow] = Field(default_factory=list)


def build_dashboard_snapshot(
    workspace_root: str | Path,
    *,
    agent_id: str | None = None,
    event_limit: int = 40,
) -> DashboardSnapshot:
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    selected_agent = _resolve_agent_id(bundle, agent_id)
    monitor = build_monitoring_snapshot(workspace, agent_id=selected_agent, event_limit=event_limit)
    events = _all_agent_events(workspace, bundle)
    active_agents = _active_agents(events)

    return DashboardSnapshot(
        status=DashboardStatus(
            service=f"{monitor.runtime.gateway.get('host')}:{monitor.runtime.gateway.get('port')}",
            automatismes="actifs" if monitor.runtime.scheduler_enabled else "arretes",
            telegram="actif" if _telegram_configured(bundle) else "non configure",
            modele=_model_label(bundle, selected_agent),
        ),
        agents=_agent_rows(bundle, active_agents),
        automations=_automation_rows(workspace, bundle),
        sessions=_session_rows(workspace, events),
        models=_model_rows(bundle),
        permissions=_permission_rows(bundle),
        skills=_skill_rows(bundle),
        logs=_log_rows(events[-event_limit:]),
    )


def _agent_rows(bundle: ConfigBundle, active_agents: set[str]) -> list[AgentDashboardRow]:
    rows = []
    for agent in bundle.agents.agents.values():
        status = _agent_status(agent.status, agent.id in active_agents)
        rows.append(
            AgentDashboardRow(
                agent_id=agent.id,
                label=f"{'* ' if agent.default else ''}{agent.id}",
                model=_model_label(bundle, agent.id),
                status=status,
                permission=agent.permission_profile,
                access=", ".join(agent.channels) or "-",
                activity=agent.id in active_agents,
            )
        )
    return rows


def _automation_rows(workspace: Path, bundle: ConfigBundle) -> list[AutomationDashboardRow]:
    rows = []
    for agent in bundle.agents.agents.values():
        store = JobStore(workspace / "agents" / agent.id / "jobs.json")
        for job in store.list():
            if str(job.status) == "completed" and not job.recurring:
                continue
            rows.append(
                AutomationDashboardRow(
                    job_id=job.id,
                    name=job.name,
                    owner_agent=agent.id,
                    status=str(job.status),
                    enabled=str(job.status) not in {"cancelled", "completed", "failed"},
                    next_run=_format_dt(job.run_at),
                    recurrence=_format_interval(job.interval_seconds),
                    last_problem=job.last_error or "-",
                )
            )
    return rows


def _session_rows(workspace: Path, events: list[Event]) -> list[SessionDashboardRow]:
    by_key: dict[tuple[str, str], Event] = {}
    for event in events:
        by_key[(event.agent_id, event.session_id)] = event

    rows: dict[tuple[str, str], SessionDashboardRow] = {}
    for path in sorted((workspace / "sessions").glob("*/*.json")):
        try:
            session = SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        key = (session.agent_id, session.id)
        last_event = by_key.get(key)
        rows[key] = SessionDashboardRow(
            session_id=session.id,
            agent_id=session.agent_id,
            origin=_session_origin(session.id),
            status=_session_status(session),
            last_event=last_event.name if last_event else "-",
            updated_at=_format_dt(session.updated_at),
        )

    for key, event in by_key.items():
        if key in rows:
            continue
        rows[key] = SessionDashboardRow(
            session_id=event.session_id,
            agent_id=event.agent_id,
            origin=_session_origin(event.session_id),
            status="actif" if event.name.endswith(".started") else "recent",
            last_event=event.name,
            updated_at=_format_dt(event.time),
        )
    return sorted(rows.values(), key=lambda row: row.updated_at, reverse=True)


def _model_rows(bundle: ConfigBundle) -> list[ModelDashboardRow]:
    rows = []
    for agent in bundle.agents.agents.values():
        model = _effective_model(bundle, agent.id)
        provider = str(model.get("provider") or "mock")
        rows.append(
            ModelDashboardRow(
                agent_id=agent.id,
                provider=provider,
                model=_model_label(bundle, agent.id),
                auth_state="configure" if model.get("credential") or provider in {"mock", "auth"} else "a verifier",
            )
        )
    return rows


def _permission_rows(bundle: ConfigBundle) -> list[PermissionDashboardRow]:
    return [
        PermissionDashboardRow(
            agent_id=agent.id,
            current_profile=agent.permission_profile,
            global_maximum=bundle.kernel.permissions.profile,
            escalation="confirmation requise" if _profile_rank(agent.permission_profile) > _profile_rank(bundle.kernel.permissions.profile) else "ok",
        )
        for agent in bundle.agents.agents.values()
    ]


def _skill_rows(bundle: ConfigBundle) -> list[SkillDashboardRow]:
    rows = []
    for agent in bundle.agents.agents.values():
        registry = SkillLoader(
            bundle.host.skill_roots,
            enabled_skills=agent.skills or bundle.kernel.skills,
            scope="global",
            agent_id=agent.id,
            session_id="dashboard",
        ).load()
        for skill in registry.skills.values():
            rows.append(
                SkillDashboardRow(
                    agent_id=agent.id,
                    name=skill.name,
                    source=skill.origin,
                    enabled=skill.state != "disabled",
                    state=str(skill.state),
                    issues="; ".join(skill.errors + skill.suggested_fixes) or "-",
                )
            )
    return rows


def _log_rows(events: list[Event]) -> list[LogDashboardRow]:
    return [
        LogDashboardRow(
            time=event.time.strftime("%H:%M:%S"),
            level=_event_level(event),
            source=event.origin,
            agent_id=event.agent_id,
            session_id=event.session_id,
            message=event.name,
        )
        for event in events
    ]


def _all_agent_events(workspace: Path, bundle: ConfigBundle) -> list[Event]:
    events: list[Event] = []
    for agent in bundle.agents.agents.values():
        events.extend(EventStore(Path(agent.event_stream)).read_all())
    return sorted(events, key=lambda event: event.time)


def _active_agents(events: list[Event]) -> set[str]:
    active: set[str] = set()
    last_started_at: dict[str, datetime] = {}
    recent_activity: dict[str, datetime] = {}
    now = datetime.now(UTC)
    for event in events:
        if event.name.endswith(".started") or event.name in {"turn.started", "job.started", "run.started"}:
            active.add(event.agent_id)
            last_started_at[event.agent_id] = event.time
        elif event.name.endswith(".completed") or event.name.endswith(".failed") or event.name.endswith(".cancelled"):
            active.discard(event.agent_id)
        if _is_activity_event(event):
            recent_activity[event.agent_id] = event.time
    for agent_id in list(active):
        started = last_started_at.get(agent_id)
        if started is not None and now - _as_utc(started) > ACTIVE_STALE_THRESHOLD:
            active.discard(agent_id)
    for agent_id, last_seen in recent_activity.items():
        if now - _as_utc(last_seen) <= ACTIVE_EVENT_GRACE:
            active.add(agent_id)
    return active


def _is_activity_event(event: Event) -> bool:
    if event.name in {
        "gateway.message.received",
        "gateway.message.sent",
        "channel.delivery.succeeded",
        "turn.started",
        "turn.completed",
        "job.started",
        "job.completed",
        "run.started",
        "run.completed",
    }:
        return True
    return event.name.endswith(".started") or event.name.endswith(".completed")


def _resolve_agent_id(bundle: ConfigBundle, agent_id: str | None) -> str:
    if agent_id:
        return agent_id
    for agent in bundle.agents.agents.values():
        if agent.default:
            return agent.id
    return next(iter(bundle.agents.agents), "main")


def _model_label(bundle: ConfigBundle, agent_id: str) -> str:
    model = _effective_model(bundle, agent_id)
    provider = model.get("provider") or "mock"
    name = model.get("name") or provider
    protocol = model.get("protocol")
    if protocol:
        return f"{provider}/{protocol}"
    return f"{provider}/{name}"


def _effective_model(bundle: ConfigBundle, agent_id: str) -> dict:
    agent = bundle.agents.agents.get(agent_id)
    override = agent.model if agent else None
    if isinstance(override, dict):
        return override
    return bundle.kernel.model.model_dump(mode="json")


def _agent_status(raw: str, active: bool) -> str:
    if active:
        return "occupe"
    if raw == "disabled":
        return "desactive"
    if raw == "archived":
        return "archive"
    if raw == "active":
        return "actif"
    return "inactif"


def _session_status(session: SessionRecord) -> str:
    if session.turns and session.turns[-1].status == "running":
        return "actif"
    return "recent"


def _session_origin(session_id: str) -> str:
    if session_id.startswith("telegram:"):
        return "Telegram"
    if session_id.startswith("local_http:"):
        return "Local"
    if session_id in {"scheduler", "dreaming", "reminders"}:
        return "Automatisme"
    if session_id == "default":
        return "Terminal"
    return "Session"


def _event_level(event: Event) -> Literal["info", "warning", "error"]:
    name = event.name.lower()
    if "failed" in name or "error" in name or event.payload.get("error"):
        return "error"
    if "denied" in name or "cancelled" in name or "missing" in name:
        return "warning"
    return "info"


def _telegram_configured(bundle: ConfigBundle) -> bool:
    telegram = bundle.host.channels.get("telegram")
    return isinstance(telegram, dict) and telegram.get("enabled", True) is not False


def _format_dt(value: datetime) -> str:
    return _as_utc(value).astimezone().strftime("%Y-%m-%d %H:%M")


def _format_interval(seconds: int | None) -> str:
    if not seconds:
        return "-"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _profile_rank(profile: str) -> int:
    return {"safe": 0, "limited": 1, "power": 2}.get(profile, 0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
