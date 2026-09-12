from __future__ import annotations

import asyncio
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.agent.adapters.langgraph.runtime.prepared_request import (
    PreparedMainRequest,
    prepare_main_request,
)
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.turn_input import ActiveTurnInput
from voidx.agent.adapters.langgraph.runtime.turn_journal import TurnJournal
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    bind_thread_execution_context,
    current_thread_execution_state,
    save_turn_message,
)
from voidx.llm.message_markers import (
    COMPACTION_MESSAGE_MARKER,
    CONTINUATION_MESSAGE_MARKER,
    is_compaction_message,
    is_continuation_message,
)
from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
    FakeStreamingModel,
    FakeRenderer,
)


def test_runtime_context_builder_does_not_compile_long_summary():
    builder = RuntimeContextBuilder(
        config=Config(workspace="/test"),
        workspace="/test",
        persona="coding",
        interaction_mode="auto",
        summary="## Obsolete Long Summary Content",
    )
    context = builder.build()
    section_names = [s.name for s in context.sections]
    assert "Long Summary" not in section_names
    for s in context.sections:
        assert "Obsolete Long Summary Content" not in s.content


def test_prepare_main_request_calculates_budget_and_over_budget():
    messages = [HumanMessage(content="hello" * 100)]
    tools = []
    prepared = prepare_main_request(
        messages=messages,
        tool_defs=tools,
        model_name="test-model",
        context_limit=1000,
        output_token_max=200,
        safety_margin=100,
        token_counter=lambda msgs, model="": 750,
    )
    assert isinstance(prepared, PreparedMainRequest)
    assert prepared.total_input_tokens == 750
    assert prepared.main_request_limit == 700
    assert prepared.should_rollover is True



def test_prepare_main_request_counts_provider_view_even_with_legacy_budget_argument():
    provider_messages = [
        HumanMessage(content="runtime system overlay"),
        HumanMessage(content="summary"),
        HumanMessage(content="continue"),
    ]
    budget_messages = [HumanMessage(content="summary")]
    counted: list[list[BaseMessage]] = []

    prepared = prepare_main_request(
        messages=provider_messages,
        budget_messages=budget_messages,
        tool_defs=[],
        model_name="test-model",
        context_limit=1000,
        output_token_max=200,
        safety_margin=100,
        token_counter=lambda msgs, model="": counted.append(list(msgs)) or len(msgs) * 100,
    )

    assert counted == [provider_messages]
    assert prepared.total_input_tokens == 300
    assert prepared.messages == provider_messages
    assert prepared.budget_messages == provider_messages
    assert prepared.should_rollover is False



