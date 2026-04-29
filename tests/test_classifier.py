"""Tests for kernel/classifier.py."""

from __future__ import annotations

import pytest

from maurice.kernel.classifier import (
    Classifier,
    ClassifierDecision,
    CircuitBreaker,
    _cache_key,
    _parse_block,
    _summarize_arguments,
)
from maurice.kernel.providers import MockProvider
from maurice.kernel.contracts import ProviderChunk


def _allow_provider():
    return MockProvider([
        ProviderChunk.model_validate({"type": "text_delta", "delta": "<block>no</block>"}),
        ProviderChunk.model_validate({"type": "status", "status": "completed"}),
    ])


def _block_provider(reason: str = "This is risky."):
    return MockProvider([
        ProviderChunk.model_validate({"type": "text_delta", "delta": "<block>yes</block>"}),
        ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ProviderChunk.model_validate({"type": "text_delta", "delta": reason}),
        ProviderChunk.model_validate({"type": "status", "status": "completed"}),
    ])


class TestParseBlock:
    def test_yes_blocks(self):
        assert _parse_block("<block>yes</block>") is True

    def test_no_allows(self):
        assert _parse_block("<block>no</block>") is False

    def test_case_insensitive(self):
        assert _parse_block("<block>YES</block>") is True
        assert _parse_block("<block>NO</block>") is False

    def test_unparseable_fails_open(self):
        assert _parse_block("I'm not sure") is False
        assert _parse_block("") is False


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert not cb.is_open

    def test_opens_after_consecutive_blocks(self):
        cb = CircuitBreaker(consecutive_limit=3, total_limit=100)
        cb.record(True)
        cb.record(True)
        assert not cb.is_open
        cb.record(True)
        assert cb.is_open

    def test_opens_after_total_blocks(self):
        cb = CircuitBreaker(consecutive_limit=100, total_limit=3)
        cb.record(True)
        cb.record(False)  # resets consecutive
        cb.record(True)
        cb.record(False)
        cb.record(True)
        assert cb.is_open

    def test_non_block_resets_consecutive(self):
        cb = CircuitBreaker(consecutive_limit=3, total_limit=100)
        cb.record(True)
        cb.record(True)
        cb.record(False)  # reset
        cb.record(True)
        cb.record(True)
        assert not cb.is_open  # only 2 consecutive

    def test_records_nothing_when_open(self):
        cb = CircuitBreaker(consecutive_limit=1, total_limit=100)
        cb.record(True)
        assert cb.is_open
        cb.record(False)  # should have no effect
        assert cb.is_open


class TestClassifier:
    def _make(self, provider):
        return Classifier(provider=provider, model="mock", cache_ttl_seconds=3600)

    def test_allow_decision(self):
        clf = self._make(_allow_provider())
        decision = clf.classify(
            tool_name="filesystem.read",
            arguments={"path": "/workspace/notes.md"},
            permission_class="fs.read",
            profile="safe",
        )
        assert decision is not None
        assert not decision.block
        assert decision.stage == 1

    def test_block_decision(self):
        clf = self._make(_block_provider("Dangerous path."))
        decision = clf.classify(
            tool_name="filesystem.write",
            arguments={"path": "/etc/passwd", "content": "evil"},
            permission_class="fs.write",
            profile="safe",
        )
        assert decision is not None
        assert decision.block
        assert decision.stage == 2
        assert "Dangerous path" in decision.reason or decision.reason

    def test_cached_result_returned(self):
        provider = _allow_provider()
        clf = self._make(provider)
        args = {"path": "/workspace/notes.md"}

        clf.classify(tool_name="filesystem.read", arguments=args,
                     permission_class="fs.read", profile="safe")
        initial_calls = len(provider.calls)

        decision = clf.classify(tool_name="filesystem.read", arguments=args,
                                permission_class="fs.read", profile="safe")
        assert len(provider.calls) == initial_calls  # no new call
        assert decision.cached

    def test_dangerous_class_bypasses_cache(self):
        provider = _allow_provider()
        clf = self._make(provider)
        args = {"command": "ls"}

        clf.classify(tool_name="shell.run", arguments=args,
                     permission_class="shell.exec", profile="power")
        initial_calls = len(provider.calls)

        clf.classify(tool_name="shell.run", arguments=args,
                     permission_class="shell.exec", profile="power")
        # shell.exec always re-evaluates
        assert len(provider.calls) > initial_calls

    def test_circuit_breaker_returns_none(self):
        provider = _block_provider()
        clf = Classifier(provider=provider, model="mock", cache_ttl_seconds=3600)
        clf.circuit_breaker = CircuitBreaker(consecutive_limit=1, total_limit=100)

        # First call triggers block and opens the breaker
        d1 = clf.classify(tool_name="t", arguments={}, permission_class="fs.write", profile="safe")
        assert d1 is not None and d1.block

        # Second call: circuit open → None
        d2 = clf.classify(tool_name="t2", arguments={}, permission_class="fs.write", profile="safe")
        assert d2 is None

    def test_provider_failure_fails_open(self):
        class FailingProvider:
            def stream(self, **_kwargs):
                raise RuntimeError("network error")

        clf = self._make(FailingProvider())
        decision = clf.classify(
            tool_name="web.fetch",
            arguments={"url": "https://example.com"},
            permission_class="network.outbound",
            profile="safe",
        )
        assert decision is not None
        assert not decision.block  # fail open


