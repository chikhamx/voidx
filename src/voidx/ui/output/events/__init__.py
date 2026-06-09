"""Typed UI event bus for serializing terminal rendering updates."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape

from voidx.ui.output.agent_display import agent_display_name
from voidx.ui.output.dock import BottomInputDock, dock
from voidx.ui.output.events.schema import (
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
    GuidanceSubmitted,
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
    TodoItemPayload,
    TodoUpdated,
    TurnStarted,
    UiEvent,
    UiEventBase,
    WarningAppended,
)
from voidx.ui.output.tree import OutputNode


TODO_MAX_VISIBLE_ITEMS = 8
TODO_STATUS_ORDER = ("in_progress", "pending", "completed", "cancelled")
TODO_ICONS = {
    "pending": "[dim]○[/dim]",
    "in_progress": "[#7AA2F7]◐[/#7AA2F7]",
    "completed": "[#A3BE8C]●[/#A3BE8C]",
    "cancelled": "[#BF616A]✕[/#BF616A]",
}


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

    def emit_direct(self, event: UiEvent) -> bool:
        """Apply a non-streaming event directly to the consumer, bypassing the queue.

        Use for one-shot events (tool calls, errors, etc.) that should appear
        immediately without waiting for queued streaming events to drain.
        """
        if not self.is_running or self._consumer is None:
            return False
        if hasattr(self._consumer, "handle_direct"):
            self._consumer.handle_direct(event)
        else:
            result = self._consumer.handle(event)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
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


class CompositeEventConsumer:
    """Apply an event to a primary consumer and mirror it to secondary consumers."""

    def __init__(self, primary: Any, mirrors: list[Any] | None = None) -> None:
        self._primary = primary
        self._mirrors = mirrors or []

    async def handle(self, event: UiEvent) -> Any:
        result = self._primary.handle(event)
        if inspect.isawaitable(result):
            result = await result
        mirror_tasks = []
        for mirror in self._mirrors:
            mirror_result = mirror.handle(event)
            if inspect.isawaitable(mirror_result):
                mirror_tasks.append(mirror_result)
        if mirror_tasks:
            await asyncio.gather(*mirror_tasks)
        return result

    def handle_direct(self, event: UiEvent) -> Any:
        """Synchronous variant: apply to primary immediately, schedule mirrors async."""
        result = self._primary.handle(event)
        if inspect.isawaitable(result):
            asyncio.create_task(result)
        for mirror in self._mirrors:
            mirror_result = mirror.handle(event)
            if inspect.isawaitable(mirror_result):
                asyncio.create_task(mirror_result)
        return result


class DockEventConsumer:
    """Apply typed events to BottomInputDock in queue order."""

    def __init__(self, target: BottomInputDock) -> None:
        self._dock = target
        self._tool_nodes: dict[str, OutputNode] = {}
        self._agent_nodes: dict[int, OutputNode] = {}

    def handle(self, event: UiEvent) -> Any:
        match event:
            case CaptureStarted():
                return self._dock.begin_capture()
            case CaptureStopped():
                return self._dock.deactivate()
            case RefreshRequested():
                return self._dock.refresh()
            case ResetRequested():
                self._tool_nodes.clear()
                self._agent_nodes.clear()
                return self._dock.reset()
            case TurnStarted(text=text):
                self._tool_nodes.clear()
                self._agent_nodes.clear()
                return self._dock.start_turn(text)
            case StartupShown() as e:
                return self._dock.append_startup(
                    model=e.model,
                    provider=e.provider,
                    workspace=e.workspace,
                    session_title=e.session_title,
                    is_new=e.is_new,
                    profile_configured=e.profile_configured,
                )
            case MessageAppended(text=text, style=style):
                return self._dock.append_message(text, style=style)
            case AnsiAppended(text=text):
                return self._dock.append_ansi(text)
            case MarkdownAppended(content=content):
                return self._dock.capture(lambda console: console.print(Markdown(content)))
            case ThoughtAppended(text=text, elapsed=elapsed):
                return self._dock.append_thought(text, elapsed)
            case WarningAppended(message=message):
                return self._dock.append_message(f"! {message}", style="yellow")
            case GuidanceSubmitted() as e:
                suffix = " [dim](truncated)[/dim]" if e.truncated else ""
                return self._dock.append_message(
                    f"[dim][guide][/dim] {escape(e.text)}{suffix}",
                    markup=True,
                )
            case ErrorAppended() as e:
                return self._dock.append_error(e.message, parent=self._agent_parent(e.agent_id))
            case DiffAppended() as e:
                from voidx.ui.output.diff import render_diff

                return self._dock.capture(lambda console: render_diff(console, e.diff_text, e.title))
            case StatusUpdated() as e:
                if (
                    e.stage == "agent_step"
                    and e.agent_id < 0
                    and not e.parent_tool_call_id
                ):
                    return self._dock.record_status(
                        e.status_id,
                        e.label,
                        e.detail,
                        stage=e.stage.replace("_", " "),
                    )
                return self._dock.set_status(
                    e.status_id,
                    e.label,
                    e.detail,
                    parent=self._status_parent(e),
                    stage=e.stage.replace("_", " "),
                )
            case StatusFinished() as e:
                return self._dock.finish_status(
                    e.status_id,
                    label=e.label,
                    detail=e.detail,
                    ok=e.ok,
                    remove=e.remove,
                )
            case AssistantStreamStarted() as e:
                return self._dock.set_stream("", parent=self._stream_parent(e.agent_id))
            case AssistantStreamUpdated() as e:
                return self._dock.set_stream(
                    e.text,
                    parent=self._stream_parent(e.agent_id),
                    phase=e.phase,
                )
            case AssistantStreamCommitted():
                return self._dock.commit_stream()
            case AssistantStreamDiscarded():
                return self._dock.discard_stream()
            case ToolStarted() as e:
                parent = self._agent_parent(e.agent_id)
                node = self._dock.start_tool(
                    e.label,
                    e.args,
                    parent=parent,
                    tool_call_id=e.tool_call_id,
                    tool_name=e.tool_name,
                    raw_args=e.raw_args,
                )
                self._tool_nodes[e.tool_call_id] = node
                return node
            case ToolFinished() as e:
                node = self._tool_nodes.get(e.tool_call_id)
                if node is None:
                    self._dock.finish_tool(e.label, e.elapsed, e.ok, e.detail)
                    return None
                return self._dock.finish_tool_node(node, e.label, e.elapsed, e.ok, e.detail)
            case ToolResultAppended() as e:
                parent = self._tool_nodes.get(e.tool_call_id) if e.tool_call_id else None
                if parent is None:
                    parent = self._stream_parent(e.agent_id)
                return self._dock.append_tool_result(
                    e.text,
                    parent=parent,
                    collapsed=e.collapsed,
                    tool_call_id=e.tool_call_id or None,
                )
            case TodoUpdated() as e:
                return self._update_todo_node(e)
            case FileChangeAppended() as e:
                parent = self._tool_nodes.get(e.tool_call_id) if e.tool_call_id else None
                if parent is None:
                    parent = self._stream_parent(e.agent_id)
                return self._dock.append_file_change(
                    e.diff_text,
                    parent=parent,
                    tool_call_id=e.tool_call_id or None,
                )
            case SubagentStepStarted() as e:
                parent = self._agent_parent(e.agent_id)
                return self._dock.set_status(
                    f"agent:{e.agent_id}:progress",
                    f"{e.name} ({e.step}/{e.max_steps})",
                    parent=parent,
                    stage="agent step",
                )
            case SubagentStarted() as e:
                parent = self._tool_nodes.get(e.parent_tool_call_id)
                if parent is not None:
                    parent.collapsed = False
                if parent is None and e.parent_agent_id >= 0:
                    parent = self._agent_parent(e.parent_agent_id)
                if parent is None:
                    parent = self._dock.ensure_agent()
                role_name = agent_display_name(e.name)
                node = self._dock.tree.new_node(
                    parent=parent,
                    node_type="subagent",
                    header=f"[#B48EAD]●[/#B48EAD] [bold]{escape(role_name)}[/bold]",
                    body_lines=[],
                    collapsed=False,
                    agent_name=role_name,
                    agent_run_id=e.subagent_id,
                    payload={
                        "role_name": role_name,
                        "agent_name": e.name,
                        "description": e.description,
                        "agent_id": e.agent_id,
                    },
                )
                self._agent_nodes[e.agent_id] = node
                self._dock.mark_node_unsettled(node)
                self._dock.refresh()
                return node
            case SubagentFinished() as e:
                node = self._agent_parent(e.agent_id)
                label = "completed" if e.ok else "failed"
                elapsed = f" ({e.elapsed:.1f}s)" if e.elapsed is not None else ""
                self._dock.finish_status(f"agent:{e.agent_id}:progress")
                if node is None:
                    return None
                color = "dim" if e.ok else "red"
                icon = "●" if e.ok else "✗"
                role_name = str(node.payload.get("role_name") or node.agent_name or e.subagent_id)
                node.header = f"[{color}]{icon}[/{color}] [{color}]{escape(role_name)} {label}{elapsed}[/{color}]"
                node.status = "done" if e.ok else "error"
                node.elapsed = e.elapsed
                node.collapsed = False
                self._dock.tree.mark_dirty()
                self._dock.mark_node_settled(node)
                self._dock.refresh()
                return node
            case InputSet() as e:
                return self._dock.set_input(e.text, e.hints, e.cursor_pos)
            case PermissionPromptShown() | PermissionPromptCleared() | NoticeSet():
                return None
            case _:
                raise TypeError(f"Unsupported UI event: {event!r}")

    def _update_todo_node(self, event: TodoUpdated) -> OutputNode | None:
        if not self._dock.active:
            return None
        root = self._dock.tree.root
        todo_node = next((child for child in root.children if child.node_type == "todo"), None)
        if todo_node is None:
            todo_node = self._dock.tree.new_node(
                parent=root,
                node_type="todo",
                header="Todo",
                body_lines=[],
                collapsed=False,
                status="done",
            )
        self._ensure_root_first_child(todo_node)

        todo_node.header = f"[bold]Todo[/bold]: {escape(event.summary)}"
        todo_node.body_lines = self._render_todo_lines(event)
        todo_node.payload = {
            "items": [item.model_dump(mode="json") for item in event.items],
            "summary": event.summary,
        }
        todo_node.status = "done"
        todo_node.collapsed = False
        self._dock.tree.mark_dirty(todo_node.id)
        self._dock.mark_node_settled(todo_node)
        self._dock.set_todo_state(event.summary, event.items)
        return todo_node

    def _ensure_root_first_child(self, node: OutputNode) -> None:
        root = self._dock.tree.root
        self._dock.tree.move_child_to_first(root, node)

    def _render_todo_lines(self, event: TodoUpdated) -> list[str]:
        total = len(event.items)
        done = sum(1 for item in event.items if item.status == "completed")
        if total == 0:
            return ["[dim]No todos[/dim]"]

        bar_len = 20
        filled = int(bar_len * (done / total))
        bar = "█" * filled + "░" * (bar_len - filled)
        lines = [escape(f"[{bar}] {done}/{total} done")]

        ordered_items = [
            item
            for status in TODO_STATUS_ORDER
            for item in event.items
            if item.status == status
        ]
        visible_items = ordered_items[:TODO_MAX_VISIBLE_ITEMS]
        for item in visible_items:
            lines.append(f"  {TODO_ICONS[item.status]} {escape(item.content)}")
        omitted = len(ordered_items) - len(visible_items)
        if omitted > 0:
            lines.append(f"  [dim]… {omitted} more todos[/dim]")
        return lines

    def _status_parent(self, event: StatusUpdated) -> OutputNode | None:
        if event.parent_tool_call_id:
            node = self._tool_nodes.get(event.parent_tool_call_id)
            if node is not None:
                return node
        if event.agent_id >= 0:
            return self._agent_parent(event.agent_id)
        return None

    def _stream_parent(self, agent_id: int) -> OutputNode | None:
        if agent_id >= 0:
            return self._agent_parent(agent_id)
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
            header=f"[#B48EAD]●[/#B48EAD] [bold]child agent {agent_id}[/bold]",
            collapsed=False,
            agent_name=f"agent {agent_id}",
        )
        self._agent_nodes[agent_id] = node
        self._dock.refresh()
        return node


ui_events = UiEventBus()


def via_events() -> bool:
    """True when the dock is active and the UI event bus is the preferred rendering path."""
    return dock.active and ui_events.is_running
