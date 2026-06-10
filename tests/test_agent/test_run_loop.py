import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import PendingApproval, TaskPhase, TaskRun, TaskRunStatus, TaskState
from voidx.config import Config
from voidx.llm.instruction import SkillRuntimeContext
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.skills.runtime import SkillActivationSource, SkillRunState, SkillRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events
from voidx.ui.protocol import UiSubmitCommand
from voidx.runtime.ui_port import runtime_ui_port


class FakeTui:
    instances = []

    def __init__(self, status, commands):
        self.status = status
        self.commands = commands
        FakeTui.instances.append(self)

    async def run(self, on_submit):
        keep_running = await on_submit("/model reasoning")
        assert keep_running is True

    def set_external_command_handler(self, handler):
        self.command_handler = handler

    def consume_quiet_command(self, command: str) -> bool:
        return command == "/model reasoning"


class ExitTui:
    def __init__(self, status, commands):
        self.status = status
        self.commands = commands
        self.command_handler = None

    async def run(self, on_submit):
        return

    def set_external_command_handler(self, handler):
        self.command_handler = handler


class NoopMcpManager:
    def statuses(self):
        return []

    async def start_all(self):
        return None

    async def stop_all(self):
        return None


class NoopLspManager:
    initialized = True
    initializing = False

    async def initialize(self):
        return None

    def doctor(self):
        return []

    async def stop_all(self):
        return None


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
    graph._ui = runtime_ui_port
    return graph


def _disable_external_managers(graph) -> None:
    graph._mcp_manager = NoopMcpManager()
    graph._lsp_manager = NoopLspManager()


@pytest.mark.asyncio
async def test_quiet_slash_command_dispatches_without_turn(monkeypatch):
    FakeTui.instances = []
    monkeypatch.setattr("voidx.agent.graph.run_loop.PureTui", FakeTui)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

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
    graph._ui = runtime_ui_port

    dispatched: list[str] = []

    async def fake_dispatch(self, command: str) -> bool:
        dispatched.append(command)
        return True

    graph._dispatch_slash = MethodType(fake_dispatch, graph)

    await graph.run()

    assert dispatched == ["/model reasoning"]


@pytest.mark.asyncio
async def test_web_guide_submit_records_guidance_without_starting_turn():
    graph = _graph()
    guidance: list[str] = []
    queued_inputs: list[str] = []

    graph.submit_guidance = lambda text: guidance.append(text) or True
    app = SimpleNamespace(
        submit_external_input=queued_inputs.append,
        cancel_external_input=lambda: None,
    )

    await graph._handle_web_command(app, UiSubmitCommand(text="/guide use TypeScript"))

    assert guidance == ["use TypeScript"]
    assert queued_inputs == []


