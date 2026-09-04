"""Child context handoff: the parent passes an explicit, trimmed
ChildContextHandoff (instructions/profile sections/summary + provenance),
and a missing handoff is visibly marked in the child task payload."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as subagent_module
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.agent.adapters.tools.subagent import AgentResultContract
from voidx.agent.application.agents import AgentDef
from voidx.agent.application.context_handoff import ChildContextHandoff
from voidx.agent.domain.prompt_contracts import ContextSection

from voidx.agent.domain.task.state import (

    GoalResolution,
    GoalSpec,
    PlanResolution,
)
from voidx.config import Config


class _CapturingModel:
    def __init__(self) -> None:
        self.messages: list = []

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
        goal=GoalSpec(desc="handoff probe"),
        plan=PlanResolution(join="review", leave="review"),
    )


def _contract() -> AgentResultContract:
    return AgentResultContract(format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions")


async def _run_and_capture(tmp_path, monkeypatch, **extra_kwargs):
    model = _CapturingModel()

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **_kwargs):
        model.messages = list(messages)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=False, can_delegate=False),
        "Probe the handoff context",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(),
        result_contract=_contract(),
        parent_tools=build_registry(),
        debug=False,
        ui_port=_FakeUiPort(),
        **extra_kwargs,
    )
    assert output == "done"
    return model.messages


def _system_text(messages: list) -> str:
    return "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))


def _task_text(messages: list) -> str:
    return "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))


@pytest.mark.asyncio
async def test_child_receives_handoff_instructions_and_summary(tmp_path, monkeypatch):
    handoff = ChildContextHandoff(
        instructions=("Instructions from: /repo/AGENTS.md\nAlways run ./test.py first.",),
        summary="Parent already verified the schema export works.",
        source_paths=("/repo/AGENTS.md",),
    )

    messages = await _run_and_capture(tmp_path, monkeypatch, context_handoff=handoff)

    system_text = _system_text(messages)
    assert "Always run ./test.py first." in system_text
    assert "Parent already verified the schema export works." in system_text


@pytest.mark.asyncio
async def test_child_receives_handoff_profile_sections(tmp_path, monkeypatch):
    handoff = ChildContextHandoff(
        instructions=("Instructions from: /repo/AGENTS.md\nRule one.",),
        profile_sections=(ContextSection(name="Profile Note", content="Use concise Chinese replies."),),
    )

    messages = await _run_and_capture(tmp_path, monkeypatch, context_handoff=handoff)

    assert "Use concise Chinese replies." in _system_text(messages)


@pytest.mark.asyncio
async def test_missing_handoff_marks_task_payload(tmp_path, monkeypatch):
    messages = await _run_and_capture(tmp_path, monkeypatch)

    task_text = _task_text(messages)
    assert "Context handoff: none" in task_text


@pytest.mark.asyncio
async def test_present_handoff_has_no_missing_marker(tmp_path, monkeypatch):
    handoff = ChildContextHandoff(
        instructions=("Instructions from: /repo/AGENTS.md\nRule one.",),
        source_paths=("/repo/AGENTS.md",),
    )

    messages = await _run_and_capture(tmp_path, monkeypatch, context_handoff=handoff)

    task_text = _task_text(messages)
    assert "Context handoff: none" not in task_text


@pytest.mark.asyncio
async def test_child_receives_only_own_task_not_parent_transcript(tmp_path, monkeypatch):
    handoff = ChildContextHandoff(
        instructions=("Instructions from: /repo/AGENTS.md\nRule one.",),
    )

    messages = await _run_and_capture(tmp_path, monkeypatch, context_handoff=handoff)

    human_messages = [m for m in messages if isinstance(m, HumanMessage)]
    assert len(human_messages) == 1
    assert "Probe the handoff context" in human_messages[0].content
