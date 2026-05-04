"""Host-layer typed exceptions — replace bare SystemExit in library code."""

from __future__ import annotations


class AgentError(RuntimeError):
    """Agent not found, inactive, or not configured."""


class ProviderError(RuntimeError):
    """LLM provider or protocol not supported."""