@pytest.mark.asyncio
async def test_web_direct_guide_command_records_guidance():
    graph = _graph()
    guidance: list[str] = []
    queued_inputs: list[str] = []

    graph.submit_guidance = lambda text: guidance.append(text) or True
    app = SimpleNamespace(
        submit_external_input=queued_inputs.append,
        cancel_external_input=lambda: None,
    )

    await graph._handle_web_command(app, {"kind": "guide", "text": "stay narrow"})

    assert guidance == ["stay narrow"]
    assert queued_inputs == []


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
    restore_calls: list[bool] = []

    async def fake_restore(self, *, append: bool = False) -> bool:
        restore_calls.append(append)
        return True

    graph._restore_transcript_snapshot = MethodType(fake_restore, graph)
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
        assert graph._session is None
        assert graph._session_msg_cache == []
        assert graph._interaction_mode == InteractionMode.AUTO
        assert graph._task_state.current_goal == ""
        assert graph._task_run.status == TaskRunStatus.IDLE
        assert restore_calls == []
        if getattr(graph, "_clear_session_tasks", None):
            await asyncio.gather(*graph._clear_session_tasks)
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_clear_detaches_old_session_and_cleans_storage_in_background(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._interaction_mode = InteractionMode.GOAL
    graph._task_state = TaskState(current_goal="old goal")
    graph._task_run = TaskRun(goal="old goal", phase=TaskPhase.DESIGN, status=TaskRunStatus.ACTIVE)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await SlashHandler(graph)._clear()
        new_session = await create_session(
            workspace=str(tmp_path),
            provider="mimo",
            model="mimo-v2.5",
        )
        await save_message(MessageRow(session_id=new_session.id, role="user", content="new question"))
        await asyncio.gather(*graph._clear_session_tasks)

        old_session = await get_session(session.id)
        assert graph._session is None
        assert old_session is not None
        assert old_session.title == "New session"
        assert old_session.updated_at == session.updated_at
        assert await load_messages(session.id) == []
        new_messages = await load_messages(new_session.id)
        assert [message.content for message in new_messages] == ["new question"]
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_show_startup_prefer_direct_skips_event_request(tmp_path, monkeypatch):
    graph = _graph(session=None, workspace=str(tmp_path))
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(DockEventConsumer(test_dock))

    async def fail_request(_event):
        raise AssertionError("prefer_direct should not call ui_events.request")

    monkeypatch.setattr(ui_events, "request", fail_request)
    try:
        await graph._show_startup(prefer_direct=True)

        rendered = "\n".join(test_dock.tree.render(120))
        assert "voidx v" in rendered
        assert "Ask anything" in rendered
    finally:
        if ui_events.is_running:
            await ui_events.stop()
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
async def test_smart_title_generation_updates_matching_session(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class FakeTitleModel:
        async def ainvoke(self, messages):
            assert messages[0].content.startswith("You are voidx title agent")
            assert "看看这个项目" in messages[1].content
            return AIMessage(content='"项目结构分析"')

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = FakeTitleModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("看看这个项目")
        task = graph._title_task
        if task is not None:
            await task

        assert graph._session is not None
        loaded = await get_session(graph._session.id)
        assert loaded is not None
        assert loaded.title == "项目结构分析"
        assert graph._session.title == "项目结构分析"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_smart_title_generation_failure_keeps_temporary_title(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class FailingTitleModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("title failed")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = FailingTitleModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("分析一下启动流程")
        task = graph._title_task
        if task is not None:
            await task

        assert graph._session is not None
        loaded = await get_session(graph._session.id)
        assert loaded is not None
        assert loaded.title == "分析一下启动流程"
        assert graph._session.title == "分析一下启动流程"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_smart_title_does_not_override_manual_title(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTitleModel:
        async def ainvoke(self, _messages):
            started.set()
            await release.wait()
            return AIMessage(content="Generated")

    graph.model = SlowTitleModel()
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    task = graph._title_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await graph.set_session_title("Manual title")
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Manual title"


@pytest.mark.asyncio
async def test_smart_title_does_not_update_after_clear(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTitleModel:
        async def ainvoke(self, _messages):
            started.set()
            await release.wait()
            return AIMessage(content="Generated")

    graph.model = SlowTitleModel()
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    task = graph._title_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await graph.clear_current_session()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if graph._clear_session_tasks:
        await asyncio.gather(*graph._clear_session_tasks)

    loaded = await get_session(session.id)
    assert graph._session is None
    assert loaded is not None
    assert loaded.title == "New session"


@pytest.mark.asyncio
async def test_smart_title_does_not_update_resumed_session(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    resumed = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(resumed.id, "Resumed title")
    resumed = resumed.model_copy(update={"title": "Resumed title"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTitleModel:
        async def ainvoke(self, _messages):
            started.set()
            await release.wait()
            return AIMessage(content="Generated")

    graph.model = SlowTitleModel()
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    task = graph._title_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await graph.resume_session(resumed)
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    loaded_old = await get_session(session.id)
    loaded_resumed = await get_session(resumed.id)
    assert graph._session is not None
    assert graph._session.id == resumed.id
    assert loaded_old is not None
    assert loaded_old.title == "temporary"
    assert loaded_resumed is not None
    assert loaded_resumed.title == "Resumed title"


@pytest.mark.asyncio
async def test_smart_title_requires_database_title_to_remain_temporary(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTitleModel:
        async def ainvoke(self, _messages):
            started.set()
            await release.wait()
            return AIMessage(content="Generated")

    graph.model = SlowTitleModel()
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    task = graph._title_task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await update_title(session.id, "Manual title")
    release.set()
    await task

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Manual title"


@pytest.mark.asyncio
async def test_title_auto_uses_first_user_message(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=session.id, role="user", content="first user request"))
    await save_message(MessageRow(session_id=session.id, role="assistant", content="response"))
    await save_message(MessageRow(session_id=session.id, role="user", content="second user request"))
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    prompts: list[str] = []

    class FakeTitleModel:
        async def ainvoke(self, messages):
            prompts.append(messages[1].content)
            return AIMessage(content="First request title")

    graph.model = FakeTitleModel()

    assert await graph.regenerate_session_title() is True
    task = graph._title_task
    assert task is not None
    await task

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "First request title"
    assert prompts == ["First user message:\n\nfirst user request"]


def test_sanitize_generated_title_rejects_markdown():
    assert _sanitize_generated_title("**Bold title**") == ""
    assert _sanitize_generated_title("# Heading title") == ""
    assert _sanitize_generated_title("`code title`") == ""
    assert _sanitize_generated_title("[Title](https://example.com)") == ""
    assert _sanitize_generated_title("Fix login-flow bug") == "Fix login-flow bug"


@pytest.mark.asyncio
async def test_delete_empty_current_session_only_deletes_sessions_without_messages(tmp_path):
    empty = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=empty)

    await graph._delete_empty_current_session()

    assert await get_session(empty.id) is None
    assert graph._session is None

    non_empty = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=non_empty.id, role="user", content="hello"))
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=non_empty)

    await graph._delete_empty_current_session()

    assert await get_session(non_empty.id) is not None
    assert graph._session is not None


@pytest.mark.asyncio
async def test_exit_cleanup_deletes_empty_current_session(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.agent.graph.run_loop.PureTui", ExitTui)
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    _disable_external_managers(graph)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        await graph.run()
    finally:
        test_dock.reset()
        set_dock(None)

    assert await get_session(session.id) is None
    assert graph._session is None


@pytest.mark.asyncio
async def test_exit_cleanup_keeps_session_with_messages_even_new_session_title(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.agent.graph.run_loop.PureTui", ExitTui)
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
    await update_title(session.id, "New session")
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    _disable_external_managers(graph)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        await graph.run()
    finally:
        test_dock.reset()
        set_dock(None)

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "New session"
    assert loaded.message_count == 1
    assert graph._session is not None
    assert graph._session.id == session.id


@pytest.mark.asyncio
async def test_run_loop_cancels_lsp_startup_tasks_on_exit(monkeypatch, tmp_path):
    class YieldingExitTui(ExitTui):
        async def run(self, on_submit):
            await asyncio.sleep(0)

    monkeypatch.setattr("voidx.agent.graph.run_loop.PureTui", YieldingExitTui)

    class HangingLspManager:
        initialized = False
        initializing = True

        def __init__(self) -> None:
            self.cancelled = False
            self.stopped = False

        async def initialize(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        def doctor(self):
            return []

        async def stop_all(self):
            self.stopped = True

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._mcp_manager = NoopMcpManager()
    manager = HangingLspManager()
    graph._lsp_manager = manager

    await graph.run()

    assert manager.cancelled is True
    assert manager.stopped is True


@pytest.mark.asyncio
async def test_run_once_clears_unconsumed_guidance(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        assert graph.submit_guidance("Use TypeScript")

        await graph._run_once("hello world")

        rendered = "\n".join(test_dock.tree.render(120))
        assert graph._pending_guidance == []
        assert "Guidance discarded: no LLM call to inject into." in rendered
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
async def test_turn_mixin_delegates_run_once_to_component():
    from voidx.agent.graph.turn_mixin import GraphTurnMixin

    class FakeTurnRunner:
        def __init__(self):
            self.calls = []

        async def run_once(self, user_text: str, *, display_text: str | None = None) -> None:
            self.calls.append((user_text, display_text))

    runner = FakeTurnRunner()
    host = SimpleNamespace(_turn_runner=runner)

    await GraphTurnMixin._run_once(host, "raw input", display_text="display input")

    assert runner.calls == [("raw input", "display input")]


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
