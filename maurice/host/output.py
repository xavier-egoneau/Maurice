"""Shared terminal formatting utilities."""

from __future__ import annotations

import sys


def _yes_no(value: bool) -> str:
    return "oui" if value else "non"


def _status_marker(status: str, frame: int = 0) -> str:
    if status == "occupe":
        spinner = "|/-\\"
        return f"{spinner[frame % 4]} actif"
    if status == "actif":
        return ". pret"
    if status in {"desactive", "archive"}:
        return f"- {status}"
    return f". {status}"


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(limit - 1, 0)] + "…"


def _ansi_padding(text: str) -> int:
    return len(_color(text, "1;38;5;208")) - len(text) if _supports_color() else 0


def _compact_text(value: str, limit: int) -> str:
    compacted = " ".join(str(value).split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _color(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _print_title(text: str) -> None:
    line = f" {text} "
    print(_color(line, "1;34"))


def _print_dim(text: str) -> None:
    print(_color(text, "2"))


def _print_host_checks(title: str, ok: bool, checks) -> None:
    status = "ok" if ok else "failed"
    print(f"{title}: {status}")
    for check in checks:
        print(f"- {check.name}: {check.state} - {check.summary}")

