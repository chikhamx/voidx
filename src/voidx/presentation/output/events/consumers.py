"""UI event consumers for output backends."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any

from voidx.observability import log_internal_error
from rich.markup import escape

from voidx.presentation.output.agent_display import subagent_display_name
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.dock.status import PERMISSION_REQUEST_STATUS_ID
from voidx.presentation.output.dock.todo import (
    render_todo_header,
    render_todo_state_lines,
    todo_state_from_items,
    todo_state_payload,
)
from voidx.presentation.output.dock.formatting import short_path, short_value
from voidx.presentation.output.manage_display import manage_display
from voidx.presentation.output.tool_display import extract_tool_display_value, mcp_gateway_tool_name
from voidx.presentation.output.events.schema import (
    AnsiAppended,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamStarted,
    AssistantStreamUpdated,
    CaptureStarted,
    CaptureStopped,
    CheckpointDecisionSubmitted,
    CheckpointPromptShown,
    ClarifyAnswerSubmitted,
    ClarifyPromptShown,
    DiffAppended,
    ErrorAppended,
    GoalSpecDecisionSubmitted,
    GoalSpecPromptShown,
    FileChangeAppended,
    GuidanceCommitted,
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
    ContextPressureFinished,
    ContextPressureUpdated,
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
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UiEvent,
    WarningAppended,
)
from voidx.presentation.output.display_policy import ToolDisplayMode
from voidx.presentation.output.tree import OutputNode


class CompositeEventConsumer:
    """Apply an event to a primary consumer and mirror it to secondary consumers."""

    def __init__(self, primary: Any, mirrors: list[Any] | None = None) -> None:
        self._primary = primary
        self._mirrors = mirrors or []

    async def handle(self, event: UiEvent) -> Any:
        result = self._primary.handle(event)
        if inspect.isawaitable(result):
            result = await result
        for mirror in self._mirrors:
            try:
                mirror_result = mirror.handle(event)
            except Exception as e:
                log_internal_error(e, context="ui_event_mirror_consumer")
                continue
            if inspect.isawaitable(mirror_result):
                self._schedule_task(mirror_result, target="mirror")
        return result

    def handle_direct(self, event: UiEvent) -> Any:
        """Synchronous variant: apply to primary immediately, schedule mirrors async."""
        result = self._primary.handle(event)
        if inspect.isawaitable(result):
            self._schedule_task(result, target="primary", direct=True)
        for mirror in self._mirrors:
            try:
                mirror_result = mirror.handle(event)
            except Exception as e:
                log_internal_error(e, context="ui_event_direct_mirror_consumer")
                continue
            if inspect.isawaitable(mirror_result):
                self._schedule_task(mirror_result, target="mirror", direct=True)
        return result

    def _schedule_task(self, result: Awaitable[Any], *, target: str, direct: bool = False) -> None:
        task = asyncio.create_task(result)
        task.add_done_callback(
            lambda done: self._log_task_error(done, target, direct=direct)
        )

    @staticmethod
    def _log_task_error(task: asyncio.Task[Any], target: str, *, direct: bool) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        direct_label = "direct_" if direct else ""
        log_internal_error(exc, context=f"ui_event_{direct_label}consumer_{target}")


class DockEventConsumer:
    """Apply typed events to BottomInputDock in queue order."""

    def __init__(self, target: BottomInputDock) -> None:
        self._dock = target
        self._tool_nodes: dict[str, OutputNode] = {}
        self._hidden_tool_ids: set[str] = set()
        self._agent_nodes: dict[int, OutputNode] = {}
        self._agents_with_specific_status: set[int] = set()

    def _reset_turn_state(self) -> None:
        self._tool_nodes.clear()
        self._dock.clear_status_record(PERMISSION_REQUEST_STATUS_ID)
        self._dock.clear_status_record("error:current")
        self._dock.clear_status_record("llm:retry")
        self._hidden_tool_ids.clear()
        self._agent_nodes.clear()
        self._agents_with_specific_status.clear()

    def handle(self, event: UiEvent) -> Any:
        match event:
            case CaptureStarted():
                return self._dock.begin_capture()
            case CaptureStopped():
                return self._dock.deactivate()
            case RefreshRequested():
                return self._dock.refresh()
            case ResetRequested():
                self._reset_turn_state()
                return self._dock.reset()
            case TurnStarted(text=text, metadata=metadata):
                self._reset_turn_state()
                return self._dock.start_turn(text, metadata=metadata)
            case TurnCompleted():
                self._dock.end_turn()
                return None
            case TurnCancelled():
                self._dock.end_turn()
                return None
            case TurnFailed() as e:
                self._dock.end_turn()
                if not e.message:
                    return None
                self._dock.record_status(
                    "error:current",
                    "Error",
                    e.message,
                    stage="error",
                )
                return self._dock.append_error(e.message)
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
                return self._dock.append_message(content, style="markdown")
            case ThoughtAppended(text=text, elapsed=elapsed):
                return self._dock.append_thought(text, elapsed)
            case WarningAppended(message=message):
                return self._dock.append_message(message, style="warning")
            case GuidanceSubmitted(text=text):
                return self._dock.set_guidance_preview(text)
            case GuidanceCommitted(text=text, source=source):
                self._dock.clear_guidance_preview()
                if source == "user" and text:
                    return self._dock.append_guidance_turn(text)
                return None
            case ErrorAppended() as e:
                self._dock.clear_status_record("llm:retry")
                self._dock.record_status(
                    "error:current",
                    "Error",
                    e.message,
                    stage="error",
                )
                return self._dock.append_error(e.message, parent=self._agent_parent(e.agent_id))
            case DiffAppended() as e:
                return self._dock.append_message(e.diff_text, style="diff", title=e.title)
            case StatusUpdated() as e:
                if e.display == "record_only" or e.status_id == "llm:retry":
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
            case ContextPressureUpdated() as e:
                return self._dock.set_status(
                    e.pressure_id,
                    f"Context pressure: converging current turn ({e.level})",
                    e.reason,
                    stage="working",
                )
            case ContextPressureFinished() as e:
                return self._dock.finish_status(
                    e.pressure_id,
                    detail=e.detail,
                    ok=e.ok,
                    remove=True,
                )
            case AssistantStreamStarted() as e:
                if e.agent_id >= 0:
                    return None
                return self._dock.set_stream("", parent=self._stream_parent(e.agent_id))
            case AssistantStreamUpdated() as e:
                if e.agent_id >= 0:
                    self._agents_with_specific_status.add(e.agent_id)
                    return self._dock.set_status(
                        f"agent:{e.agent_id}:progress",
                        "Thinking" if e.phase == "thinking" else "Responding",
                        parent=self._agent_parent(e.agent_id),
                        stage="agent step",
                    )
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
                if e.agent_id >= 0:
                    self._hidden_tool_ids.add(e.tool_call_id)
                    self._agents_with_specific_status.add(e.agent_id)
                    return self._dock.set_status(
                        f"agent:{e.agent_id}:progress",
                        _subagent_tool_status(e.tool_name, e.label, e.raw_args, e.args),
                        parent=self._agent_parent(e.agent_id),
                        stage="agent step",
                    )
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
                if e.agent_id >= 0:
                    if not e.items:
                        return self._clear_subagent_todo_node(e.agent_id)
                    return self._upsert_subagent_todo_node(e)
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
                if e.agent_id in self._agents_with_specific_status:
                    return None
                parent = self._agent_parent(e.agent_id)
                return self._dock.set_status(
                    f"agent:{e.agent_id}:progress",
                    e.name,
                    parent=parent,
                    stage="agent step",
                )
            case SubagentStarted() as e:
                fallback = self._agent_nodes.get(e.agent_id)
                canonical = self._tool_nodes.get(e.parent_tool_call_id)
                display_name = subagent_display_name(e.subagent_id or e.agent_id)
                mode = _subagent_mode(e.description)
                title = _subagent_title(display_name, e.description, mode=mode)
                self._agents_with_specific_status.discard(e.agent_id)
                if canonical is not None and canonical.payload.get("tool_name") == "agent":
                    node = canonical
                    if fallback is not None and fallback is not node:
                        self._move_children(fallback, node)
                        self._dock._remove_node(fallback)
                    self._reparent_status(f"agent:{e.agent_id}:progress", node)
                    self._tool_nodes.pop(e.parent_tool_call_id, None)
                    self._hidden_tool_ids.add(e.parent_tool_call_id)
                elif fallback is not None:
                    node = fallback
                else:
                    parent = self._agent_parent(e.parent_agent_id) if e.parent_agent_id >= 0 else None
                    if parent is None:
                        parent = self._dock.ensure_agent()
                    node = self._dock.tree.new_node(
                        parent=parent,
                        node_type="subagent",
                        collapsed=False,
                    )
                node.node_type = "subagent"
                node.header = f"[#B48EAD]●[/#B48EAD] [bold]{escape(title)}[/bold]"
                node.body_lines = []
                node.collapsed = False
                node.agent_name = display_name
                node.agent_run_id = e.subagent_id
                node.meta = None
                node.payload = {
                    "role_name": display_name,
                    "display_name": display_name,
                    "name": display_name,
                    "title": title,
                    "agent_name": e.name,
                    "mode": mode,
                    "description": e.description,
                    "agent_id": e.agent_id,
                    "parent_tool_call_id": e.parent_tool_call_id,
                }
                self._agent_nodes[e.agent_id] = node
                self._dock.mark_node_unsettled(node)
                self._dock.tree.mark_dirty()
                self._dock.refresh()
                return node
            case SubagentFinished() as e:
                node = self._agent_parent(e.agent_id)
                label = "completed" if e.ok else "failed"
                details: list[str] = []
                if e.finish_reason:
                    details.append(e.finish_reason.replace("_", " "))
                if e.error:
                    details.append(e.error)
                if e.elapsed is not None:
                    details.append(f"{e.elapsed:.1f}s")
                suffix = f" ({', '.join(details)})" if details else ""
                self._dock.finish_status(
                    f"agent:{e.agent_id}:progress",
                    label=_subagent_finish_summary(e.summary, ok=e.ok, finish_reason=e.finish_reason),
                    ok=e.ok,
                    remove=False,
                )
                self._agents_with_specific_status.discard(e.agent_id)
                if node is None:
                    return None
                color = "dim" if e.ok else "red"
                icon = "●" if e.ok else "✗"
                title = str(node.payload.get("title") or node.payload.get("role_name") or node.agent_name or e.subagent_id)
                header = f"[{color}]{icon}[/{color}] [{color}]{escape(title)} {label}{suffix}[/{color}]"
                node.header = header
                node.status = "done" if e.ok else "error"
                node.elapsed = e.elapsed
                node.collapsed = False
                self._dock.tree.mark_dirty()
                self._dock.mark_node_settled(node)
                self._dock.refresh()
                return node
            case InputSet() as e:
                return self._dock.set_input(e.text, e.hints, e.cursor_pos)
            case PermissionPromptShown() as e:
                tools = [t.model_dump() for t in e.tools]
                return self._dock.record_status(
                    PERMISSION_REQUEST_STATUS_ID,
                    "Requesting",
                    _permission_detail_text(tools),
                    stage="permission",
                )
            case PermissionPromptCleared():
                return self._dock.clear_status_record(PERMISSION_REQUEST_STATUS_ID)
            case CheckpointPromptShown() as e:
                choices = [choice.model_dump(mode="json") for choice in e.choices]
                return self._dock.show_checkpoint(
                    e.checkpoint_id,
                    e.plan.model_dump(mode="json"),
                    choices,
                    parent=self._agent_parent(e.agent_id),
                )
            case CheckpointDecisionSubmitted() as e:
                return self._dock.resolve_checkpoint(
                    e.checkpoint_id,
                    e.decision,
                    e.label,
                    e.response,
                    was_custom_input=e.was_custom_input,
                )
            case ClarifyPromptShown() as e:
                return self._dock.show_clarify(
                    e.clarify_id,
                    e.question,
                    e.options,
                    parent=self._agent_parent(e.agent_id),
                )
            case ClarifyAnswerSubmitted() as e:
                return self._dock.resolve_clarify(
                    e.clarify_id,
                    e.answer,
                    cancelled=e.cancelled,
                    was_custom_input=e.was_custom_input,
                )
            case GoalSpecPromptShown() as e:
                choices = [choice.model_dump(mode="json") for choice in e.choices]
                return self._dock.show_goal_spec(
                    e.prompt_id,
                    e.spec.model_dump(mode="json"),
                    choices,
                    parent=self._agent_parent(e.agent_id),
                )
            case GoalSpecDecisionSubmitted() as e:
                return self._dock.resolve_goal_spec(
                    e.prompt_id,
                    e.decision,
                    e.response,
                )
            case NoticeSet():
                return None
            case _:
                raise TypeError(f"Unsupported UI event: {event!r}")

    def _upsert_subagent_todo_node(self, event: TodoUpdated) -> OutputNode:
        parent = self._agent_parent(event.agent_id)
        if parent is None:
            raise RuntimeError("subagent todo requires a child agent parent")
        state = todo_state_from_items(event.summary, event.items)
        node = next((child for child in parent.children if child.node_type == "todo"), None)
        if node is None:
            node = self._dock.tree.new_node(
                parent=parent,
                node_type="todo",
                collapsed=False,
                status="done",
            )
        node.header = render_todo_header(state)
        node.body_lines = render_todo_state_lines(state)
        node.payload = todo_state_payload(state)
        node.status = "done"
        self._dock.tree.mark_dirty(node.id)
        self._dock.mark_node_settled(node)
        self._dock.refresh()
        return node

    def _clear_subagent_todo_node(self, agent_id: int) -> None:
        parent = self._agent_nodes.get(agent_id)
        if parent is None:
            return None
        node = next((child for child in parent.children if child.node_type == "todo"), None)
        if node is None:
            return None
        self._dock._remove_node(node)
        self._dock.refresh()
        return None

    def _move_children(self, source: OutputNode, destination: OutputNode) -> None:
        children = list(source.children)
        source.children.clear()
        for child in children:
            child.parent = destination
            child.depth = destination.depth + 1
            destination.children.append(child)
            self._recompute_depths(child)
        for index, child in enumerate(destination.children):
            child._is_last_sibling = index == len(destination.children) - 1
        self._dock.tree.mark_dirty()

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

    def _reparent_status(self, status_id: str, parent: OutputNode) -> None:
        node = self._dock._status_nodes.get(status_id)
        if node is None or node.parent is parent:
            return
        old_parent = node.parent
        if old_parent is not None and node in old_parent.children:
            old_parent.children.remove(node)
            for index, child in enumerate(old_parent.children):
                child._is_last_sibling = index == len(old_parent.children) - 1
        node.parent = parent
        node.depth = parent.depth + 1
        parent.children.append(node)
        for index, child in enumerate(parent.children):
            child._is_last_sibling = index == len(parent.children) - 1
        self._recompute_depths(node)
        self._dock.tree.mark_dirty()

    def _recompute_depths(self, node: OutputNode) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.children:
                child.depth = current.depth + 1
                stack.append(child)


def _subagent_tool_status(
    tool_name: str,
    label: str,
    raw_args: dict[str, Any],
    args: str = "",
) -> str:
    if tool_name == "manage":
        action, detail = manage_display(raw_args, limit=72)
        return f"{action} {detail}" if detail else action
    action = _subagent_tool_action(tool_name, label, raw_args)
    detail = extract_tool_display_value(tool_name, raw_args, args, short_path_limit=72)
    return f"{action} {detail}" if detail else action


def _subagent_finish_summary(summary: str, *, ok: bool, finish_reason: str = "") -> str:
    if ok:
        clean = " ".join(summary.split())
        if clean:
            return clean[:69] + "…" if len(clean) > 72 else clean
        return "Completed"
    reason = " ".join(finish_reason.replace("_", " ").split())
    if reason:
        text = f"Failed: {reason}"
        return text[:69] + "…" if len(text) > 72 else text
    return "Failed"


def _permission_detail_text(tools: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, tool in enumerate(tools, 1):
        name = str(tool.get("name") or "tool")
        lines.append(f"{index}. {name}")
        pattern = str(tool.get("pattern") or "")
        if pattern and pattern != "*":
            lines.append(f"   target: {pattern}")
        ai_approval_failure = str(tool.get("ai_approval_failure") or "")
        if ai_approval_failure:
            lines.append(f"   ai approval: {ai_approval_failure}")
        args = tool.get("args")
        if isinstance(args, dict):
            for key, value in args.items():
                lines.append(f"   {key}: {short_value(value)}")
    return "\n".join(lines)


def _subagent_mode(description: str) -> str:
    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith("Mode:"):
            return stripped[len("Mode:"):].strip()
    return ""


def _subagent_title(display_name: str, description: str, *, mode: str = "") -> str:
    summary = _subagent_description_summary(description)
    mode = mode or _subagent_mode(description)
    if mode and summary:
        return f"{display_name} · {mode}({short_path(summary, limit=56)})"
    if mode:
        return f"{display_name} · {mode}"
    if summary:
        return f"{display_name}({short_path(summary, limit=56)})"
    return display_name


def _subagent_description_summary(description: str) -> str:
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if not lines:
        return ""
    for prefix in ("Task:", "Target:", "Success criteria:"):
        for line in lines:
            if line.startswith(prefix):
                return " ".join(line[len(prefix):].strip().split())
    first = next(
        (line for line in lines if not line.startswith(("Mode:", "Result schema:"))),
        lines[0],
    )
    if ":" in first:
        first = first.split(":", 1)[1].strip()
    return " ".join(first.split())


def _subagent_tool_action(tool_name: str, label: str, raw_args: dict[str, Any] | None = None) -> str:
    if tool_name == "mcp":
        return mcp_gateway_tool_name(raw_args or {})
    mapping = {
        "read": "Reading",
        "manage": "Managing",
        "write": "Editing",
        "replace": "Editing",
        "edit": "Editing",
        "bash": "Running",
        "powershell": "Running",
        "git": "Git",
        "search": "Searching",
        "find": "Searching",
        "lsp": "Inspecting",
        "webfetch": "Fetching",
        "websearch": "Searching",
        "todo": "Updating tasks",
        "checkpoint": "Checking plan",
        "clarify": "Waiting for input",
    }
    if tool_name in mapping:
        return mapping[tool_name]
    return label or (tool_name.replace("_", " ").title() if tool_name else "Working")
