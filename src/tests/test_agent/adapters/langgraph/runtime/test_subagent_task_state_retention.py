import pytest
from langchain_core.messages import AIMessage

from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as subagent_module
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.agent.adapters.tools.subagent import AgentResultContract
from voidx.agent.application.agents import AgentDef
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, PlanResolution
from voidx.config import Config


class _CapturingModel:
    def __init__(self) -> None:
        self.history: list[list] = []

    def bind_tools(self, _tool_defs):
        return self


class _FakeUi:
    def step_header(self, _persona):
        return None

    def print(self, _text=""):
        return None


class _FakeEvents:
    async def emit(self, _event):
        return None

    def emit_direct(self, _event):
        return None


class _FakeUiPort:
    ui = _FakeUi()
    events = _FakeEvents()
    console = object()

    def via_events(self):
        return False


def _goal_resolution() -> GoalResolution:
    return GoalResolution(
        goal=GoalSpec(desc="task retention probe"),
        plan=PlanResolution(join="tdd", leave="verify"),
    )


def _contract() -> AgentResultContract:
    return AgentResultContract(format="")


@pytest.mark.asyncio
async def test_subagent_preserves_unchanged_snapshot_position(tmp_path, monkeypatch):
    history: list[list] = []

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **_kwargs):
        history.append(list(messages))
        if len(history) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {"file_path": "test.txt"},
                    "id": "call_1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="final answer")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=True, can_delegate=False),
        "Perform task with two steps",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(),
        result_contract=_contract(),
        parent_tools=build_registry(),
        debug=False,
        ui_port=_FakeUiPort(),
    )
    assert output == "final answer"
    assert len(history) == 2

    first_step_messages = history[0]
    second_step_messages = history[1]

    assert sum(str(m.content).count("## Current Task State") for m in first_step_messages) == 1

    assert sum(str(m.content).count("## Current Task State") for m in second_step_messages) == 1
    first_snapshot = next(m for m in first_step_messages if m.additional_kwargs.get("_voidx_task_state_snapshot"))
    assert second_step_messages[first_step_messages.index(first_snapshot)].content == first_snapshot.content


@pytest.mark.asyncio
async def test_subagent_does_not_append_unchanged_snapshot(tmp_path, monkeypatch):
    history: list[list] = []

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **_kwargs):
        history.append(list(messages))
        if len(history) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {"file_path": "test.txt"},
                    "id": "call_1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="final answer")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=True, can_delegate=False),
        "Perform task with two steps",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(),
        result_contract=_contract(),
        parent_tools=build_registry(),
        debug=False,
        ui_port=_FakeUiPort(),
    )
    assert output == "final answer"
    assert len(history) == 2

    second_step_messages = history[1]
    assert sum(str(m.content).count("## Current Task State") for m in second_step_messages) == 1


def test_subagent_has_no_retention_switch():
    import inspect
    assert "task_state_strip_enabled" not in inspect.signature(run_subagent).parameters


async def _invoke(tmp_path):
    return await run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=True, can_delegate=False),
        "Perform task", "test-key", Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(), result_contract=_contract(),
        parent_tools=build_registry(), debug=False, ui_port=_FakeUiPort(),
    )


def _observe_policy(monkeypatch, **thresholds):
    from voidx.agent.application.task_state_reminder import TaskStateReminderPolicy
    instances = []

    class ObservedPolicy(TaskStateReminderPolicy):
        def __init__(self):
            super().__init__(**thresholds)
            self.commits = []
            instances.append(self)

        def commit(self, decision, **kwargs):
            committed = super().commit(decision, **kwargs)
            if committed:
                self.commits.append(decision)
            return committed

    monkeypatch.setattr(subagent_module, "TaskStateReminderPolicy", ObservedPolicy)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    return instances


@pytest.mark.asyncio
@pytest.mark.parametrize("thresholds,expected", [
    ({"call_interval": 1}, ["initial", "unchanged", "call_interval"]),
    ({"token_interval": 1}, ["initial", "token_interval", "token_interval"]),
])
async def test_child_reminder_thresholds(tmp_path, monkeypatch, thresholds, expected):
    policies = _observe_policy(monkeypatch, **thresholds)
    requests = []

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) < 3:
            return AIMessage(content="", tool_calls=[{
                "name": "read", "args": {"file_path": str(tmp_path / "test.txt")},
                "id": f"read-{len(requests)}", "type": "tool_call",
            }])
        return AIMessage(content="done")

    (tmp_path / "test.txt").write_text("content")
    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await _invoke(tmp_path) == "done"
    assert [d.reason for d in policies[0].commits] == expected
    assert len(requests) == 3
    assert all(d.semantic_tokens < sum(subagent_module.estimate_message_tokens(m) for m in req)
               for d, req in zip(policies[0].commits, requests))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["retry", "failure", "cancel"])
