"""Two-stage LLM approval classifier with cache and circuit breaker.

Used by the 'auto' approval mode to automatically decide whether a tool call
needs human approval.

Stage 1: binary <block>yes/no</block> in ≤64 tokens, temperature 0.
Stage 2: chain-of-thought reason (≤400 tokens), triggered only on block.

The circuit breaker falls back to normal 'ask' mode after:
  - 3 consecutive blocks, or
  - 20 total blocks in the session.

Dangerous permission classes (shell.exec, runtime.write, host.control) always
re-evaluate even when a cached approval exists.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


DANGEROUS_CLASSES = {"shell.exec", "runtime.write", "host.control"}


@dataclass(frozen=True)
class ClassifierDecision:
    block: bool
    reason: str
    stage: int
    cached: bool = False


@dataclass(frozen=True)
class ClassifierOutcome:
    """Envelope returned by AgentLoop._run_classifier."""
    decision: ClassifierDecision | None
    circuit_open: bool = False

    @property
    def ran(self) -> bool:
        """True if the classifier produced a decision (not absent, not circuit-open)."""
        return self.decision is not None and not self.circuit_open


@dataclass
class CircuitBreaker:
    consecutive_limit: int = 3
    total_limit: int = 20
    _consecutive: int = field(default=0, repr=False)
    _total: int = field(default=0, repr=False)
    _open: bool = field(default=False, repr=False)

    @property
    def is_open(self) -> bool:
        return self._open

    def record(self, blocked: bool) -> None:
        if self._open:
            return
        if blocked:
            self._consecutive += 1
            self._total += 1
            if self._consecutive >= self.consecutive_limit or self._total >= self.total_limit:
                self._open = True
        else:
            self._consecutive = 0


class Classifier:
    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self.provider = provider
        self.model = model
        self.cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[ClassifierDecision, float]] = {}
        self.circuit_breaker = CircuitBreaker()

    def classify(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        permission_class: str,
        profile: str,
    ) -> ClassifierDecision | None:
        """Return a decision, or None if the circuit breaker is open (→ fall back to ask)."""
        if self.circuit_breaker.is_open:
            return None

        force_recheck = permission_class in DANGEROUS_CLASSES
        cache_key = _cache_key(tool_name, arguments, permission_class, profile)

        if not force_recheck:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return ClassifierDecision(
                    block=cached.block, reason=cached.reason, stage=cached.stage, cached=True
                )

        decision = self._stage1(tool_name, arguments, permission_class, profile)
        self._cache[cache_key] = (decision, time.monotonic())
        self.circuit_breaker.record(decision.block)
        return decision

    # --- internal ---

    def _stage1(
        self, tool_name: str, arguments: dict, permission_class: str, profile: str
    ) -> ClassifierDecision:
        args_summary = _summarize_arguments(arguments)
        prompt = (
            f"Is this tool call safe to auto-approve?\n"
            f"Reply ONLY with <block>yes</block> or <block>no</block>.\n\n"
            f"Tool: {tool_name}\n"
            f"Permission class: {permission_class}\n"
            f"Arguments: {args_summary}\n"
            f"Permission profile: {profile}"
        )
        text = self._call(prompt, max_chars=200, system="Reply only with <block>yes</block> or <block>no</block>.")
        should_block = _parse_block(text)
        if should_block:
            reason = self._stage2(tool_name, arguments, permission_class, profile)
            return ClassifierDecision(block=True, reason=reason, stage=2)
        return ClassifierDecision(block=False, reason="Auto-approved.", stage=1)

    def _stage2(
        self, tool_name: str, arguments: dict, permission_class: str, profile: str
    ) -> str:
        args_summary = _summarize_arguments(arguments)
        prompt = (
            f"Why should this tool call require human approval? 1-2 sentences.\n\n"
            f"Tool: {tool_name}\n"
            f"Permission class: {permission_class}\n"
            f"Arguments: {args_summary}\n"
            f"Permission profile: {profile}"
        )
        text = self._call(prompt, max_chars=600, system="You are a security classifier. Be concise.")
        return text.strip() or "Blocked by classifier."

    def _call(self, prompt: str, *, max_chars: int, system: str) -> str:
        from maurice.kernel.contracts import ProviderChunkType

        text = ""
        try:
            for chunk in self.provider.stream(
                messages=[{"role": "user", "content": prompt, "metadata": {}}],
                model=self.model,
                tools=[],
                system=system,
                limits={"max_tool_iterations": 1},
            ):
                if chunk.type == ProviderChunkType.TEXT_DELTA:
                    text += chunk.delta
                    if len(text) >= max_chars:
                        break
        except Exception:
            pass
        return text

    def _get_cached(self, key: str) -> ClassifierDecision | None:
        if key not in self._cache:
            return None
        decision, ts = self._cache[key]
        if time.monotonic() - ts > self.cache_ttl:
            del self._cache[key]
            return None
        return decision


def _cache_key(tool_name: str, arguments: dict, permission_class: str, profile: str) -> str:
    data = json.dumps(
        {"tool": tool_name, "args": arguments, "class": permission_class, "profile": profile},
        sort_keys=True,
    )
    return hashlib.sha256(data.encode()).hexdigest()


def _summarize_arguments(arguments: dict) -> str:
    summary = json.dumps(arguments, ensure_ascii=False)
    return summary[:200] + "..." if len(summary) > 200 else summary


def _parse_block(text: str) -> bool:
    match = re.search(r"<block>\s*(yes|no)\s*</block>", text, re.IGNORECASE)
    if not match:
        return False  # fail open — if we can't parse, don't block
    return match.group(1).lower() == "yes"
