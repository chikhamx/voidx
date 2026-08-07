"""Shared helpers for test_run_loop split files."""

import sys
from pathlib import Path
from types import SimpleNamespace

import voidx.persistence.sqlite as store


from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.presentation_adapter import (
    LangGraphPresentationBinding, LangGraphPresentationIntegrations,
    LangGraphRuntimeStatusReader, LangGraphSessionLifecycle,
)
from voidx.agent.infrastructure.input_adapter import LangGraphInputAdapter
from voidx.agent.infrastructure.input_router import LangGraphAutonomousInputRouter
from voidx.agent.ports.presentation import NullAgentEventPublisher
from voidx.agent.ports.workspace_lock import DelegatingWorkspaceWriteLock
from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.runtime import AgentRuntime
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.domain.turn.state import TurnPhase
from voidx.agent.infrastructure.langgraph.execution import _sanitize_generated_title
from voidx.config import Config, ModelConfig
from voidx.llm.usage import UsageStats
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.domain.task.state import TaskState
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, ui_events
from voidx.presentation.protocol import UiSubmitCommand
from voidx.presentation.terminal.run_loop import TerminalRunLoop
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui(dock=BottomInputDock())


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

    def hide_command_output(self) -> None:
        return None


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


def _service(execution) -> AgentService:
    runtime = AgentRuntime(
        SimpleNamespace(
            turn_engine=LangGraphTurnEngine(execution),
            sessions=MemorySessionAdapter(),
            events=NullEventPublisher(),
        )
    )
    inputs = LangGraphInputAdapter(execution)
    guidance = SimpleNamespace(
        can_submit_guidance=lambda: callable(getattr(execution, "submit_guidance", None)),
        submit_guidance=lambda text, **kwargs: bool(getattr(execution, "submit_guidance", lambda *_a, **_k: False)(text, **kwargs)),
    )
    router = LangGraphAutonomousInputRouter(execution, runtime, NullAgentEventPublisher(), guidance, chat_service=None, coding_service=None, loop_service=None, goal_service=None)
    return AgentService(inputs, inputs, router, guidance)


def _terminal_run_loop(execution, service, ui=runtime_ui_port) -> TerminalRunLoop:
    return TerminalRunLoop(
        LangGraphRuntimeStatusReader(execution),
        LangGraphSessionLifecycle(execution),
        LangGraphPresentationIntegrations(execution),
        LangGraphPresentationBinding(execution, service._slash_dispatcher),
        service,
        service,
        DelegatingWorkspaceWriteLock(),
        ui,
    )


def _service_and_run_loop(execution) -> tuple[AgentService, TerminalRunLoop]:
    service = _service(execution)
    return service, _terminal_run_loop(execution, service)


def _graph(session=None, workspace: str = "/tmp/workspace") -> AgentService:
    execution = SimpleNamespace()
    graph = _service(execution)
    graph.test_host = execution
    graph.test_router = graph._autonomous_router
    run_loop_holder = {}
    execution.session = session
    execution.workspace = workspace
    execution.model = object()
    execution.config = SimpleNamespace(
        workspace=workspace,
        model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
    )
    execution.settings = SimpleNamespace(list_mcp_servers=lambda: [], path=f"{workspace}/.voidx/settings.json")
    execution.permission = SimpleNamespace(
        status_label=lambda: "default",
        permission_mode_label=lambda: "default",
        clear_session_permissions=lambda: None,
    )
    execution.usage_stats = UsageStats()
    execution.debug_enabled = False
    execution.plan_mode = False
    execution.interaction_mode = InteractionMode.AUTO
    execution.set_interaction_mode = lambda value: setattr(execution, "interaction_mode", value)
    execution.task_state = TaskState()
    execution.set_task_state = lambda value: setattr(execution, "task_state", value)
    execution.compaction_summary = ""
    execution.set_compaction_summary = lambda value: setattr(execution, "compaction_summary", value)
    execution.session_date = ""
    execution.set_session_date = lambda value: setattr(execution, "session_date", value)
    execution._tracker = TaskTracker()
    execution._session_msg_cache = None
    execution.ui = runtime_ui_port
    execution.mcp_manager = None
    execution.lsp_manager = None
    execution.gateway_session = None
    execution.runtime_guards = SimpleNamespace(wall_clock=None)

    async def noop(*_args, **_kwargs):
        return None

    execution.apply_settings_update = noop
    execution.restore_runtime_state = noop
    execution.restore_transcript_snapshot = noop
    execution.delete_empty_current_session = noop
    execution.run_turn = noop
    execution.runtime_snapshot = lambda: LangGraphStateMapper().runtime_from_execution(
        execution,
        turn_phase=TurnPhase.RUNNING,
    )
    execution.session_id = session.id if session is not None else ""
    execution._dispatch_slash = lambda _inp: False
    execution.bind_presentation_snapshots = lambda _adapter: None
    execution.bind_startup_presenter = lambda presenter: setattr(execution, "_startup_presenter", presenter)

    def run_loop():
        loop = run_loop_holder.get("loop")
        if loop is None:
            loop = _terminal_run_loop(execution, graph, execution._ui if hasattr(execution, "_ui") else runtime_ui_port)
            run_loop_holder["loop"] = loop
        return loop

    async def handle_web_command(app, command):
        await run_loop()._command_handler.handle(app, command)

    graph.run = lambda **kwargs: run_loop().run(**kwargs)
    graph._handle_web_command = handle_web_command
    graph._show_startup = lambda **kwargs: run_loop()._startup.show(**kwargs)
    graph._run_loop_for_test = run_loop
    return graph


def _graph_and_run_loop(session=None, workspace: str = "/tmp/workspace") -> tuple[AgentService, TerminalRunLoop]:
    service = _graph(session=session, workspace=workspace)
    return service, _terminal_run_loop(service.test_host, service)


def _disable_external_managers(graph) -> None:
    execution = getattr(graph, "_execution", graph)
    if isinstance(execution, LangGraphExecution):
        execution._mcp_manager = NoopMcpManager()
        execution._lsp_manager = NoopLspManager()
    else:
        execution.mcp_manager = NoopMcpManager()
        execution.lsp_manager = NoopLspManager()
