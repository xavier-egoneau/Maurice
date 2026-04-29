"""Tests for kernel/compaction.py."""

from __future__ import annotations

import pytest

from maurice.kernel.compaction import (
    CompactionConfig,
    CompactionLevel,
    compact_messages,
    estimate_tokens,
    needed_level,
    _group_by_turn,
)


def _msg(role: str, content: str, correlation_id: str | None = None) -> dict:
    return {
        "role": role,
        "content": content,
        "metadata": {"correlation_id": correlation_id} if correlation_id else {},
    }


def _turn(cid: str, user_text: str, assistant_text: str) -> list[dict]:
    return [
        _msg("user", user_text, cid),
        _msg("assistant", assistant_text, cid),
    ]


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens([]) == 0

    def test_counts_chars_divided_by_four(self):
        msgs = [_msg("user", "a" * 400)]
        assert estimate_tokens(msgs) == 100

    def test_includes_system(self):
        msgs = [_msg("user", "a" * 400)]
        total = estimate_tokens(msgs, system="s" * 400)
        assert total == 200

    def test_none_content(self):
        msgs = [{"role": "user", "content": None, "metadata": {}}]
        assert estimate_tokens(msgs) == 0


class TestNeededLevel:
    def make_config(self):
        return CompactionConfig(
            context_window_tokens=1000,
            trim_threshold=0.60,
            summarize_threshold=0.75,
            reset_threshold=0.90,
        )

    def test_none_below_threshold(self):
        assert needed_level(500, self.make_config()) == CompactionLevel.NONE

    def test_trim_at_threshold(self):
        assert needed_level(600, self.make_config()) == CompactionLevel.TRIM

    def test_summarize_at_threshold(self):
        assert needed_level(750, self.make_config()) == CompactionLevel.SUMMARIZE

    def test_reset_at_threshold(self):
        assert needed_level(900, self.make_config()) == CompactionLevel.RESET


