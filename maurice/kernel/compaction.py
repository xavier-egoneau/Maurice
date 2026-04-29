"""Automatic context compaction for long-running sessions.

Three levels triggered by estimated token usage relative to the context window:

  Level 1 — TRIM (60%): drop the oldest complete conversation turns, keep recent ones.
  Level 2 — SUMMARIZE (75%): LLM summary of the oldest turns, keep recent ones verbatim.
  Level 3 — RESET (90%): LLM summary of the entire session history.

Compaction is applied at the start of each turn, before the provider call.
Levels 2 and 3 require a provider; without one they fall back to TRIM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


CHARS_PER_TOKEN = 4


class CompactionLevel(IntEnum):
    NONE = 0
    TRIM = 1
    SUMMARIZE = 2
    RESET = 3


@dataclass
class CompactionConfig:
    context_window_tokens: int = 100_000
    trim_threshold: float = 0.60
    summarize_threshold: float = 0.75
    reset_threshold: float = 0.90
    keep_recent_turns: int = 10


def estimate_tokens(messages: list[dict[str, Any]], system: str = "") -> int:
    total = len(system) // CHARS_PER_TOKEN
    for m in messages:
        total += len(str(m.get("content") or "")) // CHARS_PER_TOKEN
    return total


def needed_level(tokens: int, config: CompactionConfig) -> CompactionLevel:
    ratio = tokens / max(config.context_window_tokens, 1)
    if ratio >= config.reset_threshold:
        return CompactionLevel.RESET
    if ratio >= config.summarize_threshold:
        return CompactionLevel.SUMMARIZE
    if ratio >= config.trim_threshold:
        return CompactionLevel.TRIM
    return CompactionLevel.NONE


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    config: CompactionConfig,
    system: str = "",
    provider: Any = None,
    model: str = "",
) -> tuple[list[dict[str, Any]], CompactionLevel]:
    """Return (compacted messages, level applied). NONE means no change."""
    tokens = estimate_tokens(messages, system)
    level = needed_level(tokens, config)
    if level == CompactionLevel.NONE:
        return messages, CompactionLevel.NONE

    turns = _group_by_turn(messages)
    system_prefix = [m for m in messages if m["role"] == "system"
                     and not (m.get("metadata") or {}).get("compacted")]

    if level == CompactionLevel.TRIM or provider is None:
        trimmed = _trim_turns(turns, config.keep_recent_turns, system_prefix)
        return trimmed, CompactionLevel.TRIM

    if level == CompactionLevel.SUMMARIZE:
        split = max(1, len(turns) - config.keep_recent_turns)
        old_turns, recent_turns = turns[:split], turns[split:]
    else:
        old_turns, recent_turns = turns, []

    summary = _summarize(old_turns, provider=provider, model=model)
    summary_msg: dict[str, Any] = {
        "role": "system",
        "content": f"[Session summary — earlier conversation]\n{summary}",
        "metadata": {"compacted": True},
    }
    recent = [m for turn in recent_turns for m in turn]
    return system_prefix + [summary_msg] + recent, level


# --- internals ---

def _group_by_turn(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group consecutive messages that share the same correlation_id into turns."""
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_cid: str | None = None

    for msg in messages:
        meta = msg.get("metadata") or {}
        cid = meta.get("correlation_id") or msg.get("correlation_id")
        if cid != last_cid and current:
            turns.append(current)
            current = []
        current.append(msg)
        last_cid = cid

    if current:
        turns.append(current)
    return turns


def _trim_turns(
    turns: list[list[dict[str, Any]]],
    keep: int,
    system_prefix: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(turns) <= keep:
        recent = [m for turn in turns for m in turn]
    else:
        recent = [m for turn in turns[-keep:] for m in turn]
    return system_prefix + [m for m in recent if m["role"] != "system"]


def _summarize(turns: list[list[dict[str, Any]]], *, provider: Any, model: str) -> str:
    lines: list[str] = []
    for turn in turns:
        for msg in turn:
            role = msg["role"]
            content = str(msg.get("content") or "")
            if role == "system":
                continue
            lines.append(f"[{role.upper()}] {content[:600]}")

    transcript = "\n".join(lines)
    prompt = (
        "Summarize the following conversation in 4–8 concise bullet points. "
        "Keep decisions, facts, completed actions, and important context. "
        "Omit greetings, filler, and failed attempts.\n\n"
        f"--- EXCERPT ---\n{transcript}\n--- END ---\n\nSummary:"
    )

    from maurice.kernel.contracts import ProviderChunkType

    text = ""
    try:
        for chunk in provider.stream(
            messages=[{"role": "user", "content": prompt, "metadata": {}}],
            model=model,
            tools=[],
            system="You are a concise session summarizer.",
            limits={"max_tool_iterations": 1},
        ):
            if chunk.type == ProviderChunkType.TEXT_DELTA:
                text += chunk.delta
    except Exception:
        pass

    return text.strip() or "[Session summary unavailable]"
