"""Autonomous command continuation loop."""

from __future__ import annotations

import sys
from time import monotonic
from typing import Any, Callable

from maurice.host.autonomy_progress import AutonomyProgress, ProgressCallback, combine_callbacks
from maurice.kernel.loop import TurnResult

RunTurn = Callable[..., TurnResult]

_WRITE_MARKERS = ("écrit", "ecrit", "deplace", "déplacé", "cree", "créé", "mkdir", "wrote", "created", "moved")

_DEFAULT_CONTINUE_PROMPT = (
    "Continue en mode autonome. Tu viens d'annoncer une action sans la realiser. "
    "Avance concretement dans le projet actif avec les capacites disponibles. "
    "Si tu es bloque, explique le blocage precis."
)


def run_autonomous_command(
    *,
    run_turn: RunTurn,
    initial_turn: TurnResult,
    session_id: str,
    agent_id: str,
    correlation_id: str,
    source_channel: str | None,
    source_peer_id: str | None,
    source_metadata: dict[str, Any],
    agent_limits: dict[str, Any],
    command_name: str,
    autonomy_config: dict[str, Any],
    original_text: str,
    cancel_event: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[TurnResult, bool]:
    """Run the autonomous continuation loop after an initial command turn.

    Returns:
        (final_turn, any_tool_activity)
    """
    max_continuations = _positive_int(autonomy_config.get("max_continuations"), default=0)
    max_seconds = _positive_int(autonomy_config.get("max_seconds"), default=0)
    max_consecutive_announce = _positive_int(
        autonomy_config.get("max_consecutive_announce"), default=3
    )
    continue_without_activity = autonomy_config.get("continue_without_activity") is True
    continue_prompt = autonomy_config.get("continue_prompt") or ""
    if not isinstance(continue_prompt, str) or not continue_prompt.strip():
        continue_prompt = _DEFAULT_CONTINUE_PROMPT

    turn = initial_turn
    tool_activity = bool(turn.tool_results)
    any_tool_activity = tool_activity
    _log_autonomy_turn(command_name, 0, turn)
    continuation_count = 0
    consecutive_no_action = 0 if tool_activity else 1
    started_at = monotonic()

    while (
        _turn_completed(turn)
        and not _cancel_requested(cancel_event)
        and continuation_count < max_continuations
        and (not tool_activity or continue_without_activity)
        and consecutive_no_action < max_consecutive_announce
        and (max_seconds <= 0 or monotonic() - started_at < max_seconds)
        and should_continue_autonomous_command(
            turn.assistant_text,
            continue_without_activity=continue_without_activity,
        )
    ):
        continuation_count += 1
        turn = run_turn(
            message=continue_prompt,
            session_id=session_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            source_channel=source_channel,
            source_peer_id=source_peer_id,
            source_metadata={
                **source_metadata,
                "command": command_name,
                "original_text": original_text,
                "autonomy_continuation": continuation_count,
            },
            limits=agent_limits,
            message_metadata={
                "autonomy_internal": True,
                "command": command_name,
                "visible_user_message": original_text,
                "autonomy_continuation": continuation_count,
            },
            cancel_event=cancel_event,
        )
        tool_activity = bool(turn.tool_results)
        any_tool_activity = any_tool_activity or tool_activity
        consecutive_no_action = 0 if tool_activity else consecutive_no_action + 1
        _log_autonomy_turn(command_name, continuation_count, turn)
        if progress_callback is not None:
            _emit_progress(
                progress_callback, turn, continuation_count,
                max_turns=max_continuations,
                started_at=started_at,
                command_name=command_name,
                session_id=session_id,
                agent_id=agent_id,
                is_done=False,
            )
        if not _turn_completed(turn) or _cancel_requested(cancel_event):
            break

    if progress_callback is not None:
        _emit_progress(
            progress_callback, turn, continuation_count,
            max_turns=max_continuations,
            started_at=started_at,
            command_name=command_name,
            session_id=session_id,
            agent_id=agent_id,
            is_done=True,
        )

    return turn, any_tool_activity


def _turn_completed(turn: Any) -> bool:
    return str(getattr(turn, "status", "completed") or "completed") == "completed"


def _cancel_requested(cancel_event: Any | None) -> bool:
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def should_continue_autonomous_command(
    text: str, *, continue_without_activity: bool = False
) -> bool:
    """True if the autonomous loop should fire another turn."""
    import re
    match = re.search(
        r"<turn_status>\s*(done|continue|blocked)\s*</turn_status>",
        text or "",
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower() == "continue"

    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return True
    if "?" in normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "bloque", "blocage", "blocked", "blocking",
            "besoin de", "il manque", "je ne peux pas",
            "il n'y a plus", "il n y a plus", "no more tasks",
            "pas de tache", "pas de tâche", "aucune tache", "aucune tâche",
        )
    ):
        return False
    done_markers = (
        "c'est fait", "c'est fini", "j'ai terminé", "j'ai termine",
        "tout est fait", "tout est terminé", "tout est termine",
        "tâche terminée", "tache terminee", "mission accomplie",
        "all done", "travail terminé", "travail termine",
    )
    if any(marker in normalized for marker in done_markers):
        return False
    if continue_without_activity:
        return True
    intent_prefixes = (
        "je commence",
        "je vais",
        "je verifie",
        "je vérifie",
        "je corrige",
        "je cree",
        "je crée",
        "je reprends",
        "je passe",
        "je demarre",
        "je démarre",
        "je lance",
    )
    if normalized.startswith(intent_prefixes):
        return True
    return normalized.endswith(":")


