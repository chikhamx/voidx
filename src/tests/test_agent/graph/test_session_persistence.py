"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.memory.store as store

from voidx.agent.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
    get_visible_agents,
)
from voidx.agent.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.infrastructure.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.infrastructure.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, ParallelSubagentsConfig, Settings, UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.llm.instruction import InstructionService, WorkflowRuntimeContext
from voidx.memory.session import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.memory.transcript import load_transcript
from voidx.permission.service import PermissionService
from voidx.runtime import GoalResolution, GoalSpec, IntentResolution, PlanResolution, TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.runtime.task_state import TaskState, ToolStatePatch, WorkflowRoute
from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.agent import AgentResultContract, AgentTool
from voidx.tools.registry import ToolRegistry
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return LangGraphExecution(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


def _child_goal_resolution(
    goal_type: str = "feature",
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc=desc),
        plan=PlanResolution(join=join, leave=leave),
    )


def _child_result_contract(schema_name: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if schema_name == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(
        schema_name=schema_name,
        format=result_format,
    )


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    schema_name: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(schema_name),
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


def test_execute_tools_router_honors_should_continue_false():
    from voidx.agent.infrastructure.langgraph.runtime.topology import route_after_execute_tools

    assert route_after_execute_tools({"should_continue": False}) == "end"
    assert route_after_execute_tools({"should_continue": True}) == "call_llm"
    assert route_after_execute_tools({}) == "call_llm"


@pytest.mark.asyncio
async def test_session_persistence_saves_only_new_ai_and_tool_messages(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))

        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("new question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        assistant_contents = [row.content for row in rows if row.role == "assistant"]
        assert assistant_contents.count("old answer") == 1
        assert assistant_contents.count("new answer") == 1
    finally:
        await delete_session(session.id)




@pytest.mark.asyncio
async def test_run_turn_uses_execution_context_session_id_for_persistence(tmp_path):
    from voidx.ui.output.types import ThreadExecutionContext

    active = await create_session(workspace=str(tmp_path), title="Active")
    target = await create_session(workspace=str(tmp_path), title="Target")
    try:
        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=active)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="target answer")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn(
                "target question",
                context=ThreadExecutionContext(thread_id=target.id, session_id=target.id),
            )
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        active_rows = await load_messages(active.id)
        target_rows = await load_messages(target.id)
        assert [row.content for row in active_rows] == []
        assert [row.content for row in target_rows if row.role == "user"] == ["target question"]
        assert [row.content for row in target_rows if row.role == "assistant"] == ["target answer"]
    finally:
        await delete_session(active.id)
        await delete_session(target.id)


@pytest.mark.asyncio
async def test_run_turn_loads_execution_context_runtime_state(tmp_path):
    from voidx.agent.runtime_context import InteractionMode
    from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
    from voidx.ui.output.types import ThreadExecutionContext

    active = await create_session(workspace=str(tmp_path), title="Active")
    target = await create_session(workspace=str(tmp_path), title="Target")
    try:
        active_state = TaskState(current_goal=GoalSpec(desc="active goal"))
        target_state = TaskState(current_goal=GoalSpec(desc="target goal"))
        await save_runtime_state(active.id, RuntimeStateSnapshot(interaction_mode=InteractionMode.GOAL, task_state=active_state))
        await save_runtime_state(target.id, RuntimeStateSnapshot(interaction_mode=InteractionMode.PLAN, task_state=target_state))

        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=active)
        await graph.restore_runtime_state()
        captured: dict[str, str] = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                state = TaskState.model_validate(initial["task_state"])
                captured["goal"] = state.current_goal.desc if state.current_goal else ""
                captured["interaction_mode"] = initial["interaction_mode"]
                return {"messages": list(initial["messages"]) + [AIMessage(content="target answer")], "task_state": initial["task_state"]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn(
                "target question",
                context=ThreadExecutionContext(thread_id=target.id, session_id=target.id),
            )
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert captured == {"goal": "target goal", "interaction_mode": "plan"}
        assert graph._session.id == active.id
        assert graph._task_state.current_goal is not None
        assert graph._task_state.current_goal.desc == "active goal"
        assert graph._interaction_mode == InteractionMode.GOAL
    finally:
        await delete_session(active.id)
        await delete_session(target.id)


@pytest.mark.asyncio
async def test_run_turn_model_enabled_first_turn_syncs_default_task_state(tmp_path):
    from voidx.agent.runtime_context import InteractionMode

    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key="test", session=None)
    graph._interaction_mode = InteractionMode.GOAL
    ready = asyncio.Event()
    proceed = asyncio.Event()
    seen: list[str | None] = []

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            ready.set()
            await asyncio.wait_for(proceed.wait(), timeout=1)
            return {"messages": list(initial["messages"]) + [AIMessage(content="first answer")], "task_state": initial["task_state"]}

    async def external_reader():
        await asyncio.wait_for(ready.wait(), timeout=1)
        seen.append(graph._task_state.current_goal.desc if graph._task_state.current_goal else None)
        proceed.set()

    graph.graph = FakeGraph()
    reader_task = asyncio.create_task(external_reader())

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("first question")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    await reader_task
    assert seen == ["first question"]


