import sys
import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import PendingApproval, TaskPhase, TaskRun, TaskRunStatus, TaskState
from voidx.config import Config
from voidx.llm.instruction import SkillRuntimeContext
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, load_messages, save_message
from voidx.skills.runtime import SkillActivationSource, SkillRunState, SkillRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock


class FakeTui:
    instances = []

    def __init__(self, status, commands):
        self.status = status
        self.commands = commands
        FakeTui.instances.append(self)

    async def run(self, on_submit):
        keep_running = await on_submit("/model reasoning")
        assert keep_running is True

    def consume_quiet_command(self, command: str) -> bool:
        return command == "/model reasoning"


def _graph(session=None, workspace: str = "/tmp/workspace") -> GraphRunLoopMixin:
    graph = GraphRunLoopMixin()
    graph._session = session
    graph._workspace = workspace
    graph.model = object()
    graph.config = SimpleNamespace(
        workspace=workspace,
        model=SimpleNamespace(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
    )
    graph._settings = SimpleNamespace(list_mcp_servers=lambda: [], path=f"{workspace}/.voidx/settings.json")
    graph._permission = SimpleNamespace(
        status_label=lambda: "default",
        clear_session_permissions=lambda: None,
    )
    graph._usage_stats = UsageStats()
    graph._debug = False
    graph._plan_mode = False
    graph._tracker = TaskTracker()
    graph._session_msg_cache = None
    return graph


@pytest.mark.asyncio
async def test_quiet_slash_command_dispatches_without_turn(monkeypatch):
    FakeTui.instances = []
    monkeypatch.setattr("voidx.ui.tui.PureTui", FakeTui)
    monkeypatch.setattr("voidx.agent.graph.run_loop.show_startup", lambda **_: None)

    graph = GraphRunLoopMixin()
    graph._session = None
    graph._workspace = "/tmp/workspace"
    graph.model = object()
    graph.config = SimpleNamespace(
        workspace=graph._workspace,
        model=SimpleNamespace(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
    )
    graph._settings = SimpleNamespace(list_mcp_servers=lambda: [], path="/tmp/workspace/.voidx/settings.json")
    graph._permission = SimpleNamespace(status_label=lambda: "default")
    graph._usage_stats = UsageStats()
    graph._debug = False
    graph._plan_mode = False

    dispatched: list[str] = []

    async def fake_dispatch(self, command: str) -> bool:
        dispatched.append(command)
        return True

    graph._dispatch_slash = MethodType(fake_dispatch, graph)

    await graph.run()

    assert dispatched == ["/model reasoning"]


@pytest.mark.asyncio
async def test_clear_reprints_startup(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    graph = _graph(session=session, workspace=str(tmp_path))
    graph._interaction_mode = InteractionMode.GOAL
    graph._task_state = TaskState(current_goal="修复 UI")
    graph._task_run = TaskRun(goal="修复 UI", phase=TaskPhase.DESIGN, status=TaskRunStatus.ACTIVE)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        test_dock.append_message("old transcript")

        await SlashHandler(graph)._clear()

        rendered = "\n".join(test_dock.tree.render(120))
        assert "voidx v" in rendered
        assert "Ask anything" in rendered
        assert "old transcript" not in rendered
        assert graph._session.title == "New session"
        assert graph._session.message_count == 0
        assert graph._interaction_mode == InteractionMode.AUTO
        assert graph._task_state.current_goal == ""
        assert graph._task_run.status == TaskRunStatus.IDLE
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_resume_does_not_reprint_startup(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    graph = _graph(session=None, workspace="/tmp/old-workspace")
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    restore_calls: list[bool] = []

    async def fake_restore(self, *, append: bool = False) -> bool:
        restore_calls.append(append)
        test_dock.append_message("restored transcript")
        return True

    graph._restore_transcript_snapshot = MethodType(fake_restore, graph)
    try:
        test_dock.append_message("old transcript")

        await SlashHandler(graph)._resume(f"/resume {session.id}")

        rendered_lines = test_dock.tree.render(120)
        rendered = "\n".join(rendered_lines)
        assert "voidx v" not in rendered
        assert restore_calls == [True]
        assert "old transcript" not in rendered
        assert "restored transcript" in rendered
        assert graph._session.id == session.id
        assert graph._workspace == str(tmp_path)
        assert graph.config.workspace == str(tmp_path)
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_resume_restores_structured_runtime_state(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    await save_runtime_state(
        session.id,
        RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(
                current_intent=TaskIntent.DESIGN,
                current_goal="优化 markdown 渲染截断",
                pending_approval=PendingApproval(scope="优化 markdown 渲染截断"),
            ),
            task_run=TaskRun(
                goal="优化 markdown 渲染截断",
                phase=TaskPhase.DESIGN,
                status=TaskRunStatus.ACTIVE,
                pending_approval=PendingApproval(scope="优化 markdown 渲染截断", created_turn=2),
                turn_count=2,
            ),
        ),
    )
    graph = _graph(session=None, workspace="/tmp/old-workspace")
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await SlashHandler(graph)._resume(f"/resume {session.id}")

        assert graph._interaction_mode == InteractionMode.GOAL
        assert graph._task_state.current_intent == TaskIntent.DESIGN
        assert graph._task_state.pending_approval is not None
        assert graph._task_run.goal == "优化 markdown 渲染截断"
        assert graph._task_run.phase == TaskPhase.DESIGN
        assert graph._task_run.turn_count == 2
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_run_once_cancel_deletes_pending_user_message(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    graph = _graph(session=session, workspace=str(tmp_path))
    graph.config = SimpleNamespace(
        workspace=str(tmp_path),
        model=SimpleNamespace(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
        agent=SimpleNamespace(recursion_limit=5),
    )
    graph._interaction_mode = InteractionMode.AUTO
    graph._task_state = TaskState()
    graph._task_run = None

    async def fake_maybe_compact(self, messages, session_messages, **_kwargs):
        return messages, None

    started = asyncio.Event()

    async def fake_ainvoke(initial, _config):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    graph._maybe_compact = MethodType(fake_maybe_compact, graph)
    graph.graph = SimpleNamespace(ainvoke=fake_ainvoke)
    graph._compaction = SimpleNamespace(prune=lambda _messages: None)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        task = asyncio.create_task(graph._run_once("hello world"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        messages = await load_messages(session.id)
        assert messages == []
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_large_uncompacted_resume_forces_compaction_without_truncating(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    for index in range(501):
        await save_message(MessageRow(
            session_id=session.id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        ))

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph.model = object()
    graph._compaction.prune = lambda _messages: None
    compact_calls: list[dict] = []
    captured: dict[str, list] = {}

    async def fake_maybe_compact(messages, session_messages, *, force=False, ask=True):
        compact_calls.append({
            "force": force,
            "ask": ask,
            "session_count": len(session_messages),
            "message_contents": [getattr(message, "content", None) for message in messages],
        })
        return None, None

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            await graph._prepare_with_stream(initial)
            captured["messages"] = list(initial["messages"])
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph._maybe_compact = fake_maybe_compact
    graph.graph = FakeGraph()

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("current request")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert compact_calls == [{
        "force": True,
        "ask": False,
        "session_count": 501,
        "message_contents": [f"message {index}" for index in range(501)] + ["current request"],
    }]
    system_content = captured["messages"][0].content
    assert "older persisted messages were omitted" not in system_content
    assert any(getattr(message, "content", None) == "message 0" for message in captured["messages"])
    assert any(getattr(message, "content", None) == "message 500" for message in captured["messages"])


@pytest.mark.asyncio
async def test_prepare_includes_restored_skill_runs(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    restored = SkillRunState(
        name="brainstorming",
        status=SkillRunStatus.ACTIVE,
        source=SkillActivationSource.WORKFLOW,
        reason="resume",
        phase="design",
        scope="resume optimization",
        activated_turn=1,
        updated_turn=2,
    )
    graph._task_run = TaskRun(skill_runs={"brainstorming": restored})

    class FakeInstruction:
        async def system(self):
            return []

        async def skill_context_for(self, *_args, **_kwargs):
            return SkillRuntimeContext(instructions=[], active=[], runs=[])

    graph._instruction = FakeInstruction()
    state = {
        "messages": [HumanMessage(content="continue")],
        "workspace": str(tmp_path),
        "agent": "orchestrator",
        "interaction_mode": "auto",
        "task_intent": "design",
        "intent_resolution_reason": "resume",
        "pending_approval": None,
        "goal": "resume optimization",
        "goal_phase": "design",
        "goal_status": "active",
        "goal_turn_count": 2,
        "available_tool_ids": [],
        "step_count": 0,
        "max_steps": 50,
        "tool_results": {},
        "should_continue": True,
    }

    result = await graph._prepare_with_stream(state)

    assert result["skill_runs"] == [restored]
    assert (
        "Skill run state: brainstorming=active "
        "phase=design source=workflow reason=resume"
    ) in state["messages"][-1].content


def test_resolve_recursion_limit_derives_minimum_from_max_steps():
    from voidx.agent.graph.turn_mixin import _resolve_recursion_limit
    from voidx.config.models import AgentMaxSteps

    # Default: orchestrator=100, recursion_limit=500 → 2*100+10=210 < 500, so 500
    steps = AgentMaxSteps()
    assert _resolve_recursion_limit(steps, "orchestrator") == 500

    # High orchestrator steps, low recursion_limit → derived minimum wins
    steps = AgentMaxSteps(orchestrator=500, recursion_limit=500)
    assert _resolve_recursion_limit(steps, "orchestrator") == 1010

    # High recursion_limit, low max_steps → configured limit wins
    steps = AgentMaxSteps(orchestrator=50, recursion_limit=1000)
    assert _resolve_recursion_limit(steps, "orchestrator") == 1000

    # Exact boundary: 2*max_steps+10 == recursion_limit
    steps = AgentMaxSteps(orchestrator=245, recursion_limit=500)
    assert _resolve_recursion_limit(steps, "orchestrator") == 500


def test_resolve_recursion_limit_uses_correct_agent_field():
    from voidx.agent.graph.turn_mixin import _resolve_recursion_limit
    from voidx.config.models import AgentMaxSteps

    steps = AgentMaxSteps(implement=200, recursion_limit=300)
    assert _resolve_recursion_limit(steps, "implement") == 410
