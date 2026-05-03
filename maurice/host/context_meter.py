"""Shared context usage display helpers for host channels."""

from __future__ import annotations

from typing import Any


DEFAULT_CONTEXT_WINDOW = 250_000
MAX_BAR_FRACTION = 0.8


def context_usage(
    input_tokens: int,
    output_tokens: int,
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> dict[str, Any] | None:
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    window = max(1, int(context_window or DEFAULT_CONTEXT_WINDOW))
    total = max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))
    ratio = total / window
    return {
        "input_tokens": max(0, int(input_tokens or 0)),
        "output_tokens": max(0, int(output_tokens or 0)),
        "total_tokens": total,
        "context_window_tokens": window,
        "ratio": ratio,
        "percent": round(ratio * 100),
        "visual_ratio": min(max(ratio, 0.0), 1.0),
        "level": context_level(ratio),
    }


def context_level(ratio: float) -> str:
    if ratio >= 0.8:
        return "high"
    if ratio >= 0.5:
        return "medium"
    return "low"


def context_summary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return ""
    return (
        f"context {int(usage['total_tokens']):,} / "
        f"{int(usage['context_window_tokens']):,} tokens ({int(usage['percent'])}%)"
    )


def context_bar(usage: dict[str, Any], *, available_width: int, min_width: int = 10) -> str:
    max_width = max(min_width, int(max(1, available_width) * MAX_BAR_FRACTION))
    filled = round(float(usage.get("visual_ratio") or 0.0) * max_width)
    filled = max(0, min(max_width, filled))
    return "█" * filled + "░" * (max_width - filled)