@pytest.mark.asyncio
async def test_rollover_for_live_state_two_consecutive_cycles(tmp_path, monkeypatch):
    session = await create_session(workspace=str(tmp_path))
    sid = session.id
    try:
        cfg = Config(
            model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet"),
            workspace=str(tmp_path),
        )
        graph = make_langgraph_execution(cfg, api_key="fake-key", session=session)

        # 1. Initial user prompt saved to session
        user_row_id = await save_message(MessageRow(
            session_id=sid,
            role="user",
            content="Task: build feature",
        ))

        # TurnJournal tracks unpersisted assistant and tool messages
        inp = ActiveTurnInput(
            turn_journal_id="tj_1",
            user_message_id=user_row_id,
            raw_text="Task: build feature",
            semantic_text="Task: build feature",
            display_text="Task: build feature",
            title_text="Task: build feature",
            content="Task: build feature",
        )
        journal = TurnJournal(active_input=inp)

        # Mock the summary agent invocation to return deterministic summary
        summary_calls = []

        async def mock_run_compaction_agent(summary_head, previous_summary=None, *args, **kwargs):
            summary_calls.append((list(summary_head), previous_summary))
            idx = len(summary_calls)
            return f"## Goal\n- ongoing task v{idx}\n\n## Progress\n### Done\n- step {idx} done"

        coordinator = graph._compaction_coordinator
        monkeypatch.setattr(coordinator, "run_compaction_agent", mock_run_compaction_agent)

        user_msg = HumanMessage(id=str(user_row_id), content="Task: build feature")
        ai_1 = AIMessage(
            content="calling tool 1",
            tool_calls=[{"id": "call_1", "name": "tool1", "args": {}}],
        )
        tool_1 = ToolMessage(content="tool 1 output", tool_call_id="call_1")

        messages_cycle_1 = [user_msg, ai_1, tool_1]

        # Bind thread context with turn journal
        async with bind_thread_execution_context(
            graph,
            session_id=sid,
        ) as exec_state:
            exec_state.turn_journal = journal
            exec_state.active_turn_input = inp

            prepared_1 = prepare_main_request(
                messages=messages_cycle_1,
                tool_defs=[],
                model_name="claude-3-5-sonnet",
                context_limit=10000,
                output_token_max=100,
                safety_margin=50,
                token_counter=lambda msgs, model="": sum(3000 if isinstance(m, ToolMessage) else 100 for m in msgs),
            )

            # Rollover 1
            result_1 = await coordinator.rollover_for_live_state(
                messages_cycle_1,
                prepared_request=prepared_1,
            )

            assert result_1 is not None
            assert len(summary_calls) == 1
            # Previous summary was None because it's first rollover
            assert summary_calls[0][1] is None

            # Verify live messages structure: RemoveMessage + summary + tail + continuation
            assert isinstance(result_1.live_messages[0], RemoveMessage)
            assert result_1.live_messages[0].id == REMOVE_ALL_MESSAGES
            summary_msg_1 = result_1.live_messages[1]
            assert is_compaction_message(summary_msg_1)
            assert summary_msg_1.additional_kwargs["compaction_depth"] == 1
            assert is_continuation_message(result_1.live_messages[-1])

            # Verify persisted messages in session after Rollover 1
            db_msgs_1 = await load_messages(sid)
            # Only ONE synthetic summary row in DB!
            summary_rows_1 = [m for m in db_msgs_1 if is_compaction_message(m)]
            assert len(summary_rows_1) == 1
            # Continuation message is NOT persisted to DB
            assert not any(is_continuation_message(m) for m in db_msgs_1)

            # ── Cycle 2: second rollover in the same turn ─────────────────
            ai_2 = AIMessage(
                content="calling tool 2",
                tool_calls=[{"id": "call_2", "name": "tool2", "args": {}}],
            )
            tool_2 = ToolMessage(content="tool 2 output", tool_call_id="call_2")

            # In the graph, live messages are: summary + tail + continuation + ai_2 + tool_2
            messages_cycle_2 = [
                summary_msg_1,
                *[m for m in result_1.live_messages[2:-1]], # tail if any
                result_1.live_messages[-1], # continuation
                ai_2,
                tool_2,
            ]

            prepared_2 = prepare_main_request(
                messages=messages_cycle_2,
                tool_defs=[],
                model_name="claude-3-5-sonnet",
                context_limit=10000,
                output_token_max=100,
                safety_margin=50,
                token_counter=lambda msgs, model="": sum(3000 if isinstance(m, ToolMessage) else 100 for m in msgs),
            )

            # Rollover 2
            result_2 = await coordinator.rollover_for_live_state(
                messages_cycle_2,
                prepared_request=prepared_2,
            )

            assert result_2 is not None
            assert len(summary_calls) == 2
            summary_msg_2 = result_2.live_messages[1]
            assert is_compaction_message(summary_msg_2)
            # Depth incremented to 2!
            assert summary_msg_2.additional_kwargs["compaction_depth"] == 2

            # Verify persisted messages in session after Rollover 2:
            # Recursively replaced! Still only ONE synthetic summary row in DB!
            db_msgs_2 = await load_messages(sid)
            summary_rows_2 = [m for m in db_msgs_2 if is_compaction_message(m)]
            assert len(summary_rows_2) == 1
            assert summary_rows_2[0].additional_kwargs["compaction_depth"] == 2
            assert not any(is_continuation_message(m) for m in db_msgs_2)
    finally:
        await delete_session(sid)



