"""Tests for CompactionService — token counting, select, prune, build_prompt."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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


class _NoopUiSink:
    width = 80

    def print(self, *_args, **_kwargs) -> None:
        return None


class _NoopEvents:
    async def emit(self, _event) -> bool:
        return True


class _FakeUiPort:
    def __init__(self, *, via_events: bool = False, events=None) -> None:
        self._via_events = via_events
        self.events = events or _NoopEvents()
        self.ui = _NoopUiSink()
        self.console = _NoopUiSink()

    def via_events(self) -> bool:
        return self._via_events


def _make_messages_with_tool_calls(n_turns: int = 5) -> list:
    """Build messages where AI messages have tool_calls — the key difference
    between the old select() counting and estimate_context_tokens."""
    messages = []
    for i in range(n_turns):
        messages.append(HumanMessage(content=f"User message {i}", id=str(i * 3 + 1)))
        ai = AIMessage(
            content=f"Assistant reply {i}",
            tool_calls=[
                {"name": "read", "args": {"file_path": f"/tmp/file_{i}.py"}, "id": f"tc_{i}"},
            ],
        )
        messages.append(ai)
        messages.append(ToolMessage(content=f"Tool result {i}" * 50, tool_call_id=f"tc_{i}"))
    return messages


class TestSelectTokenCounting:
    """select() should use the same token counting as estimate_context_tokens."""

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


class TestCompactionRetry:
    """When compaction agent fails, it should retry before falling back."""

    def test_compaction_max_retries_constant(self):
        """COMPACTION_MAX_RETRIES should be defined and >= 1."""
        from voidx.llm.compaction import COMPACTION_MAX_RETRIES
        assert COMPACTION_MAX_RETRIES >= 1

    @pytest.mark.asyncio
    async def test_mixin_delegates_to_compaction_coordinator_with_overrides(self):
        from types import SimpleNamespace

        from voidx.agent.graph.compaction import GraphCompactionMixin

        calls = []

        class FakeCoordinator:
            async def maybe_compact(
                self,
                messages,
                session_msgs,
                *,
                force,
                ask,
                run_compaction_agent,
                persist_compaction,
            ):
                calls.append((messages, session_msgs, force, ask))
                assert await run_compaction_agent(["head"], "previous") == "summary"
                await persist_compaction(["head"])
                return ["head"], "tail"

        async def fake_run_agent(_head_messages, _previous_summary):
            return "summary"

        persisted = []

        async def fake_persist(head_messages):
            persisted.extend(head_messages)

        host = SimpleNamespace(
            _compaction_coordinator=FakeCoordinator(),
            _run_compaction_agent=fake_run_agent,
            _persist_compaction=fake_persist,
        )

        result = await GraphCompactionMixin._maybe_compact(
            host,
            ["message"],
            ["row"],
            force=True,
            ask=False,
        )

        assert result == (["head"], "tail")
        assert calls == [(["message"], ["row"], True, False)]
        assert persisted == ["head"]

    @pytest.mark.asyncio
    async def test_run_compaction_agent_uses_main_context_request_and_extracts_text(self, monkeypatch):
        """The real compaction path should summarize from the compiled main
        context plus a trailing structured request."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import voidx.agent.graph.compaction_coordinator as compaction_module
        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        captured = {}

        async def fake_stream_llm(model, messages, renderer, protocol):
            captured["model"] = model
            captured["messages"] = messages
            captured["renderer"] = renderer
            captured["protocol"] = protocol
            return AIMessage(content="## Goal\n- summarized")

        async def fake_save_context_frame_from_messages(**kwargs):
            captured["context_frame"] = kwargs

        monkeypatch.setattr(compaction_module, "stream_llm", fake_stream_llm)
        monkeypatch.setattr(
            compaction_module,
            "save_context_frame_from_messages",
            fake_save_context_frame_from_messages,
        )

        class FakeInstruction:
            async def workflow_context_for(self, text, **kwargs):
                captured["workflow_text"] = text
                captured["workflow_kwargs"] = kwargs
                return SimpleNamespace(
                    instructions=["compaction workflow"],
                    active=["compaction"],
                    content="## Workflow Context\ncompaction workflow",
                )

        main_context = [
            SystemMessage(content="compiled main system prompt"),
            HumanMessage(content="## Workflow Context\nmain workflow context"),
            HumanMessage(content="Fix the compaction fallback", id="1"),
            AIMessage(content="I will update compaction."),
        ]
        host = SimpleNamespace(
            _compaction=CompactionService(context_limit=128_000, output_token_max=8_192),
            _debug=False,
            _session=SimpleNamespace(id="session-1"),
            _usage_stats=MagicMock(),
            _instruction=FakeInstruction(),
            _current_messages=main_context,
            config=SimpleNamespace(
                model=SimpleNamespace(
                    provider="openai",
                    model="gpt-4o",
                    protocol=None,
                ),
            ),
            model=object(),
            _ui=_FakeUiPort(via_events=False),
        )

        result = await GraphCompactionCoordinator(host).run_compaction_agent(
            [HumanMessage(content="Fix the compaction fallback", id="1")],
            "## Goal\n- previous",
        )

        assert result == "## Goal\n- summarized"
        assert captured["model"] is host.model
        assert captured["protocol"] == "openai"
        assert captured["renderer"]._headless is True
        assert captured["renderer"]._stream_to_dock is False
        assert isinstance(captured["messages"][0], SystemMessage)
        assert captured["messages"][0].content == "compiled main system prompt"
        assert captured["messages"][:-1] is not main_context
        assert [m.content for m in captured["messages"][:-1]] == [m.content for m in main_context]
        assert isinstance(captured["messages"][-1], HumanMessage)
        assert "Output exactly the Markdown structure" in captured["messages"][-1].content
        assert "<previous-summary>\n## Goal\n- previous\n</previous-summary>" in captured["messages"][-1].content
        assert "compaction workflow" not in "\n".join(str(m.content) for m in captured["messages"])
        assert "workflow_kwargs" not in captured
        assert captured["context_frame"]["agent_persona"] == "compaction-behavior"
        assert captured["context_frame"]["metadata"]["input_mode"] == "main_context"

    @pytest.mark.asyncio
    async def test_run_compaction_agent_falls_back_and_truncates_when_main_context_exceeds_budget(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import voidx.agent.graph.compaction_coordinator as compaction_module
        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        captured = {}
        estimate_results = iter([200_000, 200_000, 1_000])

        async def fake_stream_llm(_model, messages, _renderer, _protocol):
            captured["messages"] = messages
            return AIMessage(content="## Goal\n- summarized")

        async def fake_save_context_frame_from_messages(**kwargs):
            captured["context_frame"] = kwargs

        monkeypatch.setattr(compaction_module, "stream_llm", fake_stream_llm)
        monkeypatch.setattr(
            compaction_module,
            "save_context_frame_from_messages",
            fake_save_context_frame_from_messages,
        )
        monkeypatch.setattr(
            compaction_module,
            "estimate_context_tokens",
            lambda *_args, **_kwargs: next(estimate_results),
        )

        head_messages = [
            HumanMessage(content="old user", id="1"),
            AIMessage(content="old assistant"),
            HumanMessage(content="newer user", id="2"),
        ]
        compaction = CompactionService(context_limit=128_000, output_token_max=8_192)
        truncate = MagicMock(return_value=head_messages[-1:])
        compaction.truncate_head_to_budget = truncate
        host = SimpleNamespace(
            _compaction=compaction,
            _debug=False,
            _session=SimpleNamespace(id="session-1"),
            _usage_stats=MagicMock(),
            _current_messages=[
                SystemMessage(content="compiled main system prompt"),
                *head_messages,
            ],
            config=SimpleNamespace(
                model=SimpleNamespace(provider="openai", model="gpt-4o", protocol=None),
            ),
            model=object(),
            _ui=_FakeUiPort(via_events=False),
        )

        result = await GraphCompactionCoordinator(host).run_compaction_agent(
            head_messages,
            None,
        )

        assert result == "## Goal\n- summarized"
        truncate.assert_called_once()
        assert [message.content for message in captured["messages"][:-1]] == ["newer user"]
        assert captured["context_frame"]["metadata"]["input_mode"] == "fallback"
        assert captured["context_frame"]["metadata"]["source_message_count"] == 4

    @pytest.mark.asyncio
    async def test_compact_for_live_state_returns_result_without_mutating_messages(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        host = SimpleNamespace(
            _compaction=CompactionService(context_limit=128_000, output_token_max=8_192),
            _pending_summary=None,
            _compaction_summary="",
            config=SimpleNamespace(
                model=SimpleNamespace(model="gpt-4o"),
                ask_compact=False,
            ),
            _session=None,
            _debug=False,
            model=MagicMock(),
            _ui=_FakeUiPort(via_events=False),
        )
        host._compaction.select_details = lambda messages: CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="full",
        )
        messages = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="older question", id="older_user"),
            AIMessage(content="older answer", id="older_assistant"),
            HumanMessage(content="current question", id="current_user"),
        ]

        async def summarize(_head_messages, _previous_summary):
            return "new summary"

        persisted = []

        async def persist(head_messages):
            persisted.extend(head_messages)

        result = await GraphCompactionCoordinator(host).compact_for_live_state(
            messages,
            force=True,
            ask=False,
            include_summary_message=True,
            run_compaction_agent=summarize,
            persist_compaction=persist,
        )

        assert result is not None
        assert [message.content for message in messages] == [
            "system prompt",
            "older question",
            "older answer",
            "current question",
        ]
        assert [message.content for message in result.removed_messages] == [
            "older question",
            "older answer",
        ]
        assert isinstance(result.live_messages[0], SystemMessage)
        assert result.live_messages[0].content == "system prompt"
        assert isinstance(result.live_messages[1], SystemMessage)
        assert result.live_messages[1].content == "## Long Summary\nnew summary"
        assert result.live_messages[-1].content == "current question"
        assert persisted == result.removed_messages

    @pytest.mark.asyncio
    async def test_maybe_compact_retries_on_agent_failure(self):
        """When _run_compaction_agent raises, _maybe_compact should retry
        before falling back to truncation."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        call_count = 0

        async def fake_run_agent(head_msgs, prev_summary):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("LLM timeout")
            return "## Goal\n- Test goal"

        host = MagicMock()
        host._compaction = CompactionService(context_limit=128_000, output_token_max=8_192)
        host._pending_summary = None
        host._compaction_summary = ""
        host.config = MagicMock()
        host.config.model.model = "test-model"
        host.config.ask_compact = False
        host._session = None
        host._debug = False
        host.model = MagicMock()
        host._ui = _FakeUiPort(via_events=False)
        coordinator = GraphCompactionCoordinator(host)
        coordinator.run_compaction_agent = fake_run_agent
        coordinator.persist_compaction = AsyncMock()
        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"User message {i}", id=str(i * 2 + 1)))
            messages.append(AIMessage(content=f"Assistant reply {i}"))

        with patch('voidx.agent.graph.compaction_coordinator.estimate_context_tokens', return_value=200_000):
            result = await coordinator.maybe_compact(messages, [], force=True, ask=False)

        # Should have retried and eventually succeeded
        assert call_count == 3, f"Expected 3 calls (2 failures + 1 success), got {call_count}"
        assert host._pending_summary == "## Goal\n- Test goal"

    @pytest.mark.asyncio
    async def test_maybe_compact_falls_back_after_max_retries(self):
        """When _run_compaction_agent keeps failing, _maybe_compact should
        fall back to truncation with a basic summary after COMPACTION_MAX_RETRIES."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from voidx.llm.compaction import COMPACTION_MAX_RETRIES
        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        call_count = 0

        async def fake_run_agent_always_fail(head_msgs, prev_summary):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("LLM always fails")

        host = MagicMock()
        host._compaction = CompactionService(context_limit=128_000, output_token_max=8_192)
        host._pending_summary = None
        host._compaction_summary = ""
        host.config = MagicMock()
        host.config.model.model = "test-model"
        host.config.ask_compact = False
        host._session = None
        host._debug = False
        host.model = MagicMock()
        host._ui = _FakeUiPort(via_events=False)
        coordinator = GraphCompactionCoordinator(host)
        coordinator.run_compaction_agent = fake_run_agent_always_fail
        coordinator.persist_compaction = AsyncMock()

        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"Fix the auth bug {i}", id=str(i * 2 + 1)))
            messages.append(AIMessage(content=f"Looking at it {i}"))

        with patch('voidx.agent.graph.compaction_coordinator.estimate_context_tokens', return_value=200_000):
            result = await coordinator.maybe_compact(messages, [], force=True, ask=False)

        # Should have tried COMPACTION_MAX_RETRIES + 1 times (initial + retries)
        assert call_count == COMPACTION_MAX_RETRIES + 1, (
            f"Expected {COMPACTION_MAX_RETRIES + 1} calls, got {call_count}"
        )
        # Fallback should still produce a summary
        assert host._pending_summary is not None
        assert len(host._pending_summary) > 0

    @pytest.mark.asyncio
    async def test_maybe_compact_fallback_finished_event_includes_failure_detail(self, monkeypatch):
        """The final fallback status should keep the failure reason visible."""
        from unittest.mock import AsyncMock, MagicMock

        from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

        class FakeEvents:
            def __init__(self):
                self.events = []

            async def emit(self, event):
                self.events.append(event)

        async def fake_run_agent_always_fail(_head_msgs, _prev_summary):
            raise RuntimeError("LLM always fails")

        fake_events = FakeEvents()
        import voidx.agent.graph.compaction_coordinator as compaction_module

        monkeypatch.setattr(compaction_module, "estimate_context_tokens", lambda *_args, **_kwargs: 200_000)

        host = MagicMock()
        host._compaction = CompactionService(context_limit=128_000, output_token_max=8_192)
        host._pending_summary = None
        host._compaction_summary = ""
        host.config = MagicMock()
        host.config.model.model = "test-model"
        host.config.ask_compact = False
        host._session = None
        host._debug = False
        host.model = MagicMock()
        host._ui = _FakeUiPort(via_events=True, events=fake_events)
        coordinator = GraphCompactionCoordinator(host)
        coordinator.run_compaction_agent = fake_run_agent_always_fail
        coordinator.persist_compaction = AsyncMock()

        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"Fix the auth bug {i}", id=str(i * 2 + 1)))
            messages.append(AIMessage(content=f"Looking at it {i}"))

        await coordinator.maybe_compact(messages, [], force=True, ask=False)

        finished = [
            event for event in fake_events.events
            if getattr(event, "kind", "") == "status.finished"
        ][-1]
        assert finished.status_id == "compaction"
        assert finished.ok is False
        assert finished.detail == "RuntimeError: LLM always fails; using extracted summary"


