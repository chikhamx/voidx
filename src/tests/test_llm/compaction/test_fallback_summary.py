"""Tests for CompactionService — token counting, select, prune, build_prompt."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


from voidx.llm.compaction import (
    COMPACTION_MAX_RETRIES,
    COMPACTION_THRESHOLD,
    CompactionSelection,
    CompactionService,
    DEFAULT_TAIL_TURNS,
    STEP_HINT_MARKER,
    SUMMARY_TEMPLATE,
)
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import estimate_context_tokens



class TestFallbackSummary:
    """When compaction agent fails, fallback should still produce a basic summary."""

    def test_fallback_generates_summary_from_human_messages(self):
        """fallback_summary should extract user message text to create a basic summary."""
        messages = [
            HumanMessage(content="Fix the auth bug in login.py", id="1"),
            AIMessage(content="I'll look at the auth module."),
            HumanMessage(content="Also check the session handler", id="2"),
            AIMessage(content="Checking session.py now."),
            HumanMessage(content="What about the token refresh?", id="3"),
        ]

        summary = CompactionService.fallback_summary(messages)

        assert summary is not None
        assert len(summary) > 0
        # Should contain key user intents
        assert "auth bug" in summary or "login.py" in summary
        assert "session handler" in summary or "session" in summary

    def test_fallback_summary_handles_empty_messages(self):
        messages = []
        summary = CompactionService.fallback_summary(messages)
        assert summary is not None  # Should return something, even if minimal

    def test_fallback_summary_handles_no_human_messages(self):
        messages = [AIMessage(content="Just AI talking")]
        summary = CompactionService.fallback_summary(messages)
        assert summary is not None

    def test_fallback_summary_labels_guidance_messages(self):
        messages = [
            HumanMessage(content="Fix the auth bug", id="1"),
            HumanMessage(
                content="Keep the patch small",
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ]

        summary = CompactionService.fallback_summary(messages)

        assert "User requested: Fix the auth bug" in summary
        assert "User requested: Guidance: Keep the patch small" in summary

    def test_fallback_summary_truncates_long_messages(self):
        messages = [
            HumanMessage(content="x" * 5000, id="1"),
            HumanMessage(content="y" * 5000, id="2"),
        ]
        summary = CompactionService.fallback_summary(messages)
        # Should be reasonably sized, not the full 10000 chars
        assert len(summary) < 5000

    def test_fallback_summary_preserves_ai_decisions_and_tool_results(self):
        messages = [
            HumanMessage(content="Fix src/voidx/llm/compaction.py", id="1"),
            AIMessage(
                content="Decision: keep previous complete turn before current request.",
                tool_calls=[
                    {"name": "read", "args": {"file_path": "src/voidx/llm/compaction.py"}, "id": "tc1"},
                ],
            ),
            ToolMessage(
                content="pytest failed: AssertionError in tests/test_compaction.py",
                tool_call_id="tc1",
            ),
        ]

        summary = CompactionService.fallback_summary(messages)

        assert "Decision: keep previous complete turn" in summary
        assert "Called tool read" in summary
        assert "pytest failed: AssertionError" in summary
        assert "src/voidx/llm/compaction.py" in summary

    def test_build_prompt_uses_char_budget_not_fixed_message_count(self):
        svc = CompactionService()
        messages = [
            HumanMessage(content=f"request {i}", id=str(i))
            for i in range(25)
        ]

        prompt = svc.build_prompt(messages)

        assert "request 0" in prompt
        assert "request 24" in prompt
        assert "## Conversation History" in prompt

    def test_summary_template_is_shared_by_prompt_contract(self):
        svc = CompactionService()

        prompt = svc.build_prompt([HumanMessage(content="Fix auth", id="u1")])

        assert SUMMARY_TEMPLATE in prompt
        assert "## Goal" in prompt
        assert "## Constraints & Preferences" in prompt
        assert "## Relevant Files" in prompt

    def test_fallback_summary_extracts_constraints_and_open_work(self):
        messages = [
            HumanMessage(
                content=(
                    "Fix src/voidx/llm/compaction.py, keep the patch small, "
                    "do not change the public API, and run targeted tests."
                ),
                id="u1",
            ),
            AIMessage(content="I updated the summary contract and still need to run pytest."),
        ]

        summary = CompactionService.fallback_summary(messages)

        constraints = summary.split("## Constraints & Preferences", 1)[1].split("## Progress", 1)[0]
        next_steps = summary.split("## Next Steps", 1)[1].split("## Critical Context", 1)[0]

        assert "keep the patch small" in constraints
        assert "do not change the public API" in constraints
        assert "run targeted tests" in next_steps

    def test_fallback_summary_does_not_match_test_substring_in_contesting(self):
        messages = [
            HumanMessage(content="I am contesting the result", id="u1"),
        ]

        summary = CompactionService.fallback_summary(messages)

        next_steps = summary.split("## Next Steps", 1)[1].split("## Critical Context", 1)[0]
        assert "- (none)" in next_steps

    def test_fallback_summary_no_markers_yields_empty_sections(self):
        messages = [
            HumanMessage(content="Hello world", id="u1"),
            AIMessage(content="Hi there"),
        ]

        summary = CompactionService.fallback_summary(messages)

        constraints = summary.split("## Constraints & Preferences", 1)[1].split("## Progress", 1)[0]
        next_steps = summary.split("## Next Steps", 1)[1].split("## Critical Context", 1)[0]
        assert "- (none)" in constraints
        assert "- (none)" in next_steps

    def test_fallback_summary_splits_clauses_without_space_after_period(self):
        messages = [
            HumanMessage(content="keep it small.Do not break API", id="u1"),
        ]

        summary = CompactionService.fallback_summary(messages)

        constraints = summary.split("## Constraints & Preferences", 1)[1].split("## Progress", 1)[0]
        assert "keep it small" in constraints
        assert "Do not break API" in constraints