def _positive_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _turn_write_count(turn: Any) -> int:
    return sum(
        1 for r in turn.tool_results
        if r.ok and any(w in (r.summary or "").lower() for w in _WRITE_MARKERS)
    )


def _log_autonomy_turn(command: str, turn_number: int, turn: Any) -> None:
    tool_count = len(turn.tool_results)
    ok_count = sum(1 for r in turn.tool_results if r.ok)
    err_count = tool_count - ok_count
    error_codes = [r.error.code for r in turn.tool_results if r.error and r.error.code]
    write_count = _turn_write_count(turn)
    text_preview = (turn.assistant_text or "").strip().replace("\n", " ")[:80]
    parts = [f"[autonomy] {command} tour={turn_number} outils={ok_count}/{tool_count}"]
    if write_count:
        parts.append(f"ecritures={write_count}")
    if err_count:
        parts.append(f"erreurs={err_count} codes={error_codes}")
    if text_preview:
        parts.append(f"texte={text_preview!r}")
    print(" ".join(parts), file=sys.stderr, flush=True)


def _emit_progress(
    progress_callback: ProgressCallback,
    turn: Any,
    turn_number: int,
    *,
    max_turns: int,
    started_at: float,
    command_name: str,
    session_id: str,
    agent_id: str,
    is_done: bool,
) -> None:
    tool_count = len(turn.tool_results)
    ok_count = sum(1 for r in turn.tool_results if r.ok)
    text = (turn.assistant_text or "").strip().replace("\n", " ")
    status = str(getattr(turn, "status", "completed") or "completed")
    is_blocked = status == "completed" and not should_continue_autonomous_command(turn.assistant_text)
    try:
        progress_callback(AutonomyProgress(
            command=command_name,
            turn=turn_number,
            max_turns=max_turns,
            elapsed_seconds=monotonic() - started_at,
            tool_count=tool_count,
            tool_ok_count=ok_count,
            write_count=_turn_write_count(turn),
            error_count=tool_count - ok_count,
            assistant_text_preview=text[:120],
            is_blocked=is_blocked,
            is_done=is_done,
            session_id=session_id,
            agent_id=agent_id,
            status=status,
        ))
    except Exception:
        pass
