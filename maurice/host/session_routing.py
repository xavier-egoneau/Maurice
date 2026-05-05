"""Session identity rules for Maurice conversation surfaces."""

from __future__ import annotations


def canonical_session_id(agent_id: str) -> str:
    """Return the single user-facing canonical session for an agent."""
    return f"telegram:{agent_id or 'main'}"


def is_canonical_session(session_id: str, agent_id: str) -> bool:
    """Whether a session belongs in the canonical conversation history."""
    return session_id == canonical_session_id(agent_id)


def system_session_ids() -> set[str]:
    return {"daily", "dreaming", "sentinelle", "reminders"}
