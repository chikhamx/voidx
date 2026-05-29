"""Typed UI event bus for serializing terminal rendering updates."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from rich.markdown import Markdown

from voidx.ui.dock import BottomInputDock
from voidx.ui.event_parts.schema import (
    AnsiAppended,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamStarted,
    AssistantStreamUpdated,
    CaptureStarted,
    CaptureStopped,
    DiffAppended,
    ErrorAppended,
    FileChangeAppended,
    InputSet,
    MarkdownAppended,
    MessageAppended,
    NoticeSet,
    PermissionPromptCleared,
    PermissionPromptShown,
    PermissionToolDetail,
    RefreshRequested,
    ResetRequested,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
    SubagentStepStarted,
    ThoughtAppended,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TurnStarted,
    UiEvent,
    UiEventBase,
    WarningAppended,
)
from voidx.ui.tree import OutputNode


@dataclass
class _QueuedEvent:
    event: UiEvent
    future: asyncio.Future[Any] | None = None


class UiEventBus:
    """Single-consumer async queue for all UI mutations."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_QueuedEvent | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._consumer: DockEventConsumer | None = None
        self._last_error: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and self._queue is not None

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def start(self, consumer: DockEventConsumer) -> None:
        if self.is_running:
            self._consumer = consumer
            return
        self._queue = asyncio.Queue()
        self._consumer = consumer
        self._last_error = None
        self._task = asyncio.create_task(self._run(), name="voidx-ui-event-bus")

    async def emit(self, event: UiEvent) -> bool:
        if not self.is_running or self._queue is None:
            return False
        await self._queue.put(_QueuedEvent(event))
        return True

    def emit_nowait(self, event: UiEvent) -> bool:
        if not self.is_running or self._queue is None:
            return False
        self._queue.put_nowait(_QueuedEvent(event))
        return True

    async def request(self, event: UiEvent) -> Any:
        if not self.is_running or self._queue is None:
            raise RuntimeError("UI event bus is not running")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put(_QueuedEvent(event, future))
        return await future

    async def drain(self) -> None:
        if self._queue is not None:
            await self._queue.join()
        if self._last_error is not None:
            raise self._last_error

    async def stop(self) -> None:
        if self._queue is None or self._task is None:
            return
        await self._queue.join()
        await self._queue.put(None)
        await self._task
        self._queue = None
        self._task = None
        self._consumer = None

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                result = None
                try:
                    if self._consumer is None:
                        raise RuntimeError("UI event bus has no consumer")
                    result = self._consumer.handle(item.event)
                    if inspect.isawaitable(result):
                        result = await result
                except BaseException as exc:
                    self._last_error = exc
                    if item.future is not None and not item.future.done():
                        item.future.set_exception(exc)
                else:
                    if item.future is not None and not item.future.done():
                        item.future.set_result(result)
            finally:
                self._queue.task_done()


