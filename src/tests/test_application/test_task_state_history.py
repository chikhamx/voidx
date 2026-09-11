from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.application.task_state_history import TaskStateHistory, task_state_snapshot


def test_restore_keeps_old_state_before_new_tool_exchange():
    history = TaskStateHistory()
    user = HumanMessage(content="work")
    first = [SystemMessage(content="system"), user, task_state_snapshot("state X")]
    history.remember(first)
    call = AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "a", "type": "tool_call"}])
    result = ToolMessage(content="result", tool_call_id="a")
    second = history.restore([first[0], user, call, result, task_state_snapshot("state Y")])
    assert [m.content for m in second] == ["system", "work", "state X", "", "result", "state Y"]
    history.remember(second)
    third = history.restore([first[0], user, call, result, AIMessage(content="done"), HumanMessage(content="next"), task_state_snapshot("state Z")])
    assert [m.content for m in third][:len(second)] == [m.content for m in second]


def test_rebuild_and_failed_retry_do_not_accumulate_snapshots():
    history = TaskStateHistory()
    original = [HumanMessage(content="work"), task_state_snapshot("X")]
    history.remember(original)
    candidate = [HumanMessage(content="work"), AIMessage(content="done"), task_state_snapshot("Y")]
    once = history.restore(candidate)
    twice = history.restore(once)
    assert [m.content for m in twice] == ["work", "X", "done", "Y"]
    assert [m.content for m in history.restore(candidate)] == [m.content for m in once]


def test_changed_content_keeps_snapshots():
    history = TaskStateHistory()
    history.remember([HumanMessage(content="old"), task_state_snapshot("X")])
    result = history.restore([HumanMessage(content="compacted"), task_state_snapshot("Y")])
    assert [m.content for m in result] == ["compacted", "X", "Y"]


def test_reloaded_message_ids_do_not_lose_retained_state():
    history = TaskStateHistory()
    history.remember([HumanMessage(content="work", id="live"), task_state_snapshot("X")])
    result = history.restore([HumanMessage(content="work", id="db-row"), AIMessage(content="done"), task_state_snapshot("Y")])
    assert [m.content for m in result] == ["work", "X", "done", "Y"]


def test_content_edits_preserve_all_snapshots_until_explicit_clear():
    history = TaskStateHistory()
    original = [HumanMessage(content="task"), task_state_snapshot("first"), AIMessage(content="old"), task_state_snapshot("second")]
    history.remember(original)
    rewritten = [HumanMessage(content="task"), AIMessage(content="rewritten")]
    assert history.snapshot_count == 2
    assert history.anchors_valid(original)
    assert not history.history_reset(original + [HumanMessage(content="next")])
    assert not history.history_reset(rewritten)
    assert history.anchors_valid(rewritten)
    assert [m.content for m in history.restore(rewritten)] == ["task", "first", "rewritten", "second"]
    assert history.snapshot_count == 2
    assert not history.history_reset(original)
    history.clear()
    assert history.snapshot_count == 0
    assert not history.history_reset(rewritten)
    assert history.restore(rewritten) == rewritten


def test_persisted_tool_roundtrip_preserves_cross_turn_snapshots():
    from voidx.agent.adapters.persistence.message_rows import messages_from_rows
    from voidx.agent.adapters.persistence.session_models import MessageRow

    calls = [{"name": "read", "args": {}, "id": "call_1", "type": "tool_call"}]
    history = TaskStateHistory()
    history.remember([
        HumanMessage(content="work"), task_state_snapshot("initial"),
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content="result", tool_call_id="call_1", name="read"),
        task_state_snapshot("after tool"),
    ])
    reloaded = messages_from_rows([
        MessageRow(session_id="s", role="user", content="work"),
        MessageRow(session_id="s", role="assistant", content="", tool_calls=calls),
        MessageRow(session_id="s", role="tool", content="result", tool_call_id="call_1"),
        MessageRow(session_id="s", role="assistant", content="done"),
        MessageRow(session_id="s", role="user", content="next turn"),
    ])
    assert history.anchors_valid(reloaded)
    restored = history.restore([*reloaded, task_state_snapshot("next")])
    assert [m.content for m in restored] == [
        "work", "initial", "", "result", "after tool", "done", "next turn", "next",
    ]
    for update in ({"content": "changed"}, {"tool_call_id": "different"}):
        changed = list(reloaded)
        changed[2] = changed[2].model_copy(update=update)
        assert history.history_reset(changed) == ("tool_call_id" in update)
        assert [m.content for m in history.restore(changed)] == [
            "work", "initial", "", changed[2].content, "after tool", "done", "next turn",
        ]


def test_non_tool_message_name_changes_do_not_reset_history():
    history = TaskStateHistory()
    history.remember([HumanMessage(content="work", name="one"), task_state_snapshot("state")])
    assert not history.history_reset([HumanMessage(content="work", name="two")])


def test_trimmed_tool_body_and_args_survive_reload_and_repeated_builds():
    history = TaskStateHistory()
    calls = [{"name": "read", "args": {"path": "large original args"}, "id": "a", "type": "tool_call"}]
    history.remember([
        HumanMessage(content="work", id="live"), task_state_snapshot("first"),
        AIMessage(content="original", tool_calls=calls, id="live-ai"),
        ToolMessage(content="large original body", tool_call_id="a", name="read"),
        task_state_snapshot("second"),
    ])
    reloaded = [
        HumanMessage(content="work", id="db-user"),
        AIMessage(content="trimmed", tool_calls=[{**calls[0], "args": {}, "name": "renamed"}], id="db-ai"),
        ToolMessage(content="trimmed", tool_call_id="a"),
        HumanMessage(content="next"),
    ]
    assert history.anchors_valid(reloaded)
    expected = ["work", "first", "trimmed", "trimmed", "second", "next"]
    for _ in range(3):
        restored = history.restore(reloaded)
        assert [m.content for m in restored] == expected
        assert [m.content for m in history.restore(restored)] == expected
        history.remember(restored)
        assert history.snapshot_count == 2


def test_shrinking_history_maps_stable_calls_and_keeps_unmatched_snapshots():
    history = TaskStateHistory()
    history.remember([
        HumanMessage(content="work"), task_state_snapshot("first"),
        ToolMessage(content="old a", tool_call_id="a"), task_state_snapshot("second"),
        ToolMessage(content="old b", tool_call_id="b"), task_state_snapshot("third"),
        AIMessage(content="done"), task_state_snapshot("fourth"),
    ])
    remaining = [ToolMessage(content="trim b", tool_call_id="b")]
    assert not history.anchors_valid(remaining)
    restored = history.restore(remaining)
    assert [m.content for m in restored] == ["first", "second", "trim b", "third", "fourth"]
    assert history.snapshot_count == 4
    history.remember(restored)
    assert [m.content for m in history.restore([])] == ["first", "second", "third", "fourth"]
    assert history.snapshot_count == 4
    history.clear()
    assert history.restore([]) == []
    assert history.snapshot_count == 0