@pytest.mark.asyncio
async def test_run_turn_model_enabled_borrowed_context_does_not_leak_task_state(tmp_path):
    from voidx.agent.runtime_context import InteractionMode
    from voidx.memory.runtime_state import RuntimeStateSnapshot, load_runtime_state, save_runtime_state
    from voidx.ui.output.types import ThreadExecutionContext

    active = await create_session(workspace=str(tmp_path), title="Active")
    target = await create_session(workspace=str(tmp_path), title="Target")
    try:
        active_state = TaskState(current_goal=GoalSpec(desc="active goal"))
        target_state = TaskState(current_goal=GoalSpec(desc="target goal"))
        await save_runtime_state(active.id, RuntimeStateSnapshot(interaction_mode=InteractionMode.GOAL, task_state=active_state))
        await save_runtime_state(target.id, RuntimeStateSnapshot(interaction_mode=InteractionMode.PLAN, task_state=target_state))

        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key="test", session=active)
        await graph.restore_runtime_state()
        captured: dict[str, str] = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                state = TaskState.model_validate(initial["task_state"])
                captured["goal"] = state.current_goal.desc if state.current_goal else ""
                captured["interaction_mode"] = initial["interaction_mode"]
                updated_state = state.model_copy(update={"current_goal": GoalSpec(desc="target mutated goal")})
                return {
                    "messages": list(initial["messages"]) + [AIMessage(content="target answer")],
                    "task_state": updated_state.model_dump(mode="json"),
                }

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn(
                "target question",
                context=ThreadExecutionContext(thread_id=target.id, session_id=target.id),
            )
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert captured == {"goal": "target goal", "interaction_mode": "plan"}
        assert graph._session.id == active.id
        assert graph._task_state.current_goal is not None
        assert graph._task_state.current_goal.desc == "active goal"
        assert graph._interaction_mode == InteractionMode.GOAL

        active_snapshot = await load_runtime_state(active.id)
        target_snapshot = await load_runtime_state(target.id)
        assert active_snapshot.task_state.current_goal is not None
        assert active_snapshot.task_state.current_goal.desc == "active goal"
        assert target_snapshot.task_state.current_goal is not None
        assert target_snapshot.task_state.current_goal.desc == "target mutated goal"
    finally:
        await delete_session(active.id)
        await delete_session(target.id)
@pytest.mark.asyncio
async def test_run_turn_isolates_concurrent_execution_context_state(tmp_path):
    from voidx.agent.runtime_context import InteractionMode
    from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
    from voidx.ui.output.types import ThreadExecutionContext

    session_a = await create_session(workspace=str(tmp_path), title="Session A")
    session_b = await create_session(workspace=str(tmp_path), title="Session B")
    try:
        await save_runtime_state(
            session_a.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.PLAN,
                task_state=TaskState(current_goal=GoalSpec(desc="goal a")),
            ),
        )
        await save_runtime_state(
            session_b.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.PLAN,
                task_state=TaskState(current_goal=GoalSpec(desc="goal b")),
            ),
        )

        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session_a)
        entered: dict[str, asyncio.Event] = {
            session_a.id: asyncio.Event(),
            session_b.id: asyncio.Event(),
        }
        release = asyncio.Event()
        captured: dict[str, dict[str, object]] = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                user_text = next(
                    message.content
                    for message in initial["messages"]
                    if isinstance(message, HumanMessage)
                    and str(message.content).startswith("question ")
                )
                session_id = session_a.id if user_text == "question a" else session_b.id
                state = TaskState.model_validate(initial["task_state"])
                captured[session_id] = {
                    "goal": state.current_goal.desc if state.current_goal else "",
                    "cache_id": id(graph._context_cache),
                    "session_id": graph._session.id if graph._session else "",
                }
                entered[session_id].set()
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), timeout=1)
                await release.wait()
                return {
                    "messages": list(initial["messages"]) + [AIMessage(content=f"answer {session_id}")],
                    "task_state": initial["task_state"],
                }

        graph.graph = FakeGraph()
        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            task_a = asyncio.create_task(
                graph.run_turn(
                    "question a",
                    context=ThreadExecutionContext(thread_id=session_a.id, session_id=session_a.id),
                )
            )
            task_b = asyncio.create_task(
                graph.run_turn(
                    "question b",
                    context=ThreadExecutionContext(thread_id=session_b.id, session_id=session_b.id),
                )
            )
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), timeout=1)
            release.set()
            await asyncio.gather(task_a, task_b)
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert captured[session_a.id]["goal"] == "goal a"
        assert captured[session_b.id]["goal"] == "goal b"
        assert captured[session_a.id]["session_id"] == session_a.id
        assert captured[session_b.id]["session_id"] == session_b.id
        assert captured[session_a.id]["cache_id"] != captured[session_b.id]["cache_id"]
        rows_a = await load_messages(session_a.id)
        rows_b = await load_messages(session_b.id)
        assert [row.content for row in rows_a if row.role == "user"] == ["question a"]
        assert [row.content for row in rows_b if row.role == "user"] == ["question b"]
        assert [row.content for row in rows_a if row.role == "assistant"] == [f"answer {session_a.id}"]
        assert [row.content for row in rows_b if row.role == "assistant"] == [f"answer {session_b.id}"]
    finally:
        await delete_session(session_a.id)
        await delete_session(session_b.id)

