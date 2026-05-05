"""Progress tracking for long-running autonomous command loops."""

from __future__ import annotations

import dataclasses
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class AutonomyProgress:
    command: str
    turn: int
    max_turns: int
    elapsed_seconds: float
    tool_count: int
    tool_ok_count: int
    write_count: int        # tools that wrote / created / moved something
    error_count: int
    assistant_text_preview: str  # first ~120 chars
    is_blocked: bool
    is_done: bool           # True only on the final call after the loop
    session_id: str
    agent_id: str
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


ProgressCallback = Callable[[AutonomyProgress], None]


class SessionProgressStore:
    """Thread-safe store linking the autonomy loop (writer) to SSE handlers (readers).

    One Queue per active session. The autonomy loop pushes AutonomyProgress objects.
    SSE handlers block on get() and stream each object as a JSON event.
    A None sentinel signals end-of-run and causes SSE handlers to close.
    """

    def __init__(self) -> None:
        self._queues: dict[str, queue.Queue[AutonomyProgress | None]] = {}
        self._lock = threading.Lock()

    def open(self, session_id: str) -> queue.Queue[AutonomyProgress | None]:
        """Create and return the queue for a session. Call before starting the run."""
        q: queue.Queue[AutonomyProgress | None] = queue.Queue(maxsize=256)
        with self._lock:
            self._queues[session_id] = q
        return q

    def get_queue(self, session_id: str) -> queue.Queue[AutonomyProgress | None] | None:
        with self._lock:
            return self._queues.get(session_id)

    def push(self, session_id: str, event: "AutonomyProgress | dict[str, Any]") -> None:
        """Push a progress update or a raw dict event (e.g. text_delta).
        No-op if the session queue is full or closed."""
        with self._lock:
            q = self._queues.get(session_id)
        if q is not None:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def close(self, session_id: str) -> None:
        """Signal end-of-run via sentinel None, then remove the queue."""
        with self._lock:
            q = self._queues.pop(session_id, None)
        if q is not None:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


def combine_callbacks(*callbacks: ProgressCallback | None) -> ProgressCallback | None:
    """Return a single callback that calls all non-None callbacks in order.

    Individual callback exceptions are swallowed so one failing backend
    (e.g. Telegram network error) does not break the autonomy loop.
    """
    active = [cb for cb in callbacks if cb is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def combined(progress: AutonomyProgress) -> None:
        for cb in active:
            try:
                cb(progress)
            except Exception:
                pass

    return combined
