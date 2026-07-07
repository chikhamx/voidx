"""Tests for run loop startup, clear, resume, and cancel."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store


from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.compaction_coordinator import PreflightCompactionResult
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config, ModelConfig
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.selfupdate import UpdateCheckResult
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events
from voidx.ui.output.types import ThreadExecutionContext
from voidx.ui.protocol import UiCancelCommand, UiSubmitCommand
from voidx.runtime.ui_port import runtime_ui_port
from tests.test_agent.graph.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _disable_external_managers,
)

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
    monkeypatch.setattr("voidx.agent.graph.run_loop.create_frontend", FakeTui)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    graph = GraphRunLoopMixin()
    graph._session = None
    graph._workspace = "/tmp/workspace"
    graph.model = object()
    graph.config = SimpleNamespace(
        workspace=graph._workspace,
        model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
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
async def test_web_headless_uses_gateway_frontend_without_default_tui_factory(monkeypatch, tmp_path):
    graph = _graph(workspace=str(tmp_path))
    _disable_external_managers(graph)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    def fail_create_frontend(*_args, **_kwargs):
        raise AssertionError("web_headless must not create the default TUI frontend")

    class ExitHeadlessFrontend:
        instances = []

        def __init__(self, status, commands):
            self.status = status
            self.commands = commands
            self.request_handler = None
            ExitHeadlessFrontend.instances.append(self)

        async def run_headless(self, on_submit):
            return

        def set_external_command_handler(self, handler):
            self.command_handler = handler

        def set_external_request_handler(self, handler):
            self.request_handler = handler

    monkeypatch.setattr("voidx.agent.graph.run_loop.create_frontend", fail_create_frontend)
    monkeypatch.setattr("voidx.agent.graph.run_loop.GatewayHeadlessFrontend", ExitHeadlessFrontend)
    monkeypatch.setattr("voidx.agent.graph.run_loop.emit_web_gateway_bootstrap", lambda _url: None)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run(web=True, web_headless=True)
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert len(ExitHeadlessFrontend.instances) == 1
    assert ExitHeadlessFrontend.instances[0].request_handler is not None


@pytest.mark.asyncio
async def test_apply_settings_update_refreshes_live_model(monkeypatch, tmp_path):
    from voidx.config import Profile, Settings

    created_models: list[tuple[str | None, str, str, str | None]] = []

    def fake_create_chat_model(api_key, model_config):
        marker = SimpleNamespace(
            api_key=api_key,
            provider=model_config.provider,
            model=model_config.model,
            base_url=model_config.base_url,
        )
        created_models.append((api_key, model_config.provider, model_config.model, model_config.base_url))
        return marker

    monkeypatch.setattr("voidx.agent.graph.core.voidx_graph.create_chat_model", fake_create_chat_model)

    settings = await Settings.create(str(tmp_path))
    await settings.save_profile(
        Profile(
            name="deepseek/deepseek-v4-pro",
            api_key="sk-deepseek",
            base_url="https://api.deepseek.com",
            protocol="openai",
        )
    )
    graph = VoidXGraph(
        Config(workspace=str(tmp_path), model=ModelConfig(provider="openai", model="gpt-4.1")),
        api_key="sk-openai",
        settings=settings,
    )

    graph._app = SimpleNamespace(
        status=SimpleNamespace(
            provider="openai",
            model="gpt-4.1",
            context_limit=0,
            reasoning_effort="xhigh",
            permission_label=lambda: "default",
        )
    )

    await graph._apply_settings_update(settings)

    assert graph.config.workspace == str(tmp_path)
    assert graph.config.model.provider == "deepseek"
    assert graph.config.model.model == "deepseek-v4-pro"
    assert graph.config.model.base_url == "https://api.deepseek.com"
    assert graph.api_key == "sk-deepseek"
    assert graph.model.provider == "deepseek"
    assert graph.model.model == "deepseek-v4-pro"
    assert created_models[-1] == (
        "sk-deepseek",
        "deepseek",
        "deepseek-v4-pro",
        "https://api.deepseek.com",
    )
    assert graph._app.status.provider == "deepseek"
    assert graph._app.status.model == "deepseek-v4-pro"


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
    graph._task_state = TaskState(current_goal=GoalSpec(desc="修复 UI"))
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
    graph._task_state = TaskState(current_goal=GoalSpec(desc="old goal"))

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
                current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
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
        model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
        agent=SimpleNamespace(recursion_limit=5),
    )
    graph._interaction_mode = InteractionMode.AUTO
    graph._task_state = TaskState()

    async def fake_maybe_compact(self, messages, session_messages, **_kwargs):
        return messages, None

    async def fake_preflight_compact(self, messages, session_msgs=None, **_kwargs):
        return None, PreflightCompactionResult(compacted=False)

    started = asyncio.Event()

    async def fake_ainvoke(initial, _config):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    graph._maybe_compact = MethodType(fake_maybe_compact, graph)
    graph._preflight_compact_if_needed = MethodType(fake_preflight_compact, graph)
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
async def test_web_submit_preserves_thread_id_for_execution_context():
    graph = _graph()
    submitted: list[tuple[str, str]] = []

    class ContextAwareApp:
        def submit_external_input(self, text: str, *, context=None, thread_id: str = "") -> None:
            submitted.append((text, context.thread_id if context is not None else thread_id))

        def cancel_external_input(self, *, context=None, thread_id: str = "") -> None:
            pass

    await graph._handle_web_command(
        ContextAwareApp(),
        UiSubmitCommand(text="run in t2", thread_id="t2"),
    )

    assert submitted == [("run in t2", "t2")]


@pytest.mark.asyncio
async def test_web_cancel_preserves_thread_id_for_execution_context():
    graph = _graph()
    cancelled: list[str] = []

    class ContextAwareApp:
        def submit_external_input(self, text: str, *, context=None, thread_id: str = "") -> None:
            pass

        def cancel_external_input(self, *, context=None, thread_id: str = "") -> None:
            cancelled.append(context.thread_id if context is not None else thread_id)

    await graph._handle_web_command(
        ContextAwareApp(),
        UiCancelCommand(thread_id="t2"),
    )

    assert cancelled == ["t2"]


@pytest.mark.asyncio
async def test_handle_user_input_passes_execution_context_to_run_once():
    graph = _graph()
    captured: list[tuple[str, str]] = []

    async def fake_run_once(self, user_text: str, *, display_text=None, context=None):
        captured.append((user_text, context.thread_id if context is not None else ""))

    graph._run_once = MethodType(fake_run_once, graph)

    keep_running, exit_message = await graph._handle_user_input(
        SimpleNamespace(),
        "hello from t2",
        context=ThreadExecutionContext(thread_id="t2", session_id="t2"),
    )

    assert keep_running is True
    assert exit_message is None
    assert captured == [("hello from t2", "t2")]
