import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.selfupdate import UpdateCheckResult
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events
from voidx.ui.protocol import UiSubmitCommand
from voidx.runtime.ui_port import runtime_ui_port


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

    async def warm_up(self):
        return {}

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
async def test_startup_update_check_appends_update_notice(tmp_path, monkeypatch):
    from voidx.config import Settings

    graph = _graph(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    graph._settings = settings
    messages: list[tuple[str, bool]] = []
    graph._ui = SimpleNamespace(
        dock=SimpleNamespace(
            append_message=lambda text, *, markup=False: messages.append((text, markup)),
        ),
    )

    async def fake_check_for_update():
        return UpdateCheckResult(
            current_version="1.0.0",
            latest_version="9.0.0",
            update_available=True,
            message="voidx 9.0.0 is available.",
        )

    monkeypatch.setattr("voidx.selfupdate.check_for_update", fake_check_for_update)
    monkeypatch.setattr("voidx.selfupdate.upgrade_hint", lambda: "Run /upgrade now")

    await graph._show_update_check_if_needed()

    assert messages == [
        ("[yellow]Update available:[/yellow] voidx 1.0.0 -> 9.0.0. [dim]Run /upgrade now[/dim]", True)
    ]
    assert settings.get_update_check_latest_version() == "9.0.0"
    assert settings.get_update_check_last_checked_at() is not None


@pytest.mark.asyncio
async def test_startup_update_check_skips_when_ttl_not_due(tmp_path, monkeypatch):
    from voidx.config import Settings

    graph = _graph(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    settings.mark_update_check("9.0.0")
    graph._settings = settings
    messages: list[str] = []
    graph._ui = SimpleNamespace(
        dock=SimpleNamespace(
            append_message=lambda text, *, markup=False: messages.append(text),
        ),
    )

    async def fail_check_for_update():
        raise AssertionError("check_for_update should not run before TTL expires")

    monkeypatch.setattr("voidx.selfupdate.check_for_update", fail_check_for_update)

    await graph._show_update_check_if_needed()

    assert messages == []


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
    graph._task_state = TaskState(current_goal=GoalSpec(type=GoalType.DESIGN, desc="修复 UI"))
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
        assert graph._task_state.current_goal is None
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
    graph._task_state = TaskState(current_goal=GoalSpec(type=GoalType.DESIGN, desc="old goal"))

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
                current_intent=TaskIntent.CODING,
                current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
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
        assert graph._task_state.current_intent == TaskIntent.CODING
        assert graph._task_state.current_goal is not None
        assert graph._task_state.current_goal.desc == "优化 markdown 渲染截断"
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
async def test_first_turn_without_goal_uses_temporary_session_title(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "看看这个项目" in messages[1].content
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING, desc="workspace inspection"),
                goal=None,
                plan=None,
            )

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = StructuredGoalModel()
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
        assert loaded.title == "看看这个项目"
        assert graph._session.title == "看看这个项目"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_run_once_uses_general_fallback_when_structured_resolver_fails(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("structured resolver unavailable")

        async def ainvoke(self, _messages):
            raise AssertionError("resolver should fail before invoking")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("review runtime context")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    assert initial["task_state"]["current_intent"] == "general"
    assert initial["task_state"]["current_goal"] is None
    assert initial["task_state"]["recent_user_texts"] == ["review runtime context"]
    rows = await load_messages(graph._session.id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert all("GoalResolution JSON schema" not in row.content for row in rows)


@pytest.mark.asyncio
async def test_run_once_does_not_preadvance_workflow_without_resolver_join(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.DESIGN, desc="agent_name 语义清理"),
        workflow_runs={
            "brainstorm": WorkflowRunState(
                name="brainstorm",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="design",
                scope="agent_name 语义清理",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("structured resolver unavailable")

        async def ainvoke(self, _messages):
            raise AssertionError("resolver should fail before invoking")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("可以，先写一个 spec")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs["brainstorm"].status == WorkflowRunStatus.ACTIVE
    assert "design-doc" not in state.workflow_runs
    assert initial["persona"] == "coordinate"


@pytest.mark.asyncio
async def test_run_once_clears_stale_completed_workflow_when_resolver_has_no_join(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.CHORE, desc="检查检查，准备push吧"),
        workflow_runs={
            "verify": WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.SATISFIED,
                reason="transition from tdd via implemented",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" in messages[0].content
            return {
                "intent": {"type": "coding", "desc": "plain follow-up request"},
                "goal": None,
                "plan": None,
            }

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("检查检查，准备push吧")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs == {}
    assert initial["persona"] == "implement"


@pytest.mark.asyncio
async def test_run_once_preadvances_workflow_from_resolver_workflow_start(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.DESIGN, desc="agent_name 语义清理"),
        workflow_runs={
            "brainstorm": WorkflowRunState(
                name="brainstorm",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="design",
                scope="agent_name 语义清理",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" in messages[0].content
            return {
                "intent": {"type": "coding", "desc": "user requested spec"},
                "goal": {"type": "doc", "desc": "agent_name 语义清理"},
                "plan": {"join": "design-doc", "leave": "design-doc"},
            }

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("可以，先写一个 spec")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert state.workflow_runs["brainstorm"].evidence[-1].condition == "approved"
    assert state.workflow_runs["design-doc"].status == WorkflowRunStatus.ACTIVE
    assert initial["persona"] == "plan"


@pytest.mark.asyncio
async def test_run_once_activates_workflow_start_from_resolver_route(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    captured: dict[str, object] = {}
    invalidations = 0

    class FakeApp:
        def invalidate(self):
            nonlocal invalidations
            invalidations += 1

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" in messages[0].content
            return {
                "intent": {"type": "coding", "desc": "review only"},
                "goal": {"type": "review", "desc": "current diff"},
                "plan": {"join": "review", "leave": "review"},
            }

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            assert graph._task_state.current_goal is not None
            assert graph._task_state.current_goal.desc == "current diff"
            assert graph._task_state.workflow_route is not None
            assert graph._task_state.workflow_route.join == "review"
            assert graph._task_state.workflow_runs["review"].status == WorkflowRunStatus.ACTIVE
            assert invalidations > 0
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    graph._app = FakeApp()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("review 一下这个")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_route is not None
    assert state.workflow_route.join == "review"
    assert state.workflow_route.leave == "review"
    assert state.workflow_runs["review"].status == WorkflowRunStatus.ACTIVE
    assert state.workflow_runs["review"].reason == "resolver plan.join"
    assert initial["persona"] == "review"


@pytest.mark.asyncio
async def test_run_once_overrides_stale_brainstorm_when_resolver_requests_tdd(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.DESIGN, desc="LSP 工具合并"),
        workflow_runs={
            "brainstorm": WorkflowRunState(
                name="brainstorm",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="design",
                scope="LSP 工具合并",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" in messages[0].content
            return {
                "intent": {"type": "coding", "desc": "user explicitly requested implementation"},
                "goal": {"type": "feature", "desc": "LSP 工具合并"},
                "plan": {"join": "tdd", "leave": "verify"},
            }

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("开干")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert state.workflow_runs["brainstorm"].evidence[-1].condition == "superseded_by_intent"
    assert state.workflow_runs["tdd"].status == WorkflowRunStatus.ACTIVE
    assert initial["persona"] == "implement"


@pytest.mark.asyncio
async def test_run_once_uses_user_text_for_first_session_title_without_resolver_goal(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, _messages):
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING, desc="review request"),
                goal=None,
                plan=None,
            )

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("review runtime")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert graph._session is not None
    assert graph._session.title == "review runtime"


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
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.set_session_title("Manual title")

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Manual title"


@pytest.mark.asyncio
async def test_smart_title_does_not_update_after_clear(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.clear_current_session()
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
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.resume_session(resumed)

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
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await update_title(session.id, "Manual title")

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
    assert graph._title_task is None

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "first user request"
    assert prompts == []


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
async def test_run_loop_lsp_startup_warms_servers(monkeypatch, tmp_path):
    class YieldingExitTui(ExitTui):
        async def run(self, on_submit):
            await asyncio.sleep(0.01)

    monkeypatch.setattr("voidx.agent.graph.run_loop.PureTui", YieldingExitTui)

    class WarmupLspManager:
        initialized = False
        initializing = False

        def __init__(self) -> None:
            self.warmed = False
            self.stopped = False

        async def initialize(self):
            self.initialized = True

        def doctor(self):
            return [
                SimpleNamespace(
                    language="python",
                    enabled=True,
                    available=True,
                    resolved_path="/usr/bin/pyright-langserver",
                    detected_source="PATH",
                )
            ]

        async def warm_up(self):
            self.warmed = True
            return {"python": "ok"}

        async def stop_all(self):
            self.stopped = True

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._mcp_manager = NoopMcpManager()
    manager = WarmupLspManager()
    graph._lsp_manager = manager
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        await graph.run()

        assert manager.warmed is True
        assert manager.stopped is True
        rendered = "\n".join(test_dock.tree.render(120))
        assert "warming..." in rendered
        assert "ready" in rendered
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


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
async def test_prepare_includes_restored_workflow_runs(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    restored = WorkflowRunState(
        name="brainstorm",
        status=WorkflowRunStatus.ACTIVE,
        source=WorkflowActivationSource.WORKFLOW,
        reason="resume",
        goal_type="design",
        scope="resume optimization",
    )
    graph._task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(type=GoalType.DESIGN, desc="resume optimization"),
        workflow_runs={"brainstorm": restored},
    )

    state = {
        "messages": [HumanMessage(content="continue")],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "interaction_mode": "auto",
        "intent_resolution_reason": "resume",
        "task_state": graph._task_state.model_dump(mode="json"),
        "step_count": 0,
        "max_steps": 50,
        "tool_results": {},
        "should_continue": True,
    }

    result = await graph._prepare_with_stream(state)

    result_task_state = TaskState.model_validate(result["task_state"])
    assert list(result_task_state.workflow_runs.values()) == [restored]
    assert "## Workflow Node: brainstorm" in state["messages"][1].content
    assert "Present a design and get user approval before writing any code." in state["messages"][1].content
    assert (
        "Workflow run state: brainstorm=active "
        "goal_type=design source=workflow reason=resume"
    ) in state["messages"][-1].content


def test_resolve_recursion_limit_uses_graph_safety_default():
    from voidx.agent.graph.turn_mixin import _resolve_recursion_limit

    assert _resolve_recursion_limit() == 500


def test_resolve_recursion_limit_ignores_legacy_agent_fields():
    from voidx.agent.graph.turn_mixin import _resolve_recursion_limit

    class LegacySteps:
        voidx = 500
        implement = 200
        recursion_limit = 300

    assert _resolve_recursion_limit(LegacySteps(), "implement") == 500


def test_main_agent_has_no_static_max_steps():
    from voidx.agent.agents import get_agent
    from voidx.agent.graph.topology import prepare_state

    agent = get_agent("voidx")
    assert agent is not None
    assert not hasattr(agent, "max_steps")
    assert "Max steps" not in agent.tool_contract

    prepared = prepare_state({
        "messages": [],
        "workspace": ".",
        "persona": "coordinate",
        "plan_mode": False,
        "interaction_mode": "auto",
        "tool_results": {},
        "step_count": 0,
        "should_continue": True,
    })

    assert prepared == {"step_count": 1}