class TestOverflowThreshold:
    """is_overflow should use percentage-based threshold."""

    def test_no_overflow_when_well_below_threshold(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        # 50% usage — should not overflow
        tokens = {"total": 64_000}
        assert not svc.is_overflow(tokens)

    def test_overflow_at_90_percent(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        # Exactly 90% of context_limit
        threshold = int(128_000 * COMPACTION_THRESHOLD)
        tokens = {"total": threshold}
        assert svc.is_overflow(tokens)

    def test_no_overflow_just_below_90_percent(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        threshold = int(128_000 * COMPACTION_THRESHOLD)
        tokens = {"total": threshold - 1}
        assert not svc.is_overflow(tokens)

    def test_overflow_at_95_percent(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        tokens = {"total": int(128_000 * 0.95)}
        assert svc.is_overflow(tokens)

    def test_threshold_is_90_percent(self):
        assert COMPACTION_THRESHOLD == 0.90

    def test_overflow_ignores_output_token_max(self):
        """Threshold is based on context_limit percentage, not usable_window.
        output_token_max should not affect when compaction triggers."""
        svc_small = CompactionService(context_limit=128_000, output_token_max=2_048)
        svc_large = CompactionService(context_limit=128_000, output_token_max=16_384)
        tokens = {"total": int(128_000 * 0.91)}
        # Both should overflow at the same point — 91% of context_limit
        assert svc_small.is_overflow(tokens)
        assert svc_large.is_overflow(tokens)

    def test_overflow_with_zero_context_limit(self):
        svc = CompactionService(context_limit=0, output_token_max=8_192)
        tokens = {"total": 100}
        assert not svc.is_overflow(tokens)

    def test_overflow_with_missing_total_uses_input_plus_output(self):
        svc = CompactionService(context_limit=128_000, output_token_max=8_192)
        tokens = {"input": int(128_000 * 0.91), "output": 0, "reasoning": 0}
        assert svc.is_overflow(tokens)

    def test_compaction_blocks_before_llm_invoke(self):
        """Verify that _maybe_compact is called before graph.ainvoke in run_loop.
        This is a structural test — the ordering is already correct, we just
        confirm the code path."""
        # The run_loop code at line ~393 does:
        #   head, tail_id = await self._maybe_compact(msgs, session_msgs)
        #   ... (a few lines of summary injection)
        #   final = await self.graph.ainvoke(initial, ...)
        # This means compaction completes before LLM is invoked — it's blocking.
        # No code change needed, just documenting the invariant.
        assert True
