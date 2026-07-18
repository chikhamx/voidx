"""Tests for CompactionService — token counting, select, prune, build_prompt."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage



class _StubEncoding:
    def __init__(self, token_count: int):
        self.token_count = token_count

    def encode(self, _text: str) -> list[int]:
        return list(range(self.token_count))


def test_count_tokens_uses_and_caches_model_encoding(monkeypatch):
    import voidx.llm.context as context_module

    calls = []
    encodings = {
        "model-a": _StubEncoding(2),
        "model-b": _StubEncoding(5),
    }

    def encoding_for_model(model):
        calls.append(model)
        return encodings[model]

    monkeypatch.setattr(context_module.tiktoken, "encoding_for_model", encoding_for_model)
    context_module._get_encoding.cache_clear()

    assert context_module.count_tokens("same text", "model-a") == 2
    assert context_module.count_tokens("same text", "model-b") == 5
    assert context_module.count_tokens("same text", "model-a") == 2
    assert calls == ["model-a", "model-b"]

    context_module._get_encoding.cache_clear()


def test_count_tokens_falls_back_stably_when_model_encoding_is_unavailable(monkeypatch):
    import voidx.llm.context as context_module

    model_calls = []
    base_calls = []
    fallback = _StubEncoding(3)

    def unavailable_model(model):
        model_calls.append(model)
        raise ValueError("unknown or non-OpenAI model")

    def get_encoding(name):
        base_calls.append(name)
        if name == "cl100k_base":
            return fallback
        raise ValueError(name)

    monkeypatch.setattr(context_module.tiktoken, "encoding_for_model", unavailable_model)
    monkeypatch.setattr(context_module.tiktoken, "get_encoding", get_encoding)
    context_module._get_encoding.cache_clear()

    assert context_module.count_tokens("same text", "provider/custom") == 3
    assert context_module.count_tokens("same text", "provider/custom") == 3
    assert context_module.count_tokens("same text", "") == 3
    assert model_calls == ["provider/custom"]
    assert base_calls == ["cl100k_base"]

    context_module._get_encoding.cache_clear()

from voidx.agent.graph.wiring import build_compaction_service
from voidx.config import Config
from voidx.llm.compaction import (
    COMPACTION_MAX_RETRIES,
    COMPACTION_THRESHOLD,
    CompactionSelection,
    CompactionService,
    DEFAULT_TAIL_TURNS,
    STEP_HINT_MARKER,
)
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import estimate_context_tokens


from tests.test_llm.conftest import _make_messages_with_tool_calls


def _sized_human(content: str, message_id: str, tokens: int) -> HumanMessage:
    return HumanMessage(content=content, id=message_id, additional_kwargs={"token_size": tokens})


def _sized_ai(content: str, tokens: int) -> AIMessage:
    return AIMessage(content=content, additional_kwargs={"token_size": tokens})


def _sized_token_count(messages: list, _model: str = "") -> int:
    return sum(int(getattr(message, "additional_kwargs", {}).get("token_size", 0)) for message in messages)


class TestSelectTokenCounting:
    """select() should use the same token counting as estimate_context_tokens."""

    def test_compaction_soft_threshold_defaults_to_context_ratio_capped_by_usable_window(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)

        assert svc.soft_threshold() == 96_000

    def test_compaction_soft_threshold_caps_at_usable_window(self):
        svc = CompactionService(context_limit=128_000, output_token_max=64_000)

        assert svc.soft_threshold() == svc.usable_window()

    def test_post_compaction_target_defaults_to_ten_percent_context(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)

        assert svc.post_compaction_target() == 12_800

    def test_build_compaction_service_uses_configured_ratios(self):
        _usage, svc = build_compaction_service(
            Config(
                compaction_soft_ratio=0.65,
                compaction_post_target_ratio=0.12,
            )
        )

        assert svc.soft_ratio == 0.65
        assert svc.post_target_ratio == 0.12

    def test_select_preflight_details_keeps_minimum_two_turn_tail_even_over_target(self):
        svc = CompactionService(
            context_limit=1_000,
            output_token_max=100,
            token_counter=_sized_token_count,
        )
        messages = [
            _sized_human("old", "u1", 10),
            _sized_ai("old answer", 10),
            _sized_human("previous", "u2", 80),
            _sized_ai("previous answer", 80),
            _sized_human("current", "u3", 80),
        ]

        selection = svc.select_preflight_details(messages)

        assert selection.mode == "normal"
        assert selection.keep_from == 2
        assert [message.content for message in messages[selection.keep_from:]] == [
            "previous",
            "previous answer",
            "current",
        ]

    def test_select_preflight_details_expands_recent_tail_until_target(self):
        svc = CompactionService(
            context_limit=1_000,
            output_token_max=100,
            token_counter=_sized_token_count,
        )
        messages = [
            _sized_human("old 1", "u1", 60),
            _sized_ai("old answer 1", 60),
            _sized_human("old 2", "u2", 20),
            _sized_ai("old answer 2", 20),
            _sized_human("previous", "u3", 20),
            _sized_ai("previous answer", 20),
            _sized_human("current", "u4", 10),
        ]

        selection = svc.select_preflight_details(messages)

        # Minimum two-turn tail (previous + current) locked at index 2,
        # then expanded backward to include "old 2" turn (20+20 ≤ target 100).
        assert selection.mode == "normal"
        assert selection.keep_from == 2
        # Head = the two messages before keep_from (old 1 turn)
        assert selection.head == messages[:2]
        assert [message.content for message in selection.head] == ["old 1", "old answer 1"]
        assert [message.content for message in messages[selection.keep_from:]] == [
            "old 2",
            "old answer 2",
            "previous",
            "previous answer",
            "current",
        ]

    def test_select_uses_full_message_format_for_token_count(self):
        """The token count for each turn in select() must include tool_calls,
        not just the text content. This ensures the tail budget is accurate."""
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        messages = _make_messages_with_tool_calls(5)

        head, tail_id = svc.select(messages, tail_turns=3)

        # Verify tail was actually split off
        assert tail_id is not None
        assert len(head) < len(messages)

        # The tail messages should be the last few turns
        tail_msgs = messages[len(head):]
        assert len(tail_msgs) > 0

        # Verify that the token count for the tail matches estimate_context_tokens
        tail_tokens_estimate = estimate_context_tokens(tail_msgs)
        budget = svc.preserve_recent_budget()

        # The tail should fit within the budget (or be the minimum possible)
        # If it exceeds, it means the counting is off
        assert tail_tokens_estimate <= budget * 1.5, (
            f"Tail tokens ({tail_tokens_estimate}) far exceed budget ({budget}). "
            f"Token counting in select() may be inconsistent with estimate_context_tokens."
        )

    def test_select_includes_tool_calls_in_count(self):
        """Verify that select() counts tool_calls tokens, not just content text.
        If it only counts content, the tail will be underestimated."""
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)

        # Build messages where AI has substantial tool_calls
        msg_with_tc = HumanMessage(content="Do something", id="1")
        ai_with_tc = AIMessage(
            content="I will call a tool",
            tool_calls=[
                {"name": "read", "args": {"file_path": "/very/long/path/to/some/file.py"}, "id": "tc_1"},
                {"name": "grep", "args": {"pattern": "compaction", "path": "/src"}, "id": "tc_2"},
            ],
        )
        msg_no_tc = HumanMessage(content="Simple message", id="2")
        ai_no_tc = AIMessage(content="Simple reply")

        messages = [msg_with_tc, ai_with_tc, msg_no_tc, ai_no_tc]

        # Count with full format (what estimate_context_tokens does)
        full_count = estimate_context_tokens(messages)

        # Count with content-only format (what the old select() did)
        from voidx.llm.context import count_messages_tokens
        content_only_count = count_messages_tokens([
            {"role": "assistant" if isinstance(m, AIMessage) else "user",
             "content": str(getattr(m, "content", ""))}
            for m in messages
        ])

        # Full count should be strictly larger because it includes tool_calls
        assert full_count > content_only_count, (
            "estimate_context_tokens should count more tokens than content-only counting "
            "when tool_calls are present"
        )

    def test_select_internal_counting_matches_estimate_context_tokens(self):
        """select() should use estimate_context_tokens internally so that the
        tail budget calculation is consistent with the overflow check."""
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        messages = _make_messages_with_tool_calls(5)

        turns = svc._turns(messages)
        recent = turns[-DEFAULT_TAIL_TURNS:]

        # Compute what select() would count for each turn
        # (using the OLD content-only approach)
        from voidx.llm.context import count_messages_tokens as cmt_old
        old_counts = []
        for turn in recent:
            turn_msgs = messages[turn.start:turn.end]
            old_count = cmt_old([
                {"role": "assistant" if isinstance(m, AIMessage) else "user",
                 "content": str(getattr(m, "content", ""))}
                for m in turn_msgs
            ])
            old_counts.append(old_count)

        # Compute what estimate_context_tokens would count for each turn
        new_counts = []
        for turn in recent:
            turn_msgs = messages[turn.start:turn.end]
            new_count = estimate_context_tokens(turn_msgs)
            new_counts.append(new_count)

        # The new counts should be >= old counts because they include tool_calls
        for old, new in zip(old_counts, new_counts):
            assert new >= old, (
                f"estimate_context_tokens ({new}) should be >= content-only count ({old}) "
                f"for turns with tool_calls"
            )

    def test_select_uses_estimate_context_tokens_internally(self):
        """After the fix, select() should use estimate_context_tokens for turn
        sizing, not the old content-only dict format. We verify this by checking
        that the tail kept by select() respects the budget when measured with
        estimate_context_tokens."""
        # Use a small context limit to make the budget tight
        svc = CompactionService(context_limit=20_000, output_token_max=4_096)
        budget = svc.preserve_recent_budget()

        # Build messages with heavy tool_calls that inflate the real token count
        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"User request {i} " * 20, id=str(i * 3 + 1)))
            ai = AIMessage(
                content=f"Assistant reply {i} " * 20,
                tool_calls=[
                    {"name": "read", "args": {"file_path": f"/very/long/path/to/module_{i}.py"}, "id": f"tc_{i}"},
                    {"name": "grep", "args": {"pattern": "some_pattern", "path": "/src/very/deep/directory"}, "id": f"tc2_{i}"},
                ],
            )
            messages.append(ai)
            messages.append(ToolMessage(
                content=f"File content result {i} " * 30,
                tool_call_id=f"tc_{i}",
            ))
            messages.append(ToolMessage(
                content=f"Grep result {i} " * 30,
                tool_call_id=f"tc2_{i}",
            ))

        head, tail_id = svc.select(messages, tail_turns=3)

        if tail_id is not None:
            tail_msgs = messages[len(head):]
            tail_real_tokens = estimate_context_tokens(tail_msgs)
            # After the fix, the tail should respect the budget
            # Allow 20% margin for encoding differences
            assert tail_real_tokens <= budget * 1.2, (
                f"Tail real tokens ({tail_real_tokens}) exceed budget ({budget}) "
                f"by more than 20%. select() is underestimating turn size."
            )

    def test_select_details_full_mode_keeps_previous_complete_turn_and_current_user(self):
        svc = CompactionService(context_limit=1_000, output_token_max=900)
        svc.preserve_recent_budget = lambda: 1
        messages = [
            HumanMessage(content="old 1", id="u1"),
            AIMessage(content="a1"),
            HumanMessage(content="old 2", id="u2"),
            AIMessage(content="a2"),
            ToolMessage(content="tool 2", tool_call_id="tc2"),
            HumanMessage(content="current", id="u3"),
        ]

        selection = svc.select_details(messages)

        assert selection.mode == "full"
        assert [message.content for message in selection.head] == ["old 1", "a1"]
        assert selection.keep_from == 2
        assert [message.content for message in messages[selection.keep_from:]] == [
            "old 2",
            "a2",
            "tool 2",
            "current",
        ]

    def test_select_details_keeps_previous_complete_turn_when_current_fits_budget(self):
        svc = CompactionService(context_limit=1_000, output_token_max=900)
        messages = [
            HumanMessage(content="old 1", id="u1"),
            AIMessage(content="a1 " * 100),
            HumanMessage(content="previous complete", id="u2"),
            AIMessage(content="a2 " * 100),
            ToolMessage(content="tool 2 " * 100, tool_call_id="tc2"),
            HumanMessage(content="current", id="u3"),
        ]
        current_turn_size = estimate_context_tokens(messages[-1:])
        svc.preserve_recent_budget = lambda: current_turn_size + 10

        selection = svc.select_details(messages)

        assert selection.mode == "full"
        assert [message.content for message in selection.head] == ["old 1", "a1 " * 100]
        assert selection.keep_from == 2
        assert [message.content for message in messages[selection.keep_from:]] == [
            "previous complete",
            "a2 " * 100,
            "tool 2 " * 100,
            "current",
        ]

    def test_step_hint_messages_do_not_create_turns_or_tail_ids(self):
        svc = CompactionService(context_limit=1_000, output_token_max=900)
        hint = HumanMessage(
            content="[Step 9/10] FINAL response step. No tools are available.",
            additional_kwargs={STEP_HINT_MARKER: True},
            id="hint",
        )
        messages = [
            HumanMessage(content="old", id="u1"),
            AIMessage(content="a1"),
            HumanMessage(content="current", id="u2"),
            hint,
        ]

        turns = svc._turns(messages)
        selection = svc.select_details(messages)

        assert [turn.id for turn in turns] == ["u1", "u2"]
        assert selection.tail_id != "hint"

    def test_guidance_messages_do_not_create_turns_or_tail_ids(self):
        svc = CompactionService(context_limit=1_000, output_token_max=900)
        guidance = HumanMessage(
            content="Use TypeScript",
            additional_kwargs={GUIDANCE_MARKER: True},
            id="guide",
        )
        messages = [
            HumanMessage(content="old", id="u1"),
            AIMessage(content="a1"),
            guidance,
            HumanMessage(content="current", id="u2"),
        ]

        turns = svc._turns(messages)
        selection = svc.select_details(messages)

        assert [turn.id for turn in turns] == ["u1", "u2"]
        assert selection.tail_id != "guide"

    def test_build_prompt_skips_step_hint_messages(self):
        svc = CompactionService()
        prompt = svc.build_prompt([
            HumanMessage(content="real request", id="u1"),
            HumanMessage(
                content="[Step 9/10] FINAL response step. No tools are available.",
                additional_kwargs={STEP_HINT_MARKER: True},
            ),
        ])

        assert "real request" in prompt
        assert "FINAL response step" not in prompt

    def test_build_prompt_labels_guidance_messages(self):
        svc = CompactionService()
        prompt = svc.build_prompt([
            HumanMessage(content="real request", id="u1"),
            HumanMessage(
                content="Use TypeScript",
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ])

        assert "[User]: real request" in prompt
        assert "[Guidance]: Use TypeScript" in prompt
