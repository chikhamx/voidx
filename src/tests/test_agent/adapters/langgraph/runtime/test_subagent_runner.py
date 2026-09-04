"""Regression tests for core graph behavior."""

from tests.tool_registry import build_registry
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.persistence.sqlite as store

from voidx.agent.application.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
)
from voidx.agent.application.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.adapters.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.adapters.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.adapters.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.adapters.persistence.message_rows import RowMessageCacheEntry
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, Settings
from voidx.agent.domain.user_profile import UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.agent.application.instruction import InstructionService, WorkflowRuntimeContext
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, PlanResolution


from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.task.state import TaskState, ToolStatePatch

from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.domain.capability import ToolCapability
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return make_langgraph_execution(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


def _asked_tool_calls(batch):
    return [getattr(item, "tool_call", item) for item in batch]


def _child_goal_resolution(
    goal_type: str = "feature",
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        goal=GoalSpec(desc=desc),
        plan=PlanResolution(join=join, leave=leave),
    )


def _child_result_contract(contract_type: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if contract_type == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(format=result_format)


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    contract_type: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(contract_type),
    }


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _tree_nodes(root):
    nodes = [root]
    for child in root.children:
        nodes.extend(_tree_nodes(child))
    return nodes


def test_run_subagent_uses_workflow_contract_instead_of_model_budget():
    import inspect

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    parameters = inspect.signature(run_subagent).parameters
    assert "max_steps" not in parameters
    assert parameters["goal_resolution"].default is inspect.Parameter.empty
    assert parameters["result_contract"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_subagent_runner_passes_main_workflow_runtime_context(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    graph = _graph(tmp_path)
    goal_resolution = _child_goal_resolution()
    result_contract = _child_result_contract()
    expected_context = WorkflowRuntimeContext(
        instructions=["instruction"],
        active=["tdd (implement persona)"],
        content="skill context",
        runs=[
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="feature",
                scope="Implement the feature",
                personas=["implement"],
            )
        ],
    )
    calls: list[dict] = []
    captured: dict[str, object] = {}
    emitted: list[object] = []

    class RecordingEvents:
        async def emit(self, event):
            emitted.append(event)

    async def fake_workflow_context_for(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return expected_context

    async def fake_run_subagent(*_args, **kwargs):
        from voidx.presentation.output.events import TodoItemPayload, TodoUpdated

        captured.update(kwargs)
        kwargs["run_metadata"].update({"calls": 2, "tokens": 1234})
        await kwargs["ui_port"].events.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(id="child", content="child work", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._ui.__dict__.pop("via_events", None)
    graph._ui._events = RecordingEvents()
    graph._ui.via_events = lambda: True
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Implement the feature",
        goal_resolution,
        result_contract,
    )

    assert result == "child result"
    assert captured["goal_resolution"] == goal_resolution
    assert captured["result_contract"] == result_contract
    assert captured["runtime_persona"] == "implement"
    assert captured["workflow_runtime_context"] is expected_context
    assert "todo_state_sink" not in captured
    assert "skill_selection" not in captured
    assert ("parent" + "_messages") not in captured
    assert [event.kind for event in emitted] == [
        "subagent.started",
        "todo.updated",
        "subagent.finished",
    ]
    assert emitted[-1].summary == "child result"
    assert emitted[-1].calls == 2
    assert emitted[-1].tokens == 1234
    assert "agent" not in calls[0]["kwargs"]
    assert calls[0]["kwargs"]["goal_type"] == "feature"
    assert calls[0]["kwargs"]["scope"] == "Implement the feature"
    assert calls[0]["kwargs"]["workflow_start"] == "tdd"




@pytest.mark.asyncio
async def test_subagent_runner_reports_initialization_error(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    graph = _graph(tmp_path)
    emitted: list[object] = []

    class RecordingEvents:
        async def emit(self, event):
            emitted.append(event)

    async def fail_workflow_context_for(*_args, **_kwargs):
        raise RuntimeError("provider schema rejected tool definitions")

    graph._instruction.workflow_context_for = fail_workflow_context_for
    graph._ui.__dict__.pop("via_events", None)
    graph._ui._events = RecordingEvents()
    graph._ui.via_events = lambda: True

    with pytest.raises(RuntimeError, match="provider schema rejected tool definitions"):
        await graph._subagent_runner(
            get_agent("voidx"),
            "Inspect the backend",
            _child_goal_resolution("inspect"),
            _child_result_contract("inspection_result"),
        )

    assert emitted[-1].kind == "subagent.finished"
    assert emitted[-1].ok is False
    assert emitted[-1].error == "provider schema rejected tool definitions"


@pytest.mark.asyncio
async def test_subagent_runner_persists_lifecycle_jsonl(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    graph = _graph(tmp_path)
    graph._session = await create_session(workspace=str(tmp_path))
    goal_resolution = _child_goal_resolution("inspect", desc="Inspect storage design", join="review", leave="review")
    result_contract = _child_result_contract("inspection_result")

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[
                WorkflowRunState(
                    name="review",
                    status=WorkflowRunStatus.ACTIVE,
                    goal_type="inspect",
                    scope="Inspect storage design",
                    personas=["review"],
                )
            ],
        )

    async def fake_run_subagent(*_args, **kwargs):
        kwargs["run_metadata"].update({
            "finish_reason": "final_answer",
        })
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    try:
        result = await graph._subagent_runner(
            get_agent("voidx"),
            "Inspect storage design",
            goal_resolution,
            result_contract,
        )

        path = store.DATA_DIR / "sessions" / graph._session.id / "subagents" / "agent_0.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert result == "child result"
        assert [row["type"] for row in rows] == ["subagent_start", "subagent_finish"]
        assert rows[0]["agent_run_id"] == "agent_0"
        assert rows[0]["persona"] == "review"
        assert rows[0]["description"] == "Inspect storage design"
        assert rows[0]["goal_resolution"]["goal"]["desc"] == "Inspect storage design"
        assert "result_schema" not in rows[0]
        assert rows[1]["ok"] is True
        assert rows[1]["finish_reason"] == "final_answer"
    finally:
        await delete_session(graph._session.id)


@pytest.mark.asyncio
async def test_subagent_runner_persists_failure_error_to_jsonl(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    graph = _graph(tmp_path)
    graph._session = await create_session(workspace=str(tmp_path))
    goal_resolution = _child_goal_resolution(
        "inspect",
        desc="Inspect failing storage",
        join="review",
        leave="review",
    )

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[
                WorkflowRunState(
                    name="review",
                    status=WorkflowRunStatus.ACTIVE,
                    goal_type="inspect",
                    scope="Inspect failing storage",
                    personas=["review"],
                )
            ],
        )

    async def fail_run_subagent(*_args, **_kwargs):
        raise RuntimeError("child execution failed")

    graph._instruction.workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "_run_subagent", fail_run_subagent)

    try:
        with pytest.raises(RuntimeError, match="child execution failed"):
            await graph._subagent_runner(
                get_agent("voidx"),
                "Inspect failing storage",
                goal_resolution,
                _child_result_contract("inspection_result"),
            )

        path = store.DATA_DIR / "sessions" / graph._session.id / "subagents" / "agent_0.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        finish = rows[-1]
        assert finish["type"] == "subagent_finish"
        assert finish["ok"] is False
        assert finish["finish_reason"] == "error"
        assert finish["error"] == "child execution failed"
    finally:
        await delete_session(graph._session.id)

@pytest.mark.asyncio
async def test_subagent_runner_authorizes_with_child_interaction_mode(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    graph = _graph(tmp_path)
    authorize_calls: list[dict] = []
    goal_resolution = _child_goal_resolution("design", desc="Plan the feature", join="plan", leave="plan")
    result_contract = _child_result_contract("design_result")

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[
                WorkflowRunState(
                    name="plan",
                    status=WorkflowRunStatus.ACTIVE,
                    goal_type="design",
                    scope="Plan the feature",
                    personas=["plan"],
                )
            ],
        )

    async def fake_authorize(tool_calls, **kwargs):
        authorize_calls.append(kwargs)
        return tool_calls, []

    async def fake_run_subagent(*_args, **kwargs):
        await kwargs["authorize_tools"]([])
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._authorize_tool_calls = fake_authorize
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Plan the feature",
        goal_resolution,
        result_contract,
    )

    assert result == "child result"
    assert authorize_calls[0]["plan_mode"] is True
    assert authorize_calls[0]["interaction_mode"] == "plan"


@pytest.mark.asyncio
async def test_graph_authorization_does_not_treat_goal_as_read_only_mode(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        interaction_mode="goal",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["replace"]]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_graph_authorization_allows_read_only_bash(tmp_path):
    graph = _graph(tmp_path)

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_edit(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["replace"]]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_allow_for_edit(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.allow_silent("edit")

    async def fail_if_asked(_tool_calls):
        pytest.fail("session-allowed edit should not prompt")

    graph._ask_tool_permission = fail_if_asked

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_unsafe_bash(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "pip install requests"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["bash"]]




@pytest.mark.asyncio
async def test_subagent_tool_result_injects_next_step_hint_into_followup_messages(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    calls = 0

    class HintTool:
        child_shareable = True
        id = "search"
        description = "Returns a next step hint."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(
                output="tool output",
                next_step_hint="Run the focused verification now.",
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {}, "id": "call-hint"}],
            )
        return AIMessage(
            content=(
                "status: complete\nfiles_changed: none\n"
                "tests_run: focused\nrisks: none\nfollowups: none"
            )
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    tool = HintTool()
    parent_tools.replace("search", tool, tool.description, tool.parameters_schema())

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    result = await run_subagent(
        get_agent("voidx"),
        "Use the hint tool",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert "status: complete" in result
    followup_tools = [message for message in observed[1] if isinstance(message, ToolMessage)]
    assert len(followup_tools) == 1
    assert "Next step hint: Run the focused verification now." in followup_tools[0].content


@pytest.mark.asyncio
async def test_subagent_state_patch_is_applied_to_next_turn_context(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    calls = 0

    class StatePatchTool:
        child_shareable = True
        id = "search"
        description = "Updates workflow state."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus

            return ToolResult(
                output="workflow advanced",
                next_step_hint="Verify the implementation.",
                metadata={
                    "state_patch": ToolStatePatch(
                        workflow_runs=[
                            WorkflowRunState(
                                name="verify",
                                status=WorkflowRunStatus.ACTIVE,
                                goal="Verify the implementation",
                            )
                        ]
                    ).model_dump(mode="json", exclude_unset=True)
                },
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {}, "id": "call-state-patch"}],
            )
        return AIMessage(
            content=(
                "status: complete\nfiles_changed: none\n"
                "tests_run: focused\nrisks: none\nfollowups: none"
            )
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    tool = StatePatchTool()
    parent_tools.replace("search", tool, tool.description, tool.parameters_schema())

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    await run_subagent(
        get_agent("voidx"),
        "Advance the child workflow",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert any(
        "Active workflows: verify" in str(message.content)
        for message in observed[1]
    )
    assert any(
        "Next step hint: Verify the implementation." in str(message.content)
        for message in observed[1]
        if isinstance(message, ToolMessage)
    )


@pytest.mark.asyncio
async def test_subagent_refreshes_workflow_runtime_after_route_patch(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    calls = 0

    class RouteTool:
        child_shareable = True
        id = "search"
        description = "Changes the child workflow route."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(
                output="route changed",
                metadata={
                    "state_patch": ToolStatePatch(
                        plan=PlanResolution(join="review", leave="review"),
                    ).model_dump(mode="json", exclude_unset=True)
                },
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {}, "id": "call-route"}],
            )
        return AIMessage(
            content=(
                "status: complete\nfiles_changed: none\n"
                "tests_run: focused\nrisks: none\nfollowups: none"
            )
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    tool = RouteTool()
    parent_tools.replace("search", tool, tool.description, tool.parameters_schema())

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    await run_subagent(
        get_agent("voidx"),
        "Change the child workflow route",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        workflow_runtime_context=WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[],
            dag=DEFAULT_WORKFLOW_DAG,
        ),
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    second_system = next(message for message in observed[1] if isinstance(message, SystemMessage))
    assert "Active route joins at review and leaves at review." in second_system.content
    assert "Active route joins at tdd and leaves at verify." not in second_system.content


@pytest.mark.asyncio
async def test_subagent_removes_completed_workflow_from_active_summaries(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    calls = 0

    class CompleteWorkflowTool:
        child_shareable = True
        id = "search"
        description = "Completes the current workflow."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus

            return ToolResult(
                output="workflow completed",
                metadata={
                    "state_patch": ToolStatePatch(
                        workflow_runs=[
                            WorkflowRunState(
                                name="tdd",
                                status=WorkflowRunStatus.SATISFIED,
                                goal="Implement the feature",
                            )
                        ]
                    ).model_dump(mode="json", exclude_unset=True)
                },
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "search", "args": {}, "id": "call-complete"}
                ],
            )
        return AIMessage(
            content=(
                "status: complete\nfiles_changed: none\n"
                "tests_run: focused\nrisks: none\nfollowups: none"
            )
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    tool = CompleteWorkflowTool()
    parent_tools.replace("search", tool, tool.description, tool.parameters_schema())
    initial_run = WorkflowRunState(
        name="tdd",
        status=WorkflowRunStatus.ACTIVE,
        goal="Implement the feature",
    )

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    await run_subagent(
        get_agent("voidx"),
        "Complete the child workflow",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        workflow_runtime_context=WorkflowRuntimeContext(
            instructions=[],
            active=["tdd (implement persona)"],
            runs=[initial_run],
        ),
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    second_messages = observed[1]
    assert not any(
        "Active workflow nodes: tdd (implement persona)" in str(message.content)
        for message in second_messages
    )
    assert not any(
        "Active workflows: tdd" in str(message.content)
        for message in second_messages
    )


@pytest.mark.asyncio
async def test_subagent_route_patch_counts_as_progress_for_runtime_guard(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    calls = 0
    summaries = []

    class RouteTool:
        child_shareable = True
        id = "search"
        description = "Changes the child workflow route."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(
                output="",
                metadata={
                    "workflow_guidance": True,
                    "state_patch": ToolStatePatch(
                        plan=PlanResolution(join="review", leave="review"),
                    ).model_dump(mode="json", exclude_unset=True),
                },
            )

    original_cycle_summary = subagent_module.cycle_summary_from_tools

    def record_cycle_summary(*args, **kwargs):
        summary = original_cycle_summary(*args, **kwargs)
        summaries.append(summary)
        return summary

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {}, "id": "call-route"}],
            )
        return AIMessage(
            content=(
                "status: complete\nfiles_changed: none\n"
                "tests_run: focused\nrisks: none\nfollowups: none"
            )
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "cycle_summary_from_tools", record_cycle_summary)

    parent_tools = build_registry()
    tool = RouteTool()
    parent_tools.replace("search", tool, tool.description, tool.parameters_schema())

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    await run_subagent(
        get_agent("voidx"),
        "Change the child workflow route",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert len(summaries) == 1
    assert summaries[0].has_progress is True


@pytest.mark.asyncio
async def test_subagent_applies_state_patch_before_terminal_message_result(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    observed: list[list] = []
    refreshed_routes: list[str] = []
    calls = 0

    original_builder = subagent_module.RuntimeContextBuilder

    class RecordingBuilder(original_builder):
        def build_incremental(self, cache):
            context, updated_cache = super().build_incremental(cache)
            if self.workflow_route is not None:
                refreshed_routes.append(self.workflow_route.join)
            return context, updated_cache

    monkeypatch.setattr(subagent_module, "RuntimeContextBuilder", RecordingBuilder)

    class ResultMessageTool:
        child_shareable = True
        id = "message"
        description = "Returns a terminal result with a state patch."

        def __init__(self, description=None, **_kwargs):
            if description:
                self.description = description

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(
                output="terminal result",
                metadata={
                    "message_type": "result",
                    "state_patch": ToolStatePatch(
                        plan=PlanResolution(join="review", leave="review"),
                    ).model_dump(mode="json", exclude_unset=True),
                },
            )

    monkeypatch.setattr(subagent_module, "MessageTool", ResultMessageTool)

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(list(messages))
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "message",
                    "args": {"action": "send", "message_type": "result"},
                    "id": "call-result",
                }
            ],
        )

    model = SimpleNamespace()
    model.bind_tools = lambda _tool_defs: model
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    tool = ResultMessageTool()
    parent_tools.register(
        tool.id,
        tool,
        tool.description,
        tool.parameters_schema(),
        capability=ToolCapability.ORCHESTRATION,
    )

    from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent

    result = await run_subagent(
        get_agent("voidx"),
        "Return a terminal result",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=parent_tools,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert result == "terminal result"
    assert len(observed) == 1
    assert refreshed_routes[-1] == "review"


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not registered on Windows")
@pytest.mark.asyncio
async def test_subagent_passes_approved_risk_to_bash_execution(tmp_path, monkeypatch):
    import subprocess
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (tmp_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    command = "git status --short && git diff --check -- tracked.txt"
    calls = 0

    class Model:
        def bind_tools(self, _tool_defs):
            return self

    tool_results: list[ToolMessage] = []

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "bash",
                        "args": {"command": command},
                        "id": "call-bash",
                    }
                ],
            )
        tool_results.extend(
            message
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id == "call-bash"
        )
        return AIMessage(content="status: PASS\nfiles_changed: none")

    async def approve_calls(tool_calls):
        return [
            {
                **tool_call,
                "metadata": {
                    "approved_risk": {
                        "tool_name": "bash",
                        "pattern": command,
                        "risk_level": "extreme",
                        "tags": ["dynamic_shell"],
                        "reason": "shell policy deferred: compound shell syntax",
                    }
                },
            }
            for tool_call in tool_calls
        ], []

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: Model())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    result = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Run the approved shell check",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        debug=False,
        parent_tools=build_registry(),
        authorize_tools=approve_calls,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_args: None, print=lambda *_args: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert result == "status: PASS\nfiles_changed: none"
    assert len(tool_results) == 1
    assert tool_results[0].status == "success"
    assert json.loads(tool_results[0].content)["ok"] is True
    assert calls == 2


@pytest.mark.asyncio
async def test_subagent_runner_builds_explicit_context_handoff(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module

    from voidx.agent.application.context_handoff import ChildContextHandoff
    from voidx.agent.domain.prompt_contracts import ContextSection
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.turn_context import TurnExecutionContext

    graph = _graph(tmp_path)
    goal_resolution = _child_goal_resolution()
    result_contract = _child_result_contract()
    captured: dict[str, object] = {}

    class ParentPromptPolicy:
        def profile_sections(self, _turn_context):
            return [ContextSection(name="Profile Directive", content="Use the parent profile rule.")]

    turn_context = TurnExecutionContext(
        thread_id="parent-thread",
        session_id="parent-session",
        workspace=str(tmp_path),
        runtime_profile=RuntimeProfile(
            profile_id="coding",
            revision=1,
            name="Coding",
            prompt_policy=ParentPromptPolicy(),
        ),
    )
    parent_state = SimpleNamespace(
        turn_context=turn_context,
        pending_summary=None,
        compaction_summary="",
    )
    monkeypatch.setattr(
        core_module,
        "current_thread_execution_state",
        lambda: parent_state,
    )

    async def fake_instruction_system(*, include_files=True):
        assert include_files is True
        return [
            "Instructions from: /repo/AGENTS.md\nAlways run the focused test.",
            "## Available Skills\n- private skill summary",
        ]

    async def fake_instruction_paths():
        return ["/repo/AGENTS.md"]

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])

    async def fake_run_subagent(*_args, **kwargs):
        captured.update(kwargs)
        return "child result"

    graph._instruction.system = fake_instruction_system
    graph._instruction.system_paths = fake_instruction_paths
    graph._workflow_context_for = fake_workflow_context_for
    graph._pending_summary = "Parent verified the delegated task boundary."
    graph._current_messages = [HumanMessage(content="parent private transcript")]
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Implement the feature",
        goal_resolution,
        result_contract,
    )

    handoff = captured["context_handoff"]
    assert result == "child result"
    assert isinstance(handoff, ChildContextHandoff)
    assert handoff.instructions == (
        "Instructions from: /repo/AGENTS.md\nAlways run the focused test.",
    )
    assert handoff.profile_sections == (
        ContextSection(name="Profile Directive", content="Use the parent profile rule."),
    )
    assert handoff.summary == "Parent verified the delegated task boundary."
    assert handoff.source_paths == ("/repo/AGENTS.md",)
    assert "parent private transcript" not in repr(handoff)
    assert "context_handoff" in captured


