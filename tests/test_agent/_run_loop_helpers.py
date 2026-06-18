"""Shared helpers for test_run_loop split files."""

import sys
from pathlib import Path
from types import SimpleNamespace

import voidx.memory.store as store

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.config import Config
from voidx.llm.usage import UsageStats
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
