from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text
from voidx.agent.adapters.langgraph.runtime.convergence import generate_fallback_summary
from voidx.agent.domain.turn_input import ActiveTurnInput
from voidx.agent.adapters.langgraph.runtime.turn_journal import TurnJournal
from voidx.agent.domain.compaction import CompactionMessageMetadata
from voidx.llm.message_markers import create_compaction_user_message
from voidx.llm.compaction.service import validate_closed_tool_batches
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    load_messages,
    replace_effective_message_range,
    save_message,
    materialize_legacy_summary_if_needed,
)
from voidx.agent.adapters.persistence.message_rows import compute_source_range_hash
from voidx.agent.ports.persistence import GoalRuntimeCorruption


def test_latest_user_text_with_dict_active_turn_input():
    # Finding 2: active_turn_input as dict from AgentState
    state_input = {
        "turn_journal_id": "tj_1",
        "raw_text": "raw user text",
        "semantic_text": "semantic user text",
        "title_text": "title",
    }
    messages = [create_compaction_user_message("## Summary", CompactionMessageMetadata(
        compaction_id="c1",
        source_range_hash="h1",
        replacement_operation_id="op1",
    ))]
    assert latest_user_text(messages, active_turn_input=state_input) == "semantic user text"


def test_convergence_generate_fallback_summary_skips_compaction_messages():
    # Finding 4: convergence skips compaction messages and respects active_turn_input
    compaction_msg = create_compaction_user_message("## Summary", CompactionMessageMetadata(
        compaction_id="c1",
        source_range_hash="h1",
        replacement_operation_id="op1",
    ))
    state = {
        "step_count": 5,
        "max_steps": 5,
        "messages": [compaction_msg],
        "active_turn_input": {
            "semantic_text": "user intention from active input",
        },
    }
    summary = generate_fallback_summary(state)
    assert "user intention from active input" in summary
    assert "## Summary" not in summary


def test_validate_closed_tool_batches_malformed_tool_calls():
    # Finding 7: malformed tool_calls must be rejected, not silently skipped
    m1 = AIMessage(content="")
    m1.tool_calls = ["not a dict"]  # type: ignore
    assert validate_closed_tool_batches([m1]) is False

    m2 = AIMessage(content="")
    m2.tool_calls = [{"name": "read"}]  # missing id
    assert validate_closed_tool_batches([m2]) is False


@pytest.mark.asyncio
async def test_load_messages_jsonl_detects_corrupted_replacement_event(tmp_path, monkeypatch):
    # Finding 5: corrupted message_replaced in jsonl raises error
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    # Append a bad message_replaced record directly to messages.jsonl where source_message_ids do not exist
    bad_record = {
        "type": "message_replaced",
        "operation_id": "op_corrupted",
        "source_message_ids": [9999, 10000],
        "source_range_hash": "bad_hash",
        "replacement": {
            "id": 100,
            "role": "user",
            "content": "summary",
        },
    }
    await jsonl_mod.append_session_record(sid, "messages.jsonl", bad_record)

    with pytest.raises(GoalRuntimeCorruption):
        await load_messages(sid)


@pytest.mark.asyncio
async def test_materialize_legacy_summary_if_needed(tmp_path, monkeypatch):
    # Finding 6: legacy summary migration
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    await save_message(MessageRow(session_id=sid, role="user", content="msg 1"))

    # Session has a legacy compaction_summary string, but no synthetic compaction row yet
    created_id = await materialize_legacy_summary_if_needed(
        sid,
        legacy_summary="## Legacy Long Summary",
    )
    assert created_id is not None

    msgs = await load_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].id == created_id
    assert msgs[0].role == "user"
    assert msgs[0].content == "## Legacy Long Summary"
    assert msgs[0].additional_kwargs.get("_voidx_compaction_message") is True
    assert msgs[0].additional_kwargs.get("compaction_depth") == 0

    # Idempotent: repeating does not create a second one
    created_id_again = await materialize_legacy_summary_if_needed(
        sid,
        legacy_summary="## Legacy Long Summary",
    )
    assert created_id_again is None
    msgs_after = await load_messages(sid)
    assert len(msgs_after) == 2


