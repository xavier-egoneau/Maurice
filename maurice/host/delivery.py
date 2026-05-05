"""Reminder and daily digest delivery utilities."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from maurice.kernel.config import load_workspace_config
from maurice.kernel.events import EventStore
from maurice.kernel.scheduler import JobStore
from maurice.kernel.session import SessionStore
from maurice.host.session_routing import canonical_session_id
from maurice.host.telegram import (
    _credential_value,
    _int_list,
    _telegram_channel_configs,
    _telegram_send_message,
)


def _schedule_reminder_callback(
    workspace: Path,
    agent_id: str,
    *,
    session_id: str,
    source_channel: str | None,
    source_peer_id: str | None,
    source_metadata: dict[str, Any] | None,
):
    def schedule(payload: dict[str, object]) -> str:
        target_agent_id = str(payload.get("agent_id") or agent_id)
        store = JobStore(workspace / "agents" / target_agent_id / "jobs.json")
        metadata = source_metadata or {}
        job = store.schedule(
            name="reminders.fire",
            owner="skill:reminders",
            run_at=payload["run_at"],
            interval_seconds=payload.get("interval_seconds")
            if isinstance(payload.get("interval_seconds"), int)
            else None,
            payload={
                "agent_id": target_agent_id,
                "session_id": session_id,
                "channel": source_channel,
                "peer_id": source_peer_id,
                "chat_id": metadata.get("chat_id"),
                "arguments": {"reminder_id": payload["reminder_id"]},
            },
        )
        return job.id

    return schedule


def _deliver_reminder_result(workspace: Path, payload: dict[str, Any], text: str) -> None:
    agent_id = str(payload.get("agent_id") or "main")
    session_id = str(payload.get("session_id") or "reminders")
    store = SessionStore(workspace / "sessions")
    if session_id and session_id != "reminders":
        _append_reminder_message(store, agent_id, session_id, text)

    bundle = load_workspace_config(workspace)
    for target in _telegram_reminder_targets(workspace, bundle, agent_id, payload):
        _telegram_send_message(target["token"], target["chat_id"], text)
        target_session_id = target.get("session_id")
        if isinstance(target_session_id, str) and target_session_id and target_session_id != session_id:
            _append_reminder_message(store, agent_id, target_session_id, text)


def _append_reminder_message(store: SessionStore, agent_id: str, session_id: str, text: str) -> None:
    try:
        store.load(agent_id, session_id)
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)
    store.append_message(
        agent_id,
        session_id,
        role="assistant",
        content=text,
        metadata={"reminder": True},
    )


def _telegram_reminder_targets(
    workspace: Path,
    bundle,
    agent_id: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add_target(token: str, chat_id: int, session_id: str | None) -> None:
        if not token:
            return
        key = (token, chat_id)
        if key in seen:
            return
        seen.add(key)
        targets.append({"token": token, "chat_id": chat_id, "session_id": session_id})

    source_chat_id = payload.get("chat_id")
    if payload.get("channel") == "telegram" and isinstance(source_chat_id, int):
        channel_name = str(payload.get("telegram_channel") or "telegram")
        telegram = bundle.host.channels.get(channel_name)
        if not isinstance(telegram, dict):
            telegram = bundle.host.channels.get("telegram")
        if isinstance(telegram, dict):
            token = _credential_value(workspace, str(telegram.get("credential") or "telegram_bot"))
            source_session = canonical_session_id(agent_id)
            add_target(token, source_chat_id, source_session)

    for _channel_name, telegram in _telegram_channel_configs(bundle):
        if telegram.get("enabled", True) is False:
            continue
        if str(telegram.get("agent") or "main") != agent_id:
            continue
        token = _credential_value(workspace, str(telegram.get("credential") or "telegram_bot"))
        for user_id in _int_list(telegram.get("allowed_users")):
            add_target(token, user_id, canonical_session_id(agent_id))
        for chat_id in _int_list(telegram.get("allowed_chats")):
            add_target(token, chat_id, canonical_session_id(agent_id))
    return targets


def _build_daily_digest(
    workspace: Path,
    agent_id: str,
    *,
    daily_attachments: dict[str, str] | None = None,
) -> str:
    report = _latest_dream_report(workspace, agent_id)
    today = datetime.now().astimezone().strftime("%d/%m/%Y")
    lines = [f"Bonjour, voici ton daily Maurice du {today}."]
    daily_attachments = daily_attachments or {}
    if not report:
        lines.extend([
            "",
            "Le dreaming n'a pas encore produit de rapport exploitable.",
            "Je garde le daily actif pour les prochains matins.",
        ])
        if daily_attachments:
            lines.extend(["", "Contributions daily disponibles :"])
            lines.extend(f"- {name}" for name in sorted(daily_attachments))
        return "\n".join(lines)

    generated_at = _human_datetime(report.get("generated_at"))
    inputs = report.get("inputs") if isinstance(report.get("inputs"), list) else []
    signals = [
        signal
        for dream_input in inputs
        if isinstance(dream_input, dict)
        for signal in (dream_input.get("signals") if isinstance(dream_input.get("signals"), list) else [])
        if isinstance(signal, dict)
    ]
    actions = report.get("proposed_actions") if isinstance(report.get("proposed_actions"), list) else []
    lines.extend(["", f"Dreaming: {len(inputs)} source(s), {len(signals)} signal(aux)."])
    if generated_at:
        lines.append(f"Dernier passage: {generated_at}.")
    summaries = [str(signal.get("summary") or "").strip() for signal in signals]
    summaries = [s for s in summaries if s][:5]
    if summaries:
        lines.extend(["", "A garder en tete :"])
        lines.extend(f"- {s}" for s in summaries)
    if actions:
        lines.extend(["", "Actions candidates :"])
        for action in actions[:5]:
            if isinstance(action, dict):
                summary = str(action.get("summary") or action.get("type") or "").strip()
                if summary:
                    lines.append(f"- {summary}")
    if not summaries and not actions:
        lines.extend(["", "Rien de particulier a remonter pour l'instant."])
    if daily_attachments:
        lines.extend(["", "Contributions daily prises en compte :"])
        lines.extend(f"- {name}" for name in sorted(daily_attachments))
    lines.extend(["", f"Agent: {agent_id}"])
    return "\n".join(lines)


def _latest_dream_report(workspace: Path, agent_id: str) -> dict[str, Any] | None:
    dreams_dir = workspace / "agents" / agent_id / "dreams"
    try:
        paths = sorted(dreams_dir.glob("dream_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _human_datetime(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone().strftime("%d/%m/%Y %H:%M")


def _deliver_daily_digest(
    workspace: Path,
    payload: dict[str, Any],
    text: str,
    *,
    event_store: EventStore | None = None,
) -> None:
    agent_id = str(payload.get("agent_id") or "main")
    session_id = str(payload.get("session_id") or "daily")
    store = SessionStore(workspace / "sessions")
    try:
        store.load(agent_id, session_id)
    except FileNotFoundError:
        store.create(agent_id, session_id=session_id)
    store.append_message(
        agent_id, session_id,
        role="assistant",
        content=text,
        metadata={"daily": True},
    )
    _emit_daily_event(event_store, "daily.digest.created", agent_id, session_id, {"length": len(text)})

    bundle = load_workspace_config(workspace)
    telegram = bundle.host.channels.get("telegram")
    if not isinstance(telegram, dict) or not telegram.get("enabled", True):
        return
    credential_name = str(telegram.get("credential") or "telegram_bot")
    token = _credential_value(workspace, credential_name)
    if not token:
        return
    targets = _telegram_daily_targets(workspace, agent_id, token, telegram)
    if not targets:
        _emit_daily_event(
            event_store,
            "daily.digest.delivery_skipped",
            agent_id,
            session_id,
            {"channel": "telegram", "reason": "no_telegram_target"},
        )
        return
    for target in targets:
        chat_id = target["chat_id"]
        try:
            _telegram_send_message(token, chat_id, text)
            _emit_daily_event(
                event_store, "daily.digest.delivered", agent_id, session_id,
                {"channel": "telegram", "chat_id": chat_id},
            )
        except Exception as exc:
            _emit_daily_event(
                event_store, "daily.digest.delivery_failed", agent_id, session_id,
                {"channel": "telegram", "chat_id": chat_id, "error": str(exc)},
            )


def _telegram_daily_targets(
    workspace: Path,
    agent_id: str,
    token: str,
    telegram: dict[str, Any],
) -> list[dict[str, Any]]:
    chat_ids = sorted(set(_int_list(telegram.get("allowed_chats")) + _int_list(telegram.get("allowed_users"))))
    if not chat_ids:
        remembered = _remembered_telegram_chat_id(workspace, agent_id)
        if remembered is not None:
            chat_ids = [remembered]
    return [{"token": token, "chat_id": chat_id, "session_id": canonical_session_id(agent_id)} for chat_id in chat_ids]


def _remembered_telegram_chat_id(workspace: Path, agent_id: str) -> int | None:
    store = SessionStore(workspace / "sessions")
    try:
        session = store.load(agent_id, canonical_session_id(agent_id))
    except FileNotFoundError:
        session = None
    telegram = session.metadata.get("telegram") if session is not None else None
    if isinstance(telegram, dict) and isinstance(telegram.get("chat_id"), int):
        return int(telegram["chat_id"])

    legacy_ids: list[int] = []
    for candidate in store.list(agent_id):
        prefix, separator, suffix = candidate.id.partition(":")
        if prefix != "telegram" or not separator or suffix == agent_id:
            continue
        try:
            legacy_ids.append(int(suffix))
        except ValueError:
            continue
    if len(legacy_ids) == 1:
        return legacy_ids[0]
    return None


def _emit_daily_event(
    event_store: EventStore | None,
    name: str,
    agent_id: str,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    if event_store is None:
        return
    event_store.emit(
        name=name,
        kind="progress",
        origin="skill:daily",
        agent_id=agent_id,
        session_id=session_id,
        payload=payload,
    )


def _cancel_job_callback(workspace: Path, agent_id: str):
    def cancel(job_id: str) -> None:
        try:
            JobStore(workspace / "agents" / agent_id / "jobs.json").cancel(job_id)
        except KeyError:
            return
    return cancel
