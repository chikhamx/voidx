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


def test_changed_history_discards_snapshots_after_changed_prefix():
    history = TaskStateHistory()
    history.remember([HumanMessage(content="old"), task_state_snapshot("X")])
    result = history.restore([HumanMessage(content="compacted"), task_state_snapshot("Y")])
    assert [m.content for m in result] == ["compacted", "Y"]


def test_reloaded_message_ids_do_not_lose_retained_state():
    history = TaskStateHistory()
    history.remember([HumanMessage(content="work", id="live"), task_state_snapshot("X")])
    result = history.restore([HumanMessage(content="work", id="db-row"), AIMessage(content="done"), task_state_snapshot("Y")])
    assert [m.content for m in result] == ["work", "X", "done", "Y"]
