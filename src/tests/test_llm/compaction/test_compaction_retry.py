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
)
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import estimate_context_tokens


from tests.test_llm.conftest import _NoopUiSink, _NoopEvents, _FakeUiPort

class TestCompactionRetry:
    """When compaction agent fails, it should retry before falling back."""

    def test_compaction_max_retries_constant(self):
        """COMPACTION_MAX_RETRIES should be defined and >= 1."""
        from voidx.llm.compaction import COMPACTION_MAX_RETRIES
        assert COMPACTION_MAX_RETRIES >= 1

    @pytest.mark.asyncio
    async def test_mixin_delegates_to_compaction_coordinator_with_overrides(self):
        from types import SimpleNamespace

        from voidx.agent.domain.compaction import CompactionResult
        from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution

        calls = []

        class FakeCoordinator:
            async def compact_for_live_state(
                self,
                messages,
                session_msgs,
                *,
                force,
                ask,
                preflight=False,
                run_compaction_agent,
                persist_compaction,
            ):
                calls.append((messages, session_msgs, force, ask, preflight))
                assert await run_compaction_agent(["head"], "previous") == "summary"
                await persist_compaction(["head"])
                return CompactionResult(
                    summary="summary",
                    live_messages=list(messages),
                    removed_messages=["head"],
                    tail_id="tail",
                )

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

        result = await LangGraphExecution._maybe_compact(
            host,
            ["message"],
            ["row"],
            force=True,
            ask=False,
        )

        assert result == (["head"], "tail")
        assert calls == [(["message"], ["row"], True, False, False)]
        assert persisted == ["head"]

    @pytest.mark.asyncio
    async def test_run_compaction_agent_uses_main_context_request_and_extracts_text(self, monkeypatch):
        """The real compaction path should summarize from the compiled main
        context plus a trailing structured request."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator as compaction_module
        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

        captured = {}

        async def fake_stream_llm(model, messages, renderer, protocol, **kwargs):
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

        result = await CompactionCoordinator(host).run_compaction_agent(
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

        import voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator as compaction_module
        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

        captured = {}
        estimate_results = iter([200_000, 200_000, 1_000])

        async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
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

        result = await CompactionCoordinator(host).run_compaction_agent(
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

        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

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

        result = await CompactionCoordinator(host).compact_for_live_state(
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

        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

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
        coordinator = CompactionCoordinator(host)
        coordinator.run_compaction_agent = fake_run_agent
        coordinator.persist_compaction = AsyncMock()
        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"User message {i}", id=str(i * 2 + 1)))
            messages.append(AIMessage(content=f"Assistant reply {i}"))

        with patch('voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator.estimate_context_tokens', return_value=200_000):
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
        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

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
        coordinator = CompactionCoordinator(host)
        coordinator.run_compaction_agent = fake_run_agent_always_fail
        coordinator.persist_compaction = AsyncMock()

        messages = []
        for i in range(8):
            messages.append(HumanMessage(content=f"Fix the auth bug {i}", id=str(i * 2 + 1)))
            messages.append(AIMessage(content=f"Looking at it {i}"))

        with patch('voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator.estimate_context_tokens', return_value=200_000):
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

        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

        class FakeEvents:
            def __init__(self):
                self.events = []

            async def emit(self, event):
                self.events.append(event)

        async def fake_run_agent_always_fail(_head_msgs, _prev_summary):
            raise RuntimeError("LLM always fails")

        fake_events = FakeEvents()
        import voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator as compaction_module

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
        coordinator = CompactionCoordinator(host)
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

    @pytest.mark.asyncio
    async def test_empty_compaction_result_uses_structured_log_without_warning(self, monkeypatch, caplog):
        """An empty compaction response must not leak a warning into the TUI."""
        import logging
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator as compaction_module
        from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator

        async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
            return AIMessage(content="")

        events = []

        def fake_log_tool_event(event, **kwargs):
            events.append((event, kwargs))

        monkeypatch.setattr(compaction_module, "stream_llm", fake_stream_llm)
        monkeypatch.setattr(compaction_module, "estimate_context_tokens", lambda *_args, **_kwargs: 10)
        monkeypatch.setattr(compaction_module, "estimate_message_tokens", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(compaction_module, "extract_token_usage", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(compaction_module, "log_tool_event", fake_log_tool_event)

        host = MagicMock()
        host.model = object()
        host._debug = False
        host._session = None
        host._ui = _FakeUiPort(via_events=False)
        host._compaction = CompactionService(context_limit=128_000, output_token_max=8_192)
        host._usage_stats = SimpleNamespace(
            update_context=lambda *_args, **_kwargs: None,
            record_call=lambda *_args, **_kwargs: None,
        )
        host.config = MagicMock()
        host.config.model.provider = "test-provider"
        host.config.model.model = "test-model"

        caplog.set_level(logging.WARNING, logger="voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator")

        result = await CompactionCoordinator(host).run_compaction_agent(
            [HumanMessage(content="old context")],
            None,
        )

        assert result is None
        assert events == [
            (
                "compaction_empty_result",
                {
                    "message": (
                        "Compaction agent returned empty text: "
                        "message_type=AIMessage content_type=str"
                    ),
                },
            )
        ]
        assert "Compaction agent returned empty text" not in caplog.text