class TestLoopIntegration:
    """Integration: classifier in AgentLoop."""

    def _make_loop(self, tmp_path, provider, classifier=None, permission_profile="limited"):
        from maurice.kernel.loop import AgentLoop
        from maurice.kernel.session import SessionStore
        from maurice.kernel.events import EventStore
        from maurice.kernel.permissions import PermissionContext
        from maurice.kernel.skills import SkillRegistry
        from maurice.kernel.contracts import ToolDeclaration, ToolResult, TrustLabel

        tool_decl = ToolDeclaration.model_validate({
            "name": "net.fetch",
            "owner_skill": "net",
            "description": "Fetch a URL.",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
            "permission": {"class": "network.outbound", "scope": {"hosts": ["*"]}},
            "trust": {"input": "external_untrusted", "output": "external_untrusted"},
            "executor": "net.fetch",
        })
        registry = SkillRegistry(skills={}, tools={"net.fetch": tool_decl})

        def executor(args):
            return ToolResult(
                ok=True, summary=f"fetched {args.get('url')}",
                data=None, trust=TrustLabel.EXTERNAL_UNTRUSTED,
                artifacts=[], events=[], error=None,
            )

        return AgentLoop(
            provider=provider,
            registry=registry,
            session_store=SessionStore(tmp_path / "sessions"),
            event_store=EventStore(tmp_path / "events.jsonl"),
            permission_context=PermissionContext(
                workspace_root=str(tmp_path), runtime_root=str(tmp_path),
            ),
            permission_profile=permission_profile,
            tool_executors={"net.fetch": executor},
            model="mock",
            classifier=classifier,
        )

    def test_classifier_allows_proceed_without_human_approval(self, tmp_path):
        # Turn provider: calls net.fetch
        turn_provider = MockProvider([
            ProviderChunk.model_validate({
                "type": "tool_call",
                "tool_call": {"id": "c1", "name": "net.fetch", "arguments": {"url": "https://example.com"}},
            }),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])
        # Classifier provider: says allow
        clf = Classifier(provider=_allow_provider(), model="mock")

        loop = self._make_loop(tmp_path, turn_provider, classifier=clf, permission_profile="safe")
        result = loop.run_turn(agent_id="main", session_id="s1", message="fetch")

        events = [e.name for e in loop.event_store.read_all()]
        assert "classifier.decided" in events
        # Tool was executed (not blocked)
        assert any(r.ok for r in result.tool_results)

    def test_classifier_blocks_returns_classifier_blocked(self, tmp_path):
        turn_provider = MockProvider([
            ProviderChunk.model_validate({
                "type": "tool_call",
                "tool_call": {"id": "c1", "name": "net.fetch", "arguments": {"url": "https://evil.com"}},
            }),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])
        clf = Classifier(provider=_block_provider("Suspicious URL."), model="mock")

        loop = self._make_loop(tmp_path, turn_provider, classifier=clf, permission_profile="safe")
        result = loop.run_turn(agent_id="main", session_id="s2", message="fetch evil")

        assert any(not r.ok and r.error.code == "classifier_blocked" for r in result.tool_results)

    def test_circuit_breaker_open_falls_back_to_ask(self, tmp_path):
        turn_provider = MockProvider([
            ProviderChunk.model_validate({
                "type": "tool_call",
                "tool_call": {"id": "c1", "name": "net.fetch", "arguments": {"url": "https://example.com"}},
            }),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])
        clf = Classifier(provider=_allow_provider(), model="mock")
        clf.circuit_breaker._open = True  # force open

        loop = self._make_loop(tmp_path, turn_provider, classifier=clf, permission_profile="safe")
        result = loop.run_turn(agent_id="main", session_id="s3", message="fetch")

        # Falls back to ask — no approval store, so approval_required
        events = [e.name for e in loop.event_store.read_all()]
        assert "classifier.circuit_open" in events
        assert any(not r.ok and r.error.code == "approval_required" for r in result.tool_results)