def test_prepared_budget_counts_full_provider_messages_not_semantic_subset():
    from langchain_core.messages import SystemMessage
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request

    user = HumanMessage(content="hello")
    messages = [SystemMessage(content="mandatory context " * 3000), user]
    prepared = prepare_main_request(
        messages, [], model_name="gpt-4o", context_limit=4096,
        output_token_max=512, safety_margin=256, budget_messages=[user],
    )
    complete = prepare_main_request(
        messages, [], model_name="gpt-4o", context_limit=4096,
        output_token_max=512, safety_margin=256,
    )
    assert prepared.total_input_tokens == complete.total_input_tokens
    assert prepared.should_rollover


def test_custom_message_counter_does_not_drop_tool_schema():
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request

    tools = [{"type": "function", "function": {
        "name": "read", "description": "tool instructions " * 100,
        "parameters": {"type": "object", "properties": {}},
    }}]
    kwargs = dict(model_name="gpt-4o", context_limit=4096,
                  token_counter=lambda messages, model: 10)
    without = prepare_main_request([HumanMessage(content="hello")], [], **kwargs)
    with_tools = prepare_main_request([HumanMessage(content="hello")], tools, **kwargs)
    assert with_tools.total_input_tokens > without.total_input_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    [ToolMessage(content="orphan", tool_call_id="missing")],
    [AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "call"}])],
    [AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "call"}]),
     ToolMessage(content="first", tool_call_id="call"),
     ToolMessage(content="duplicate", tool_call_id="call")],
])
async def test_rollover_repairs_invalid_source_before_summarizing(invalid):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request

    host = SimpleNamespace(_session=None, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    coordinator = CompactionCoordinator(host)
    summary = AsyncMock(return_value="summary")
    messages = [HumanMessage(content="task " * 2000), *invalid]
    prepared = prepare_main_request(messages, [], model_name="gpt-4o", context_limit=10000,
                                    output_token_max=512, safety_margin=256)
    result = await coordinator.rollover_for_live_state(
        messages, prepared_request=prepared, run_compaction_agent=summary,
    )
    assert result is not None
    summary.assert_awaited()


@pytest.mark.asyncio
async def test_rollover_candidate_includes_mandatory_context_before_commit():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from langchain_core.messages import SystemMessage
    from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request
    from voidx.agent.domain.compaction import ContextBudgetExhausted

    host = SimpleNamespace(_session=None, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    coordinator = CompactionCoordinator(host)
    user = HumanMessage(content="task " * 3000)
    system = SystemMessage(content="mandatory context " * 3000)
    prepared = prepare_main_request([system, user], [], model_name="gpt-4o",
                                    context_limit=4096, output_token_max=512, safety_margin=256)
    with pytest.raises(ContextBudgetExhausted):
        await coordinator.rollover_for_live_state(
            [user], prepared_request=prepared, run_compaction_agent=AsyncMock(return_value="summary"),
        )


@pytest.mark.asyncio
async def test_rollover_requires_spec_minimum_net_reclaim():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request

    host = SimpleNamespace(_session=None, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    coordinator = CompactionCoordinator(host)
    messages = [HumanMessage(content="task " * 100)]
    prepared = prepare_main_request(messages, [], model_name="gpt-4o", context_limit=10000,
                                    output_token_max=512, safety_margin=256)
    result = await coordinator.rollover_for_live_state(
        messages, prepared_request=prepared, run_compaction_agent=AsyncMock(return_value="summary"),
    )
    assert result is None


@pytest.mark.asyncio
async def test_rollover_validates_rebuilt_candidate_with_continuation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from langchain_core.messages import SystemMessage
    from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request
    from voidx.agent.domain.compaction import ContextBudgetExhausted
    from voidx.llm.message_markers import is_continuation_message

    host = SimpleNamespace(_session=None, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    coordinator = CompactionCoordinator(host)
    messages = [HumanMessage(content="task " * 3000)]
    kwargs = dict(model_name="gpt-4o", context_limit=4096, output_token_max=512, safety_margin=256)
    prepared = prepare_main_request(messages, [], **kwargs)
    candidates = []

    def rebuild(candidate):
        candidates.append(candidate)
        return prepare_main_request([SystemMessage(content="runtime " * 5000), *candidate], [], **kwargs)

    with pytest.raises(ContextBudgetExhausted):
        await coordinator.rollover_for_live_state(
            messages, prepared_request=prepared,
            run_compaction_agent=AsyncMock(return_value="summary"), prepare_candidate=rebuild,
        )
    assert len(candidates) == 1
    assert sum(is_continuation_message(m) for m in candidates[0]) == 1


@pytest.mark.asyncio
async def test_goal_rollover_uses_fenced_replacement_and_advances_sequence(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import voidx.agent.adapters.langgraph.runtime.compaction_coordinator as module
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request
    from voidx.agent.domain.compaction import ReplacementResult

    session = SimpleNamespace(id="goal-session")
    context = SimpleNamespace(session_id=session.id, goal_generation="gen", goal_attempt_id="attempt",
                              goal_attempt_number=1, goal_lease_owner="owner", goal_fencing_token=7)
    execution = SimpleNamespace(session=session, turn_context=context, turn_journal=None,
                                segment_index=0, goal_transcript_local_sequence=4)
    host = SimpleNamespace(_session=session, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    rows = [MessageRow(id=1, session_id=session.id, role="user", content="task " * 4000)]
    monkeypatch.setattr(module, "current_thread_execution_state", lambda: execution)
    monkeypatch.setattr(module, "load_messages", AsyncMock(return_value=rows))
    ordinary = AsyncMock(return_value=ReplacementResult(True, "op", 2, 1, [1]))
    fenced = AsyncMock(return_value=ReplacementResult(True, "op", 2, 1, [1]))
    monkeypatch.setattr(module, "replace_effective_message_range", ordinary)
    monkeypatch.setattr(module, "replace_goal_transcript_message_range", fenced, raising=False)
    monkeypatch.setattr(module, "gc_context_frames", AsyncMock())
    messages = [HumanMessage(content=rows[0].content)]
    prepared = prepare_main_request(messages, [], model_name="gpt-4o", context_limit=10000,
                                    output_token_max=512, safety_margin=256)
    result = await module.CompactionCoordinator(host).rollover_for_live_state(
        messages, prepared_request=prepared, run_compaction_agent=AsyncMock(return_value="summary"),
    )
    assert result is not None
    ordinary.assert_not_awaited()
    assert fenced.await_args.kwargs["fencing_token"] == 7
    assert fenced.await_args.kwargs["local_sequence"] == 5
    assert execution.goal_transcript_local_sequence == 5
    assert not getattr(host, "_compaction_summary", "")


@pytest.mark.asyncio
async def test_rollover_conflict_reloads_winner_into_live_state(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import voidx.agent.adapters.langgraph.runtime.compaction_coordinator as module
    from voidx.agent.adapters.langgraph.runtime.prepared_request import prepare_main_request
    from voidx.agent.domain.compaction import ReplacementResult

    session = SimpleNamespace(id="session")
    host = SimpleNamespace(_session=session, config=SimpleNamespace(model=SimpleNamespace(model="gpt-4o")))
    source = MessageRow(id=1, session_id=session.id, role="user", content="task " * 4000)
    winner = MessageRow(id=2, session_id=session.id, role="user", content="winner summary",
                        additional_kwargs=create_compaction_user_message("winner summary", CompactionMessageMetadata(
                            compaction_id="winner", source_range_hash="hash", replacement_operation_id="winner-op",
                        )).additional_kwargs)
    monkeypatch.setattr(module, "load_messages", AsyncMock(side_effect=[[source], [winner]]))
    monkeypatch.setattr(module, "replace_effective_message_range", AsyncMock(
        return_value=ReplacementResult(False, "loser", 2, 1, [1], "winner-op")))
    messages = [HumanMessage(content=source.content)]
    prepared = prepare_main_request(messages, [], model_name="gpt-4o", context_limit=10000,
                                    output_token_max=512, safety_margin=256)
    result = await module.CompactionCoordinator(host).rollover_for_live_state(
        messages, prepared_request=prepared, run_compaction_agent=AsyncMock(return_value="loser summary"),
    )
    assert result is not None
    assert result.live_messages[1].content == "winner summary"
    assert result.metadata["replacement_operation_id"] == "winner-op"