class DockEventConsumer:
    """Apply typed events to BottomInputDock in queue order."""

    def __init__(self, target: BottomInputDock) -> None:
        self._dock = target
        self._tool_nodes: dict[str, OutputNode] = {}
        self._agent_nodes: dict[int, OutputNode] = {}

    def handle(self, event: UiEvent) -> Any:
        if isinstance(event, CaptureStarted):
            return self._dock.begin_capture()
        if isinstance(event, CaptureStopped):
            return self._dock.deactivate()
        if isinstance(event, RefreshRequested):
            return self._dock.refresh()
        if isinstance(event, ResetRequested):
            self._tool_nodes.clear()
            self._agent_nodes.clear()
            return self._dock.reset()
        if isinstance(event, TurnStarted):
            self._tool_nodes.clear()
            self._agent_nodes.clear()
            return self._dock.start_turn(event.text)
        if isinstance(event, StartupShown):
            return self._dock.append_startup(
                model=event.model,
                provider=event.provider,
                workspace=event.workspace,
                session_title=event.session_title,
                is_new=event.is_new,
                profile_configured=event.profile_configured,
            )
        if isinstance(event, MessageAppended):
            return self._dock.append_message(event.text, style=event.style)
        if isinstance(event, AnsiAppended):
            return self._dock.append_ansi(event.text)
        if isinstance(event, MarkdownAppended):
            return self._dock.capture(lambda console: console.print(Markdown(event.content)))
        if isinstance(event, ThoughtAppended):
            return self._dock.append_thought(event.text, event.elapsed)
        if isinstance(event, WarningAppended):
            return self._dock.append_message(f"! {event.message}", style="yellow")
        if isinstance(event, ErrorAppended):
            return self._dock.append_error(event.message, parent=self._agent_parent(event.agent_id))
        if isinstance(event, DiffAppended):
            from voidx.ui.diff import render_diff

            return self._dock.capture(lambda console: render_diff(console, event.diff_text, event.title))
        if isinstance(event, StatusUpdated):
            return self._dock.set_status(
                event.status_id,
                event.label,
                event.detail,
                parent=self._status_parent(event),
                stage=event.stage.replace("_", " "),
            )
        if isinstance(event, StatusFinished):
            return self._dock.finish_status(
                event.status_id,
                label=event.label,
                detail=event.detail,
                ok=event.ok,
                remove=event.remove,
            )
        if isinstance(event, AssistantStreamStarted):
            return self._dock.set_stream("")
        if isinstance(event, AssistantStreamUpdated):
            return self._dock.set_stream(event.text)
        if isinstance(event, AssistantStreamCommitted):
            return self._dock.commit_stream()
        if isinstance(event, AssistantStreamDiscarded):
            return self._dock.discard_stream()
        if isinstance(event, ToolStarted):
            parent = self._agent_parent(event.agent_id)
            node = self._dock.start_tool(event.label, event.args, parent=parent)
            self._tool_nodes[event.tool_call_id] = node
            return node
        if isinstance(event, ToolFinished):
            node = self._tool_nodes.get(event.tool_call_id)
            if node is None:
                self._dock.finish_tool(event.label, event.elapsed, event.ok, event.detail)
                return None
            return self._dock.finish_tool_node(node, event.label, event.elapsed, event.ok, event.detail)
        if isinstance(event, ToolResultAppended):
            parent = self._tool_nodes.get(event.tool_call_id) if event.tool_call_id else None
            return self._dock.append_tool_result(event.text, parent=parent, collapsed=event.collapsed)
        if isinstance(event, FileChangeAppended):
            parent = self._tool_nodes.get(event.tool_call_id) if event.tool_call_id else None
            return self._dock.append_file_change(event.diff_text, parent=parent)
        if isinstance(event, SubagentStepStarted):
            parent = self._agent_parent(event.agent_id)
            return self._dock.set_status(
                f"agent:{event.agent_id}:progress",
                f"{event.name} ({event.step}/{event.max_steps})",
                parent=parent,
                stage="agent step",
            )
        if isinstance(event, SubagentStarted):
            parent = self._tool_nodes.get(event.parent_tool_call_id)
            if parent is None and event.parent_agent_id >= 0:
                parent = self._agent_parent(event.parent_agent_id)
            if parent is None:
                parent = self._dock.ensure_agent()
            header = f"⟳ {event.name}"
            if event.description:
                header += f": {event.description}"
            node = self._dock.tree.new_node(
                parent=parent,
                node_type="subagent",
                header=header,
                collapsed=False,
            )
            self._agent_nodes[event.agent_id] = node
            self._dock.refresh()
            return node
        if isinstance(event, SubagentFinished):
            label = "completed" if event.ok else "failed"
            elapsed = f" ({event.elapsed:.1f}s)" if event.elapsed is not None else ""
            self._dock.finish_status(f"agent:{event.agent_id}:progress")
            return self._dock.append_message(
                f"subagent {label}{elapsed}",
                style="dim",
                parent=self._agent_parent(event.agent_id),
            )
        if isinstance(event, InputSet):
            return self._dock.set_input(event.text, event.hints, event.cursor_pos)
        if isinstance(event, (PermissionPromptShown, PermissionPromptCleared, NoticeSet)):
            return None
        raise TypeError(f"Unsupported UI event: {event!r}")

    def _status_parent(self, event: StatusUpdated) -> OutputNode | None:
        if event.parent_tool_call_id:
            node = self._tool_nodes.get(event.parent_tool_call_id)
            if node is not None:
                return node
        if event.agent_id >= 0:
            return self._agent_parent(event.agent_id)
        return None

    def _agent_parent(self, agent_id: int) -> OutputNode | None:
        if agent_id < 0:
            return None
        node = self._agent_nodes.get(agent_id)
        if node is not None:
            return node
        node = self._dock.tree.new_node(
            parent=self._dock.ensure_agent(),
            node_type="subagent",
            header=f"⟳ agent {agent_id}",
            collapsed=False,
        )
        self._agent_nodes[agent_id] = node
        self._dock.refresh()
        return node


ui_events = UiEventBus()