class TestGroupByTurn:
    def test_single_turn(self):
        msgs = _turn("t1", "hello", "hi")
        groups = _group_by_turn(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_two_turns(self):
        msgs = _turn("t1", "hello", "hi") + _turn("t2", "bye", "ciao")
        groups = _group_by_turn(msgs)
        assert len(groups) == 2

    def test_no_correlation_id(self):
        msgs = [_msg("user", "a"), _msg("assistant", "b")]
        groups = _group_by_turn(msgs)
        assert len(groups) == 1

    def test_system_message_separate_group(self):
        msgs = [
            _msg("system", "context"),
            *_turn("t1", "hello", "hi"),
        ]
        groups = _group_by_turn(msgs)
        assert len(groups) == 2


class TestCompactMessages:
    def make_small_config(self):
        return CompactionConfig(
            context_window_tokens=100,
            trim_threshold=0.60,
            summarize_threshold=0.75,
            reset_threshold=0.90,
            keep_recent_turns=2,
        )

    def test_no_compaction_when_under_threshold(self):
        msgs = [_msg("user", "hi")]
        result, level = compact_messages(msgs, config=self.make_small_config())
        assert level == CompactionLevel.NONE
        assert result == msgs

    def test_trim_drops_old_turns(self):
        # 6 turns of 20 chars each = 120 chars = 30 tokens
        # config window=100, trim=0.60 → triggers at 60 tokens
        # make it bigger
        turns = []
        for i in range(8):
            turns.extend(_turn(f"t{i}", "a" * 80, "b" * 80))

        result, level = compact_messages(turns, config=self.make_small_config())
        assert level == CompactionLevel.TRIM
        # only keep_recent_turns=2 turns kept
        # 2 turns * 2 messages = 4 messages
        assert len(result) <= 4

    def test_trim_fallback_when_no_provider_and_summarize_needed(self):
        turns = []
        for i in range(8):
            turns.extend(_turn(f"t{i}", "a" * 100, "b" * 100))

        cfg = CompactionConfig(
            context_window_tokens=100,
            trim_threshold=0.60,
            summarize_threshold=0.65,
            reset_threshold=0.90,
            keep_recent_turns=2,
        )
        result, level = compact_messages(turns, config=cfg, provider=None)
        assert level == CompactionLevel.TRIM

    def test_summarize_uses_provider(self):
        from maurice.kernel.providers import MockProvider
        from maurice.kernel.contracts import ProviderChunk

        provider = MockProvider([
            ProviderChunk.model_validate({"type": "text_delta", "delta": "• Discussed X\n• Did Y"}),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])

        # 4 turns × 2 msgs × 100 chars / 4 = 200 tokens; window=1000
        # ratio=0.20 → but we set summarize low to trigger it
        turns = []
        for i in range(4):
            turns.extend(_turn(f"t{i}", "a" * 100, "b" * 100))

        cfg = CompactionConfig(
            context_window_tokens=1000,
            trim_threshold=0.05,
            summarize_threshold=0.10,
            reset_threshold=0.99,  # don't hit reset
            keep_recent_turns=2,
        )
        result, level = compact_messages(turns, config=cfg, provider=provider, model="mock")
        assert level == CompactionLevel.SUMMARIZE
        # should have a compacted summary system message
        compacted = [m for m in result if (m.get("metadata") or {}).get("compacted")]
        assert compacted
        assert "Session summary" in compacted[0]["content"]

    def test_reset_uses_provider_on_all_turns(self):
        from maurice.kernel.providers import MockProvider
        from maurice.kernel.contracts import ProviderChunk

        provider = MockProvider([
            ProviderChunk.model_validate({"type": "text_delta", "delta": "• Full reset summary"}),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])

        turns = []
        for i in range(8):
            turns.extend(_turn(f"t{i}", "a" * 200, "b" * 200))

        cfg = CompactionConfig(
            context_window_tokens=100,
            trim_threshold=0.50,
            summarize_threshold=0.60,
            reset_threshold=0.70,
            keep_recent_turns=2,
        )
        result, level = compact_messages(turns, config=cfg, provider=provider, model="mock")
        assert level == CompactionLevel.RESET
        # after reset, no non-system non-compacted messages from old turns
        non_summary = [m for m in result if m["role"] != "system"]
        assert len(non_summary) == 0

    def test_system_messages_preserved_in_trim(self):
        msgs = [
            _msg("system", "base prompt"),
            *_turn("t1", "a" * 100, "b" * 100),
            *_turn("t2", "a" * 100, "b" * 100),
            *_turn("t3", "a" * 100, "b" * 100),
        ]
        cfg = CompactionConfig(
            context_window_tokens=100,
            trim_threshold=0.50,
            summarize_threshold=0.95,
            reset_threshold=0.99,
            keep_recent_turns=1,
        )
        result, level = compact_messages(msgs, config=cfg)
        assert level == CompactionLevel.TRIM
        system_msgs = [m for m in result if m["role"] == "system"]
        assert any("base prompt" in m["content"] for m in system_msgs)


class TestLoopCompaction:
    """Integration: compaction triggers inside AgentLoop."""

    def test_loop_compacts_session_when_threshold_exceeded(self, tmp_path):
        from maurice.kernel.loop import AgentLoop
        from maurice.kernel.providers import MockProvider
        from maurice.kernel.contracts import ProviderChunk
        from maurice.kernel.session import SessionStore, SessionMessage
        from maurice.kernel.events import EventStore
        from maurice.kernel.permissions import PermissionContext
        from maurice.kernel.skills import SkillRegistry

        session_root = tmp_path / "sessions"
        event_path = tmp_path / "events.jsonl"

        session_store = SessionStore(session_root)
        event_store = EventStore(event_path)

        # pre-populate session with a lot of messages
        session = session_store.create("main", session_id="default")
        for i in range(6):
            session.messages.append(SessionMessage(
                role="user", content="a" * 800, metadata={"correlation_id": f"t{i}"}
            ))
            session.messages.append(SessionMessage(
                role="assistant", content="b" * 800, metadata={"correlation_id": f"t{i}"}
            ))
        session_store.save(session)

        provider = MockProvider([
            ProviderChunk.model_validate({"type": "text_delta", "delta": "ok"}),
            ProviderChunk.model_validate({"type": "status", "status": "completed"}),
        ])

        registry = SkillRegistry(skills={}, tools={})

        loop = AgentLoop(
            provider=provider,
            registry=registry,
            session_store=session_store,
            event_store=event_store,
            permission_context=PermissionContext(
                workspace_root=str(tmp_path),
                runtime_root=str(tmp_path),
            ),
            permission_profile="safe",
            model="mock",
            compaction_config=CompactionConfig(
                context_window_tokens=1000,
                trim_threshold=0.10,  # very low to always trigger
                summarize_threshold=0.95,
                reset_threshold=0.99,
                keep_recent_turns=2,
            ),
        )

        loop.run_turn(agent_id="main", session_id="default", message="hello")

        reloaded = session_store.load("main", "default")
        # compaction should have reduced the message count
        assert len(reloaded.messages) < 14

        events = [e.name for e in event_store.read_all()]
        assert "session.compacted" in events