@pytest.mark.asyncio
async def test_call_llm_triggers_automatic_rollover_when_over_budget(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    session = await create_session(workspace=str(tmp_path))
    sid = session.id
    try:
        cfg = Config(
            model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet"),
            workspace=str(tmp_path),
        )
        graph = make_langgraph_execution(cfg, api_key="fake", session=session)
        monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

        user_row_id = await save_message(MessageRow(
            session_id=sid,
            role="user",
            content="Task: build big feature",
        ))

        inp = ActiveTurnInput(
            turn_journal_id="tj_1",
            user_message_id=user_row_id,
            raw_text="Task: build big feature",
            semantic_text="Task: build big feature",
            display_text="Task: build big feature",
            title_text="Task: build big feature",
            content="Task: build big feature",
        )
        journal = TurnJournal(active_input=inp)

        # Mock the summary agent invocation to return deterministic summary
        summary_calls = []

        async def mock_run_compaction_agent(summary_head, previous_summary=None, *args, **kwargs):
            summary_calls.append(list(summary_head))
            return "## Goal\n- ongoing big feature\n\n## Progress\n### Done\n- summarized"

        coordinator = graph._compaction_coordinator
        monkeypatch.setattr(coordinator, "run_compaction_agent", mock_run_compaction_agent)

        class AnsweringModel(FakeStreamingModel):
            def __init__(self) -> None:
                super().__init__()
                self.messages_by_call = []

            async def astream(self, messages, *args, **kwargs):
                self.messages_by_call.append(list(messages))
                yield AIMessageChunk(content="Finished answering.")

        graph.model = AnsweringModel()

        user_msg = HumanMessage(id=str(user_row_id), content="Task: build big feature")
        ai_1 = AIMessage(
            content="calling tool 1",
            tool_calls=[{"id": "call_1", "name": "tool1", "args": {}}],
        )
        tool_1 = ToolMessage(content="tool 1 output", tool_call_id="call_1")

        state = {
            "messages": [user_msg, ai_1, tool_1],
            "step_count": 0,
            "persona": "coordinate",
        }

        candidate_builds = []
        original_rollover = coordinator.rollover_for_live_state

        async def checked_rollover(*args, **kwargs):
            assert callable(kwargs.get("prepare_candidate"))
            original_prepare = kwargs["prepare_candidate"]
            def checked_prepare(messages):
                candidate = original_prepare(messages)
                candidate_builds.append(candidate)
                return candidate
            kwargs["prepare_candidate"] = checked_prepare
            return await original_rollover(*args, **kwargs)

        monkeypatch.setattr(coordinator, "rollover_for_live_state", checked_rollover)
        # Force token counter or context limit so that prepare_main_request indicates rollover
        # E.g. context limit = 500, output_reserve = 100, safety = 50 -> limit = 350
        # messages has 3 msgs which exceed 350 tokens when using token_counter
        monkeypatch.setattr(
            graph_module,
            "prepare_main_request",
            lambda msgs, tool_defs, **kw: prepare_main_request(
                msgs,
                tool_defs,
                model_name="claude-3-5-sonnet",
                context_limit=100000,
                output_token_max=100,
                safety_margin=50,
                token_counter=lambda m, mdl="": sum(100000 if isinstance(x, ToolMessage) else 100 for x in m),
                budget_messages=kw.get("budget_messages"),
            ),
        )

        async with bind_thread_execution_context(
            graph,
            session_id=sid,
        ) as exec_state:
            exec_state.turn_journal = journal
            exec_state.active_turn_input = inp

            result = await graph._call_llm(state)

            assert len(summary_calls) == 1
            assert len(candidate_builds) == 1
            assert any(isinstance(m, SystemMessage) for m in candidate_builds[0].messages)
            assert any(isinstance(m, SystemMessage) for m in graph.model.messages_by_call[0])
            # Messages returned to LangGraph have replacement structure
            assert isinstance(result["messages"][0], RemoveMessage)
            assert result["messages"][0].id == REMOVE_ALL_MESSAGES
            assert any(is_compaction_message(m) for m in result["messages"])
            assert result["messages"][-1].content == "Finished answering."
            assert len(graph.model.messages_by_call) == 1
            assert sum(
                is_continuation_message(m) for m in graph.model.messages_by_call[0]
            ) == 1
            assert not any(is_continuation_message(m) for m in result["messages"])
    finally:
        await delete_session(sid)



def test_prepare_main_request_hash_includes_full_message_content():
    common_prefix = "shared context " * 100
    first = prepare_main_request(
        [HumanMessage(content=common_prefix + "tail-a")],
        [],
        model_name="test-model",
        context_limit=10_000,
        output_token_max=100,
        safety_margin=100,
        token_counter=lambda msgs, model="": 10,
    )
    second = prepare_main_request(
        [HumanMessage(content=common_prefix + "tail-b")],
        [],
        model_name="test-model",
        context_limit=10_000,
        output_token_max=100,
        safety_margin=100,
        token_counter=lambda msgs, model="": 10,
    )

    assert first.request_hash != second.request_hash


@pytest.mark.asyncio
async def test_provider_overflow_without_reclaim_raises_context_budget_exhausted(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    import voidx.agent.adapters.langgraph.runtime.core.loop as loop_module
    from voidx.agent.domain.compaction import ContextBudgetExhausted

    class AlwaysOverflowModel(FakeStreamingModel):
        async def astream(self, messages, *args, **kwargs):
            if False:
                yield AIMessageChunk(content="")
            error = RuntimeError("context length exceeded")
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    session = await create_session(workspace=str(tmp_path))
    sid = session.id
    try:
        monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
        monkeypatch.setattr(loop_module, "_llm_retry_sleep_delay", lambda _delay: 0)
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet"),
                workspace=str(tmp_path),
            ),
            api_key="fake-key",
            session=session,
        )
        graph.model = AlwaysOverflowModel()
        rollover_calls = 0

        async def no_reclaim(*args, **kwargs):
            nonlocal rollover_calls
            rollover_calls += 1
            return None

        monkeypatch.setattr(
            graph._compaction_coordinator,
            "rollover_for_live_state",
            no_reclaim,
        )

        with pytest.raises(ContextBudgetExhausted):
            await graph._call_llm({
                "messages": [HumanMessage(content="finish the task")],
                "step_count": 0,
                "persona": "coordinate",
            })

        assert rollover_calls == 1
    finally:
        await delete_session(sid)


