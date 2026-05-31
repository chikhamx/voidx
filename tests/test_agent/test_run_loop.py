import sys
import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.graph_components.run_loop import GraphRunLoopMixin
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import TaskPhase, TaskRun, TaskRunStatus, TaskState
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import create_session, load_messages
from voidx.ui.dock import BottomInputDock, set_dock


class FakePromptTui:
    instances = []

    def __init__(self, status, commands):
        self.status = status
        self.commands = commands
        self.begun_outputs: list[str] = []
        self.hidden_outputs = 0
        FakePromptTui.instances.append(self)

    async def run(self, on_submit):
        keep_running = await on_submit("/model reasoning")
        assert keep_running is True

    def consume_quiet_command(self, command: str) -> bool:
        return command == "/model reasoning"

    def hide_command_output(self) -> None:
        self.hidden_outputs += 1

    def begin_command_output(self, title: str) -> None:
        self.begun_outputs.append(title)

    def append_command_output(self, text: str) -> None:
        return None

    def command_output_width(self) -> int:
        return 80


def _graph(session=None, workspace: str = "/tmp/workspace") -> GraphRunLoopMixin:
    graph = GraphRunLoopMixin()
    graph._session = session
    graph._workspace = workspace
    graph.model = object()
    graph.config = SimpleNamespace(
        workspace=workspace,
        model=SimpleNamespace(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
    )
    graph._settings = SimpleNamespace(list_mcp_servers=lambda: [], path=f"{workspace}/voidx.json")
    graph._permission = SimpleNamespace(
        status_label=lambda: "default",
        clear_session_permissions=lambda: None,
    )
    graph._usage_stats = UsageStats()
    graph._debug = False
    graph._plan_mode = False
    graph._tracker = SimpleNamespace(_todos=[])
    return graph


@pytest.mark.asyncio
async def test_quiet_slash_command_does_not_open_command_output(monkeypatch):
    FakePromptTui.instances = []
    monkeypatch.setattr("voidx.ui.app.PromptToolkitTui", FakePromptTui)
    monkeypatch.setattr("voidx.agent.graph_components.run_loop.show_startup", lambda **_: None)

    graph = GraphRunLoopMixin()
    graph._session = None
    graph._workspace = "/tmp/workspace"
    graph.model = object()
    graph.config = SimpleNamespace(
        workspace=graph._workspace,
        model=SimpleNamespace(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
    )
    graph._settings = SimpleNamespace(list_mcp_servers=lambda: [], path="/tmp/workspace/voidx.json")
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

    app = FakePromptTui.instances[0]
    assert dispatched == ["/model reasoning"]
    assert app.begun_outputs == []
    assert app.hidden_outputs == 2


@pytest.mark.asyncio
async def test_clear_reprints_startup_after_reset(tmp_path):
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
async def test_resume_reprints_startup_before_restored_transcript(tmp_path):
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
        startup_index = next(i for i, line in enumerate(rendered_lines) if "voidx v" in line)
        restored_index = next(i for i, line in enumerate(rendered_lines) if "restored transcript" in line)
        assert restore_calls == [True]
        assert startup_index < restored_index
        assert "old transcript" not in rendered
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
                awaiting_implementation_approval=True,
                approved_scope="优化 markdown 渲染截断",
            ),
            task_run=TaskRun(
                goal="优化 markdown 渲染截断",
                phase=TaskPhase.DESIGN,
                status=TaskRunStatus.ACTIVE,
                awaiting_implementation_approval=True,
                approved_scope="优化 markdown 渲染截断",
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
        assert graph._task_state.awaiting_implementation_approval is True
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

    async def fake_maybe_compact(self, messages, session_messages):
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
