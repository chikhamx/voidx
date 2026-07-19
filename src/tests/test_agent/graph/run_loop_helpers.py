"""Shared helpers for test_run_loop split files."""

import sys
from pathlib import Path
from types import SimpleNamespace

import voidx.memory.store as store


from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.turn_service import TurnService
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.execution import _sanitize_generated_title
from voidx.config import Config, ModelConfig
from voidx.llm.usage import UsageStats
from voidx.runtime import InteractionMode, TaskState
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

    async def warm_up(self):
        return {}

    async def stop_all(self):
        return None


def _service(execution) -> AgentService:
    return AgentService(
        execution,
        TurnService(
            LangGraphTurnEngine(execution),
            MemorySessionAdapter(),
            NullEventPublisher(),
        ),
    )


def _graph(session=None, workspace: str = "/tmp/workspace") -> AgentService:
    execution = SimpleNamespace()
    graph = _service(execution)
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
    return graph


def _disable_external_managers(graph) -> None:
    execution = getattr(graph, "_execution", graph)
    if isinstance(execution, LangGraphExecution):
        execution._mcp_manager = NoopMcpManager()
        execution._lsp_manager = NoopLspManager()
    else:
        execution.mcp_manager = NoopMcpManager()
        execution.lsp_manager = NoopLspManager()