async def test_child_commits_only_successful_request(tmp_path, monkeypatch, outcome):
    import asyncio
    from tests.test_agent.adapters.langgraph.runtime.test_subagent_llm_retry import ProviderError
    policies = _observe_policy(monkeypatch)
    requests = []

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        assert policies[0].state.snapshot is None
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "failure":
            raise RuntimeError("fatal")
        if len(requests) == 1:
            raise ProviderError("rate limited", status_code=429)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    if outcome == "retry":
        assert await _invoke(tmp_path) == "done"
        assert len(policies[0].commits) == 1
        assert [m.content for m in requests[0]] == [m.content for m in requests[1]]
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            await _invoke(tmp_path)
        assert policies[0].commits == []


@pytest.mark.asyncio
async def test_parallel_and_rebuilt_child_invocations_are_isolated(tmp_path, monkeypatch):
    import asyncio
    from voidx.agent.application.task_state_history import TaskStateHistory, task_state_snapshot
    from langchain_core.messages import HumanMessage
    parent_history = TaskStateHistory()
    parent_history.remember([HumanMessage(content="parent"), task_state_snapshot("parent snapshot")])
    policies = _observe_policy(monkeypatch)
    requests = []
    both_started = asyncio.Event()

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) >= 2:
            both_started.set()
        await both_started.wait()
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await asyncio.gather(_invoke(tmp_path), _invoke(tmp_path)) == ["done", "done"]
    assert await _invoke(tmp_path) == "done"
    assert len(policies) == 3
    assert all([d.reason for d in policy.commits] == ["initial"] for policy in policies)
    assert all(sum(bool(m.additional_kwargs.get("_voidx_task_state_snapshot")) for m in req) == 1 for req in requests)
    assert all("parent snapshot" not in str(req) for req in requests)
    assert parent_history.snapshot_count == 1


@pytest.mark.asyncio
async def test_finalize_retry_commits_actual_final_frame_once(tmp_path, monkeypatch):
    from voidx.agent.application.task_state_history import TaskStateHistory
    from tests.test_agent.adapters.langgraph.runtime.test_subagent_llm_retry import ProviderError
    policies = _observe_policy(monkeypatch)
    remembered = []
    requests = []

    class ObservedHistory(TaskStateHistory):
        def remember(self, messages):
            remembered.append(list(messages))
            super().remember(messages)

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        assert policies[0].state.snapshot is None
        if len(requests) == 1:
            raise ProviderError("rate limited", status_code=429)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "TaskStateHistory", ObservedHistory)
    monkeypatch.setattr(subagent_module, "subagent_convergence_action", lambda _decision: "finalize")
    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await _invoke(tmp_path) == "done"
    assert len(requests) == 2
    assert len(policies[0].commits) == 1
    assert remembered == [requests[-1]]
    assert any(m.additional_kwargs.get(subagent_module.GUIDANCE_MARKER) for m in remembered[0])


@pytest.mark.asyncio
async def test_discarded_success_after_deadline_commits_only_final_frame(tmp_path, monkeypatch):
    from types import SimpleNamespace

    policies = _observe_policy(monkeypatch)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(subagent_module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 0.0))
    requests = []

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            clock.now = 10**9
            return AIMessage(content="discarded response")
        assert policies[0].commits == []
        assert all("discarded response" not in str(m.content) for m in messages)
        return AIMessage(content="final frame")

    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await _invoke(tmp_path) == "final frame"
    assert len(requests) == 2
    assert [d.reason for d in policies[0].commits] == ["initial"]


@pytest.mark.asyncio
async def test_child_reminder_diagnostics_include_anchor_and_append(tmp_path, monkeypatch, caplog):
    import logging

    _observe_policy(monkeypatch)

    async def stream(*_args, **_kwargs):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    with caplog.at_level(logging.DEBUG, logger=subagent_module.__name__):
        assert await _invoke(tmp_path) == "done"
    records = [r.getMessage() for r in caplog.records if "child task reminder" in r.getMessage()]
    assert len(records) == 1
    assert "anchor_valid=True" in records[0]
    assert "append_snapshot=True" in records[0]


