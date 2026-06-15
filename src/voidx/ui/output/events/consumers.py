"""UI event consumers for output backends."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape

from voidx.ui.output.agent_display import agent_display_name
from voidx.ui.output.dock import BottomInputDock
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
    TodoCleared,
    TodoCommitted,
    TodoUpdated,
    TurnStarted,
    UiEvent,
    WarningAppended,
)
from voidx.ui.output.display_policy import ToolDisplayMode
from voidx.ui.output.tree import OutputNode


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
            self._schedule_direct_task(result, target="primary")
        for mirror in self._mirrors:
            mirror_result = mirror.handle(event)
            if inspect.isawaitable(mirror_result):
                self._schedule_direct_task(mirror_result, target="mirror")
        return result

    def _schedule_direct_task(self, result: Awaitable[Any], *, target: str) -> None:
        task = asyncio.create_task(result)
        task.add_done_callback(
            lambda done: self._log_direct_task_error(done, target)
        )

    @staticmethod
    def _log_direct_task_error(task: asyncio.Task[Any], target: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logging.getLogger(__name__).warning(
            "UI event direct %s consumer failed",
            target,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


class DockEventConsumer:
    """Apply typed events to BottomInputDock in queue order."""

    def __init__(self, target: BottomInputDock) -> None:
        self._dock = target
        self._tool_nodes: dict[str, OutputNode] = {}
        self._hidden_tool_ids: set[str] = set()
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
                self._hidden_tool_ids.clear()
                self._agent_nodes.clear()
                return self._dock.reset()
            case TurnStarted(text=text):
                self._tool_nodes.clear()
                self._hidden_tool_ids.clear()
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
            case GuidanceSubmitted():
                return None
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
                if e.display_mode == ToolDisplayMode.HIDDEN:
                    self._hidden_tool_ids.add(e.tool_call_id)
                    return None
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
                if e.tool_call_id in self._hidden_tool_ids:
                    return None
                node = self._tool_nodes.get(e.tool_call_id)
                if node is None:
                    self._dock.finish_tool(e.label, e.elapsed, e.ok, e.detail)
                    return None
                return self._dock.finish_tool_node(node, e.label, e.elapsed, e.ok, e.detail)
            case ToolResultAppended() as e:
                if e.tool_call_id in self._hidden_tool_ids:
                    return None
                text = e.text
                if e.display_mode == ToolDisplayMode.SUMMARY:
                    lines = text.splitlines()
                    if len(lines) > e.summary_max_lines:
                        truncated = "\n".join(lines[:e.summary_max_lines])
                        omitted = len(lines) - e.summary_max_lines
                        text = f"{truncated}\n[dim]… +{omitted} more lines[/dim]"
                parent = self._tool_nodes.get(e.tool_call_id) if e.tool_call_id else None
                if parent is None:
                    parent = self._stream_parent(e.agent_id)
                return self._dock.append_tool_result(
                    text,
                    parent=parent,
                    collapsed=e.collapsed,
                    tool_call_id=e.tool_call_id or None,
                )
            case TodoUpdated() as e:
                return self._dock.set_todo_state(e.summary, e.items)
            case TodoCommitted():
                return self._dock.commit_todo_state()
            case TodoCleared():
                return self._dock.clear_todo_state()
            case FileChangeAppended() as e:
                parent = self._tool_nodes.get(e.tool_call_id) if e.tool_call_id else None
                if e.tool_call_id in self._hidden_tool_ids:
                    return None
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
                details: list[str] = []
                if e.final_step is not None and e.max_steps is not None:
                    details.append(f"{e.final_step}/{e.max_steps}")
                if e.finish_reason:
                    details.append(e.finish_reason.replace("_", " "))
                if e.elapsed is not None:
                    details.append(f"{e.elapsed:.1f}s")
                suffix = f" ({', '.join(details)})" if details else ""
                self._dock.finish_status(f"agent:{e.agent_id}:progress")
                if node is None:
                    return None
                color = "dim" if e.ok else "red"
                icon = "●" if e.ok else "✗"
                role_name = str(node.payload.get("role_name") or node.agent_name or e.subagent_id)
                header = f"[{color}]{icon}[/{color}] [{color}]{escape(role_name)} {label}{suffix}[/{color}]"
                node.header = header
                if (
                    node.parent is not None
                    and node.parent.node_type == "tool_call"
                    and node.parent.payload.get("tool_name") == "agent"
                ):
                    node.parent.header = header
                    node.parent.status = "done" if e.ok else "error"
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