@pytest.mark.asyncio
async def test_provider_overflow_allows_only_one_live_rollover_rebuild(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.core.loop as loop_module
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.domain.compaction import CompactionResult, ContextBudgetExhausted

    class OverflowOnceModel(FakeStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def astream(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if False:
                    yield AIMessageChunk(content="")
                error = RuntimeError("context length exceeded")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            yield AIMessageChunk(content="answer")

    session = await create_session(workspace=str(tmp_path))
    sid = session.id
    try:
        monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
        monkeypatch.setattr(loop_module, "_llm_retry_sleep_delay", lambda _delay: 0)
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet"),
                workspace=str(tmp_path),
            ),
            api_key="test-key",
            session=session,
        )
        graph.model = OverflowOnceModel()

        prepare_calls = 0
        rollover_calls = 0

        def over_after_rebuild(messages, tool_defs, **kwargs):
            nonlocal prepare_calls
            prepare_calls += 1
            over_budget = prepare_calls >= 2
            return PreparedMainRequest(
                model_name=kwargs["model_name"],
                messages=list(messages),
                budget_messages=list(kwargs.get("budget_messages") or messages),
                tool_defs=list(tool_defs),
                context_limit=1000,
                main_output_reserve=100,
                safety_margin=50,
                total_input_tokens=900 if over_budget else 100,
                main_request_limit=850,
                should_rollover=over_budget,
                request_hash=f"request-{prepare_calls}",
            )

        async def one_rollover(*args, **kwargs):
            nonlocal rollover_calls
            rollover_calls += 1
            return CompactionResult(
                summary="",
                removed_messages=[],
                live_messages=[
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    HumanMessage(content="compacted live state"),
                ],
                tail_id=None,
            )

        monkeypatch.setattr(graph_module, "prepare_main_request", over_after_rebuild)
        monkeypatch.setattr(graph._compaction_coordinator, "rollover_for_live_state", one_rollover)

        with pytest.raises(ContextBudgetExhausted, match="after one rollover rebuild"):
            await graph._call_llm({
                "messages": [HumanMessage(content="finish the task")],
                "step_count": 0,
                "persona": "coordinate",
            })

        assert prepare_calls == 2
        assert rollover_calls == 1
        assert graph.model.calls == 1
    finally:
        await delete_session(sid)


@pytest.mark.asyncio
async def test_rollover_handles_unclosed_tool_batch_gracefully(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.core.loop as loop_module
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.domain.compaction import CompactionResult

    class FakeAnswerModel(FakeStreamingModel):
        async def astream(self, messages, *args, **kwargs):
            yield AIMessageChunk(content="repaired and answered")

    session = await create_session(workspace=str(tmp_path))
    sid = session.id
    try:
        monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
        monkeypatch.setattr(loop_module, "_llm_retry_sleep_delay", lambda _delay: 0)
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet"),
                workspace=str(tmp_path),
            ),
            api_key="sk-fake",
            session=session,
        )
        graph.model = FakeAnswerModel()

        # Save an unclosed tool batch into persistence
        await save_message(MessageRow(
            session_id=sid,
            role="user",
            content="task 1",
        ))
        await save_message(MessageRow(
            session_id=sid,
            role="assistant",
            content="",
            tool_calls=[{"id": "call_unclosed", "name": "bash", "args": {"command": "echo hi"}}],
        ))
        # Missing ToolMessage for call_unclosed! Followed directly by another user turn
        await save_message(MessageRow(
            session_id=sid,
            role="user",
            content="task 2: where is my answer?",
        ))

        # Force prepared_request.should_rollover = True on first prepare
        prepare_calls = 0
        original_prepare = graph_module.prepare_main_request

        def prepare_with_rollover(messages, tool_defs, **kwargs):
            nonlocal prepare_calls
            prepare_calls += 1
            prepared = original_prepare(messages, tool_defs, **kwargs)
            if prepare_calls == 1:
                return PreparedMainRequest(
                    model_name=prepared.model_name,
                    messages=prepared.messages,
                    budget_messages=prepared.budget_messages,
                    tool_defs=prepared.tool_defs,
                    context_limit=prepared.context_limit,
                    main_output_reserve=prepared.main_output_reserve,
                    safety_margin=prepared.safety_margin,
                    total_input_tokens=prepared.total_input_tokens,
                    main_request_limit=1,  # Force rollover
                    should_rollover=True,
                    request_hash=prepared.request_hash,
                    metadata=prepared.metadata,
                )
            return prepared

        monkeypatch.setattr(graph_module, "prepare_main_request", prepare_with_rollover)

        async def fake_compaction_agent(head, prev_summary):
            return "Summarized previous progress including interrupted tool"

        graph._compaction_coordinator.run_compaction_agent = fake_compaction_agent

        # This should NOT raise ContextBudgetExhausted("Cannot rollover an unclosed or invalid tool batch")
        result = await graph._call_llm({
            "messages": [HumanMessage(content="task 2: where is my answer?")],
            "step_count": 0,
            "persona": "coordinate",
        })

        assert result is not None
        assert result.get("should_continue") is not False
        # Verify model successfully answered after rollover
        last_msg = result["messages"][-1]
        assert isinstance(last_msg, AIMessage)
        assert last_msg.content == "repaired and answered"
    finally:
        await delete_session(sid)


@pytest.mark.asyncio
async def test_rollover_unrepairable_tool_batch_logs_warning_and_returns_none(tmp_path, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request
    import voidx.agent.adapters.langgraph.runtime.compaction_coordinator as coordinator_module

    host = SimpleNamespace(_session=None, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    coordinator = CompactionCoordinator(host)
    summary = AsyncMock(return_value="summary")

    messages = [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "read", "args": {}}]),
    ]
    prepared = prepare_main_request(messages, [], model_name="gpt-4o", context_limit=10000,
                                    output_token_max=512, safety_margin=256)

    # Force validate_closed_tool_batches to return False even after repair
    monkeypatch.setattr(coordinator_module, "validate_closed_tool_batches", lambda msgs: False)

    with caplog.at_level(logging.WARNING):
        result = await coordinator.rollover_for_live_state(
            messages, prepared_request=prepared, run_compaction_agent=summary,
        )

    assert result is None
    assert any("Cannot rollover: unclosed or invalid tool batch could not be repaired" in record.message for record in caplog.records)
