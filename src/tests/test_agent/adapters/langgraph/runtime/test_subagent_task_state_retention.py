import pytest
from langchain_core.messages import AIMessage, ToolMessage

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
async def test_subagent_retains_task_state_when_strip_disabled(tmp_path, monkeypatch):
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
        task_state_strip_enabled=False,
    )
    assert output == "final answer"
    assert len(history) == 2

    first_step_messages = history[0]
    second_step_messages = history[1]

    # In first step, task state snapshot was generated
    assert sum(str(m.content).count("## Current Task State") for m in first_step_messages) == 1

    # In second step with strip=False, the previous step's task state snapshot is retained
    assert sum(str(m.content).count("## Current Task State") for m in second_step_messages) == 2


@pytest.mark.asyncio
async def test_subagent_strips_task_state_by_default(tmp_path, monkeypatch):
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
        task_state_strip_enabled=True,
    )
    assert output == "final answer"
    assert len(history) == 2

    second_step_messages = history[1]
    # Default (strip=True) strips historical task state, keeping only the latest
    assert sum(str(m.content).count("## Current Task State") for m in second_step_messages) == 1