async def test_runtime_context_overlay_not_persisted_to_user_history(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {
                    "messages": [
                        *initial["messages"],
                        HumanMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Runtime State\n- Workspace: tmp"),
                        AIMessage(content="new answer"),
                    ]
                }

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("new question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        assert [row.content for row in rows if row.role == "user"] == ["new question"]
        assert all("VOIDX_RUNTIME_CONTEXT" not in row.content for row in rows)
        assert all("Docs body" not in row.content for row in rows)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_synthetic_turn_uses_display_text_without_losing_prompt(tmp_path):
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None)
    captured: dict[str, list] = {}

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["messages"] = list(initial["messages"])
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.graph = FakeGraph()

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_synthetic_turn(
            "full initialization prompt with unique model marker",
            display_text="/init",
        )
        turn_header = test_dock.tree.root.children[0].header
        rendered = "\n".join(test_dock.tree.render(120))
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert turn_header == "[bold white]❯[/] /init"
    assert "full initialization prompt" not in rendered
    assert any(
        isinstance(message, HumanMessage)
        and message.content == "full initialization prompt with unique model marker"
        for message in captured["messages"]
    )


@pytest.mark.asyncio
async def test_run_turn_wraps_explicit_skill_refs_in_user_message(tmp_path):
    skill_dir = tmp_path / ".voidx" / "skills" / "docs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
        encoding="utf-8",
    )
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=session,
            settings=Settings(str(tmp_path)),
        )
        captured: dict[str, list] = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                captured["messages"] = list(initial["messages"])
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("Use $docs for this README")
            turn_header = test_dock.tree.root.children[0].header
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        user_message = captured["messages"][-1]
        assert isinstance(user_message, HumanMessage)
        assert user_message.content.startswith("Explicit skills requested:\n- docs: Write docs")
        assert "call skill with op='load'" in user_message.content
        assert "not the full instructions" in user_message.content
        assert "Use for this README" in user_message.content
        assert "$docs" not in user_message.content
        assert "Docs body" not in user_message.content
        assert turn_header == "[bold white]❯[/] Use $docs for this README"

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]
        assert user_rows[-1].content == user_message.content
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_persists_clipboard_image_attachment_as_structured_user_message(tmp_path):
    image_dir = tmp_path / ".voidx" / "attachments"
    image_dir.mkdir(parents=True)
    image = image_dir / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key="test-key", session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                user = initial["messages"][-1]
                assert isinstance(user.content, list)
                assert user.content[1]["type"] == "image_url"
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("describe [image-shot]")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]
        assert user_rows[-1].content_format == "structured"
        assert "image_url" in user_rows[-1].content
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_does_not_persist_compiled_overlay_to_user_history(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("hello world")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]

        assert user_rows
        assert user_rows[-1].content == "hello world"
        assert "VOIDX_RUNTIME_CONTEXT" not in user_rows[-1].content
        assert "Runtime State" not in user_rows[-1].content
        assert "Active Skills" not in user_rows[-1].content
    finally:
        await delete_session(session.id)