@pytest.mark.asyncio
async def test_subagent_runner_passes_profile_scoped_registry_to_child(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.execution as core_module
    from voidx.agent.adapters.langgraph.runtime.thread_context import ThreadExecutionState

    graph = _graph(tmp_path)
    profile_registry = build_registry().child_copy({"read"})
    state = ThreadExecutionState(tool_registry=profile_registry)
    captured: dict[str, object] = {}

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])

    async def fake_run_subagent(*_args, **kwargs):
        captured.update(kwargs)
        return "child result"

    graph._workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "current_thread_execution_state", lambda: state)
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Inspect the profile registry",
        _child_goal_resolution("inspect", join="review", leave="review"),
        _child_result_contract("inspection_result"),
    )

    assert result == "child result"
    assert captured["parent_tools"] is profile_registry


@pytest.mark.asyncio
async def test_run_subagent_passes_parent_process_sandbox_to_scoped_binder(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    class Model:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        return AIMessage(content="done")

    bound: dict[str, object] = {}

    def scoped_binder(_registry, **kwargs):
        bound.update(kwargs)

    sandbox = object()
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: Model())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    result = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Run with the parent sandbox",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_child_goal_resolution(),
        result_contract=_child_result_contract(),
        parent_tools=build_registry(),
        process_sandbox=sandbox,
        scoped_tools_binder=scoped_binder,
        debug=False,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_a: None, print=lambda *_a: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert result == "done"
    assert bound["process_sandbox"] is sandbox


@pytest.mark.asyncio
async def test_implement_child_does_not_expose_external_write_capabilities(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    class Model:
        def __init__(self):
            self.tool_names: list[str] = []

        def bind_tools(self, tool_defs):
            self.tool_names = [
                item["function"]["name"]
                for item in tool_defs
                if isinstance(item, dict) and "function" in item
            ]
            return self

    model = Model()

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    result = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Inspect the child external capability boundary",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_child_goal_resolution(join="tdd", leave="tdd"),
        result_contract=_child_result_contract(),
        parent_tools=build_registry(),
        debug=False,
        ui_port=SimpleNamespace(
            ui=SimpleNamespace(step_header=lambda *_a: None, print=lambda *_a: None),
            console=object(),
            via_events=lambda: False,
        ),
    )

    assert result == "done"
    assert "skill" not in model.tool_names
    assert "mcp" not in model.tool_names