@pytest.mark.asyncio
async def test_child_default_reminder_on_seventh_request(tmp_path, monkeypatch):
    policies = _observe_policy(monkeypatch)
    requests = []
    for index in range(1, 7):
        (tmp_path / f"item-{index}").write_text(f"content {index}")

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) < 7:
            return AIMessage(content="", tool_calls=[{
                "name": "read", "args": {"file_path": str(tmp_path / f"item-{len(requests)}")},
                "id": f"read-{len(requests)}", "type": "tool_call",
            }])
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await _invoke(tmp_path) == "done"
    assert [d.reason for d in policies[0].commits] == ["initial", *(["unchanged"] * 5), "call_interval"]
    assert [sum(bool(m.additional_kwargs.get("_voidx_task_state_snapshot")) for m in req)
            for req in requests] == [1, 1, 1, 1, 1, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("delta,reason", [(8191, "unchanged"), (8192, "token_interval")])
async def test_child_default_token_boundary_in_compiled_request(tmp_path, monkeypatch, delta, reason):
    from langchain_core.messages import ToolMessage, SystemMessage

    policies = _observe_policy(monkeypatch)
    original_compile = subagent_module.ContextCompiler.compile_messages
    requests = []

    def compile_messages(compiler, source, **kwargs):
        compiled = original_compile(compiler, source, **kwargs)
        tools = [m for m in compiled if isinstance(m, ToolMessage)]
        if tools:
            tool = tools[-1]
            others = sum(subagent_module.estimate_message_tokens(m) for m in compiled
                         if m is not tool and not isinstance(m, SystemMessage)
                         and not m.additional_kwargs.get("_voidx_task_state_snapshot"))
            target = policies[0].state.semantic_tokens_at_snapshot + delta - others
            low, high = 0, 100000
            while low < high:
                mid = (low + high) // 2
                tool.content = " x" * mid
                if subagent_module.estimate_message_tokens(tool) < target:
                    low = mid + 1
                else:
                    high = mid
            tool.content = " x" * low
            assert subagent_module.estimate_message_tokens(tool) == target
        return compiled

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "read", "args": {"file_path": str(tmp_path / "missing")},
                "id": "read-boundary", "type": "tool_call",
            }])
        assert any(isinstance(m, ToolMessage) and len(m.content) > 10000 for m in messages)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module.ContextCompiler, "compile_messages", compile_messages)
    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    assert await _invoke(tmp_path) == "done"
    assert [d.reason for d in policies[0].commits] == ["initial", reason]
    assert policies[0].commits[-1].new_semantic_tokens == delta
    assert sum(bool(m.additional_kwargs.get("_voidx_task_state_snapshot")) for m in requests[-1]) == (2 if delta == 8192 else 1)


@pytest.mark.asyncio
async def test_child_preserves_snapshot_after_rewritten_compile_source(tmp_path, monkeypatch, caplog):
    import logging
    from langchain_core.messages import HumanMessage

    policies = _observe_policy(monkeypatch)
    original_compile = subagent_module.ContextCompiler.compile_messages
    sources = []
    requests = []

    def compile_messages(compiler, source, **kwargs):
        source = list(source)
        if policies[0].state.snapshot is not None:
            source = [HumanMessage(content="rewritten task history")
                      if isinstance(m, HumanMessage) and "Perform task" in str(m.content) else m
                      for m in source]
        sources.append(source)
        return original_compile(compiler, source, **kwargs)

    async def stream(_model, messages, *_args, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "read", "args": {"file_path": str(tmp_path / "missing")},
                "id": "rewrite", "type": "tool_call",
            }])
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module.ContextCompiler, "compile_messages", compile_messages)
    monkeypatch.setattr(subagent_module, "stream_llm", stream)
    with caplog.at_level(logging.DEBUG, logger=subagent_module.__name__):
        assert await _invoke(tmp_path) == "done"
    assert [d.reason for d in policies[0].commits] == ["initial", "unchanged"]
    assert policies[0].commits[-1].anchor_valid
    assert any(m.content == "rewritten task history" for m in sources[-1])
    assert any(m.content == "rewritten task history" for m in requests[-1])
    assert sum(bool(m.additional_kwargs.get("_voidx_task_state_snapshot")) for m in requests[-1]) == 1
    assert any("reason=unchanged" in r.getMessage() and "anchor_valid=True" in r.getMessage()
               and "append_snapshot=False" in r.getMessage() for r in caplog.records)
