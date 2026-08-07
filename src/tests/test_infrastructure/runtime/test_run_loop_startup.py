"""Tests for run loop startup, clear, resume, and cancel."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.persistence.sqlite as store


from voidx.agent.slash import SlashHandler
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.infrastructure.presentation_adapter import LangGraphRuntimeStatusReader
from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import PreflightCompactionResult
from voidx.agent.application.coding_service import CODING_PROFILE, CodingService
from voidx.agent.application.agent_service import AgentService
from voidx.agent.infrastructure.langgraph.execution import _sanitize_generated_title
from voidx.agent.application.runtime_context import InteractionMode, TaskIntent
from voidx.agent.domain.task.state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config, ModelConfig
from voidx.llm.usage import UsageStats
from voidx.agent.adapters.persistence.runtime_state_repository import RuntimeStateSnapshot, save_runtime_state
from voidx.agent.adapters.persistence.session_repository import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.selfupdate import UpdateCheckResult
from voidx.agent.application.automation.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, ui_events
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.presentation.gateway.command_handler import GatewayCommandHandler
from voidx.presentation.protocol import UiCancelCommand, UiSubmitCommand
from voidx.presentation.terminal.startup import StartupPresenter
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui()
from tests.test_infrastructure.runtime.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _graph_and_run_loop,
    _service,
    _service_and_run_loop,
    _disable_external_managers,
)

@pytest.mark.asyncio
async def test_startup_update_check_appends_update_notice(tmp_path, monkeypatch):
    from voidx.config import Settings

    graph = _graph(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    graph.test_host.settings = settings
    messages: list[tuple[str, bool]] = []
    graph.test_host.ui = SimpleNamespace(
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

    await StartupPresenter(
        LangGraphRuntimeStatusReader(graph.test_host),
        graph.test_host.ui,
        restore_snapshot=graph.test_host.restore_transcript_snapshot,
        update_check_due=settings.update_check_due,
        mark_update_check=settings.mark_update_check,
    ).show_update_check_if_needed()

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
    graph.test_host.settings = settings
    messages: list[str] = []
    graph.test_host.ui = SimpleNamespace(
        dock=SimpleNamespace(
            append_message=lambda text, *, markup=False: messages.append(text),
        ),
    )

    async def fail_check_for_update():
        raise AssertionError("check_for_update should not run before TTL expires")

    monkeypatch.setattr("voidx.selfupdate.check_for_update", fail_check_for_update)

    await StartupPresenter(
        LangGraphRuntimeStatusReader(graph.test_host),
        graph.test_host.ui,
        restore_snapshot=graph.test_host.restore_transcript_snapshot,
        update_check_due=settings.update_check_due,
        mark_update_check=settings.mark_update_check,
    ).show_update_check_if_needed()

    assert messages == []


@pytest.mark.asyncio
async def test_quiet_slash_command_dispatches_without_turn(monkeypatch):
    FakeTui.instances = []
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", FakeTui)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    graph = _graph()

    dispatched: list[str] = []

    async def fake_dispatch(self, command: str) -> bool:
        dispatched.append(command)
        return True

    graph.test_host.slash = SimpleNamespace(dispatch=MethodType(fake_dispatch, graph.test_host))

    await graph.run()

    assert dispatched == ["/model reasoning"]


@pytest.mark.asyncio
async def test_run_loop_default_context_includes_workspace(monkeypatch, tmp_path):
    workspace = str(tmp_path)

    class SubmitTui:
        def __init__(self, status, commands):
            self.status = status
            self.commands = commands
            self.command_handler = None

        async def run(self, on_submit):
            keep_running = await on_submit("hello from tui")
            assert keep_running is True

        def set_external_command_handler(self, handler):
            self.command_handler = handler

    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", SubmitTui)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    graph = _graph(workspace=workspace)
    _disable_external_managers(graph)

    class FakeRuntime:
        def __init__(self):
            self.requests = []

        async def run_turn(self, request):
            self.requests.append(request)

    runtime = FakeRuntime()
    graph.test_router._coding_service = CodingService(runtime)

    await graph.run()

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.context.thread_id == "coding"
    assert request.context.session_id == ""
    assert request.context.workspace == workspace


@pytest.mark.asyncio
async def test_run_loop_status_goal_label_reflects_task_state_current_goal(monkeypatch, tmp_path):
    captured = {}

    class CaptureTui:
        def __init__(self, status, commands):
            self.status = status
            self.commands = commands
            captured["status"] = status

        async def run(self, on_submit):
            return False

        def set_external_command_handler(self, handler):
            pass

    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", CaptureTui)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    graph = _graph(workspace=str(tmp_path))
    _disable_external_managers(graph)
    graph.test_host.task_state.set_goal("实现自动重试机制")

    await graph.run()

    assert captured["status"].goal_label() == "实现自动重试机制"

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

    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", fail_create_frontend)
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.GatewayHeadlessFrontend", ExitHeadlessFrontend)
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.emit_web_gateway_bootstrap", lambda _url: None)

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
async def test_web_non_headless_falls_back_to_gateway_frontend_when_tui_unavailable(monkeypatch, tmp_path):
    """--web without --web-headless should fall back to GatewayHeadlessFrontend
    when voidx_cli is not installed, instead of crashing."""
    graph = _graph(workspace=str(tmp_path))
    _disable_external_managers(graph)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    class ExitHeadlessFrontend:
        instances = []

        def __init__(self, status, commands):
            self.status = status
            self.commands = commands
            self.request_handler = None
            ExitHeadlessFrontend.instances.append(self)

        async def run(self, on_submit):
            return

        async def run_headless(self, on_submit):
            return

        def set_external_command_handler(self, handler):
            self.command_handler = handler

        def set_external_request_handler(self, handler):
            self.request_handler = handler

    def fail_create_frontend(*_args, **_kwargs):
        raise RuntimeError("voidx_cli is required for terminal UI mode.")

    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", fail_create_frontend)
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.GatewayHeadlessFrontend", ExitHeadlessFrontend)
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.emit_web_gateway_bootstrap", lambda _url: None)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run(web=True, web_headless=False)
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert len(ExitHeadlessFrontend.instances) == 1
    assert ExitHeadlessFrontend.instances[0].request_handler is not None


@pytest.mark.asyncio
async def test_non_web_create_frontend_failure_exits_with_error(monkeypatch, tmp_path):
    """Non --web mode: create_frontend failure must print error and exit,
    not silently fall back to GatewayHeadlessFrontend (which would hang
    forever without a gateway server)."""
    from voidx.agent.application.agent_service import RunLoopStartupError

    graph = _graph(workspace=str(tmp_path))
    _disable_external_managers(graph)
    monkeypatch.setattr(runtime_ui_port, "show_startup", lambda **_: None)

    headless_instances = []

    class HeadlessFrontend:
        def __init__(self, status, commands):
            headless_instances.append(self)

        async def run(self, on_submit):
            raise AssertionError("GatewayHeadlessFrontend.run must not be called in non-web mode")

        async def run_headless(self, on_submit):
            raise AssertionError("GatewayHeadlessFrontend.run_headless must not be called in non-web mode")

        def set_external_command_handler(self, handler):
            pass

        def set_external_request_handler(self, handler):
            pass

    def fail_create_frontend(*_args, **_kwargs):
        raise RuntimeError("voidx_cli is required for terminal UI mode.")

    monkeypatch.setattr("voidx.presentation.terminal.run_loop.create_frontend", fail_create_frontend)
    monkeypatch.setattr("voidx.presentation.terminal.run_loop.GatewayHeadlessFrontend", HeadlessFrontend)

    messages: list[str] = []

    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        original_append = test_dock.append_message

        def capture_append(text, *, markup=False):
            messages.append(text)
            original_append(text, markup=markup)

        test_dock.append_message = capture_append  # type: ignore[method-assign]
        with pytest.raises(RunLoopStartupError) as exc_info:
            await graph.run(web=False)

        assert not test_dock.active, "run loop must deactivate dock on startup failure"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert headless_instances == [], "must not create GatewayHeadlessFrontend in non-web mode"
    assert str(exc_info.value) == "Cannot start terminal UI: voidx_cli is required for terminal UI mode."
    assert messages == [], f"run loop must not also print startup error; got: {messages}"


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

    monkeypatch.setattr("voidx.agent.infrastructure.langgraph.execution.create_chat_model", fake_create_chat_model)

    settings = await Settings.create(str(tmp_path))
    await settings.save_profile(
        Profile(
            name="deepseek/deepseek-v4-pro",
            api_key="sk-deepseek",
            base_url="https://api.deepseek.com",
            protocol="openai",
        )
    )
    graph = make_langgraph_execution(
        Config(workspace=str(tmp_path), model=ModelConfig(provider="openai", model="gpt-4.1")),
        api_key="sk-openai",
        settings=settings,
    )

    frontend = SimpleNamespace(
        status=SimpleNamespace(
            provider="openai",
            model="gpt-4.1",
            context_limit=0,
            reasoning_effort="xhigh",
        )
    )
    graph._ui.bind_frontend(frontend)

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
    assert frontend.status.provider == "deepseek"
    assert frontend.status.model == "deepseek-v4-pro"



@pytest.mark.asyncio
async def test_web_submit_context_defaults_to_coding_identity(tmp_path):
    workspace = str(tmp_path)
    graph = _graph(workspace=workspace)
    queued_inputs: list[tuple[str, TurnExecutionContext]] = []

    app = SimpleNamespace(
        submit_external_input=lambda text, *, context: queued_inputs.append((text, context)),
        cancel_external_input=lambda **_: None,
    )

    await graph._handle_web_command(app, UiSubmitCommand(text="hello from web"))

    assert len(queued_inputs) == 1
    text, context = queued_inputs[0]
    assert text == "hello from web"
    assert context.thread_id == "coding"
    assert context.session_id == ""
    assert context.workspace == workspace

@pytest.mark.asyncio
async def test_web_guide_submit_records_guidance_without_starting_turn():
    graph = _graph()
    guidance: list[tuple[str, dict[str, str]]] = []
    queued_inputs: list[str] = []

    graph.test_host.submit_guidance = lambda text, **kwargs: guidance.append((text, kwargs)) or True
    app = SimpleNamespace(
        submit_external_input=queued_inputs.append,
        cancel_external_input=lambda: None,
    )

    await graph._handle_web_command(app, UiSubmitCommand(text="/guide use TypeScript"))

    assert guidance == [("use TypeScript", {"source": "user"})]
    assert queued_inputs == []


@pytest.mark.asyncio
async def test_web_direct_guide_command_records_guidance():
    graph = _graph()
    guidance: list[tuple[str, dict[str, str]]] = []
    queued_inputs: list[str] = []

    graph.test_host.submit_guidance = lambda text, **kwargs: guidance.append((text, kwargs)) or True
    app = SimpleNamespace(
        submit_external_input=queued_inputs.append,
        cancel_external_input=lambda: None,
    )

    await graph._handle_web_command(app, {"kind": "guide", "text": "stay narrow"})

    assert guidance == [("stay narrow", {"source": "user"})]
    assert queued_inputs == []


@pytest.mark.asyncio
async def test_clear_reprints_startup(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    execution = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session, ui=runtime_ui_port)
    _service_and_run_loop(execution)
    execution._interaction_mode = InteractionMode.GOAL
    execution._task_state = TaskState(current_goal=GoalSpec(desc="修复 UI"))
    restore_calls: list[bool] = []

    async def fake_restore(self, *, append: bool = False) -> bool:
        restore_calls.append(append)
        return True

    execution._restore_transcript_snapshot = MethodType(fake_restore, execution)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        test_dock.append_message("old transcript")

        await SlashHandler(execution)._clear()

        rendered = "\n".join(test_dock.tree.render(120))
        assert "voidx v" in rendered
        assert "Ask anything" in rendered
        assert "old transcript" not in rendered
        assert execution._session is None
        assert execution._session_msg_cache == []
        assert execution._interaction_mode == InteractionMode.AUTO
        assert execution._task_state.current_goal is None
        assert restore_calls == []
        if getattr(execution, "_clear_session_tasks", None):
            await asyncio.gather(*execution._clear_session_tasks)
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
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session, ui=runtime_ui_port)
    _service_and_run_loop(graph)
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
    execution = make_langgraph_execution(Config(workspace="/tmp/old-workspace"), api_key=None)
    execution._ui = runtime_ui_port
    _service(execution)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    restore_calls: list[bool] = []

    async def fake_restore(self, *, append: bool = False) -> bool:
        restore_calls.append(append)
        test_dock.append_message("restored transcript")
        return True

    execution._restore_transcript_snapshot = MethodType(fake_restore, execution)
    try:
        test_dock.append_message("old transcript")

        await SlashHandler(execution)._resume(f"/resume {session.id}")

        rendered_lines = test_dock.tree.render(120)
        rendered = "\n".join(rendered_lines)
        assert "voidx v" not in rendered
        assert restore_calls == [True]
        assert "old transcript" not in rendered
        assert "restored transcript" in rendered
        assert execution._session.id == session.id
        assert execution._workspace == str(tmp_path)
        assert execution.config.workspace == str(tmp_path)
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
    execution = make_langgraph_execution(Config(workspace="/tmp/old-workspace"), api_key=None)
    _service(execution)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await SlashHandler(execution)._resume(f"/resume {session.id}")

        assert execution._interaction_mode == InteractionMode.GOAL
        assert execution._task_state.current_intent == TaskIntent.CODING
        assert execution._task_state.current_goal is not None
        assert execution._task_state.current_goal.desc == "优化 markdown 渲染截断"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_run_turn_cancel_preserves_pending_user_message(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    execution = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session, ui=runtime_ui_port)
    execution.config = SimpleNamespace(
        workspace=str(tmp_path),
        model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
        agent=SimpleNamespace(recursion_limit=5),
    )
    execution._interaction_mode = InteractionMode.AUTO
    execution._task_state = TaskState()

    async def fake_maybe_compact(self, messages, session_messages, **_kwargs):
        return messages, None

    async def fake_preflight_compact(self, messages, session_msgs=None, **_kwargs):
        return None, PreflightCompactionResult(compacted=False)

    started = asyncio.Event()

    async def fake_astream(initial, _config, *, stream_mode="values"):
        if False:
            yield
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    execution._maybe_compact = MethodType(fake_maybe_compact, execution)
    execution._preflight_compact_if_needed = MethodType(fake_preflight_compact, execution)
    execution.graph = SimpleNamespace(astream=fake_astream)
    execution._compaction = SimpleNamespace(prune=lambda _messages: None)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        task = asyncio.create_task(
            execution.run_turn(
                "hello world",
                context=TurnExecutionContext(
                    thread_id=execution.session_id or "coding",
                    session_id=execution.session_id or "",
                ),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        messages = await load_messages(session.id)
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == "hello world"
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
async def test_dispatch_input_passes_execution_context_to_coding_turn_runner():
    graph = _graph()
    captured: list[tuple[str, str, str]] = []

    async def fake_run_coding_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ):
        captured.append(
            (
                user_text,
                thread_id,
                context.thread_id if context is not None else "",
            )
        )

    graph.run_coding_turn = MethodType(fake_run_coding_turn, graph)

    keep_running, exit_message = await graph.dispatch_input(
        "hello from t2",
        context=TurnExecutionContext(thread_id="t2", session_id="t2", runtime_profile=CODING_PROFILE),
        thread_id="t2",
    )

    assert keep_running is True
    assert exit_message is None
    assert captured == [("hello from t2", "t2", "t2")]


@pytest.mark.asyncio
async def test_dispatch_input_delegates_coding_turn_to_coding_service():
    graph = _graph()
    captured: list[tuple[str, str, str, str, str | None, str]] = []
    graph.test_host.session_id = "session-1"

    class FakeCodingService:
        async def run_coding_turn(
            self,
            *,
            user_text,
            thread_id="",
            session_id=None,
            context=None,
            display_text=None,
            workspace="",
        ):
            captured.append(
                (
                    user_text,
                    thread_id,
                    session_id or "",
                    context.thread_id if context is not None else "",
                    display_text,
                    workspace,
                )
            )

    graph.test_router._coding_service = FakeCodingService()

    keep_running, exit_message = await graph.dispatch_input(
        "hello from t2",
        context=TurnExecutionContext(thread_id="t2", session_id="t2", runtime_profile=CODING_PROFILE),
        thread_id="t2",
    )

    assert keep_running is True
    assert exit_message is None
    assert captured == [("hello from t2", "t2", "t2", "t2", None, "/tmp/workspace")]


@pytest.mark.asyncio
async def test_dispatch_input_preserves_missing_context_for_coding_service():
    graph = _graph()
    captured: list[tuple[object, str]] = []

    class FakeCodingService:
        async def run_coding_turn(
            self,
            *,
            user_text,
            thread_id="",
            session_id=None,
            context=None,
            display_text=None,
            workspace="",
        ):
            captured.append((context, workspace))

    graph.test_router._coding_service = FakeCodingService()

    keep_running, exit_message = await graph.dispatch_input(
        "hello without context",
    )

    assert keep_running is True
    assert exit_message is None
    assert captured == [(None, "/tmp/workspace")]
