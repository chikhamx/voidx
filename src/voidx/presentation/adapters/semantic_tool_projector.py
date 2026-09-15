"""Opt-in tool/status/todo projection on a caller-owned transactional state copy.

No reference resolver is installed: tool result_ref is rejected (summary is not
its detail); inline file diff is sufficient. Legacy file events lack a path slot.
Tool labels default to the existing title formatter, not invented gerunds.
Status description becomes label, result becomes finish detail; unknown stages
require an explicit UI policy. Todo summary is absent and stays empty; boundary
and full commit payload are validated locally because legacy commit has no data.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from voidx.agent.domain import semantic_events as s
from voidx.agent.domain import ui_events as ui
from voidx.agent.domain.display_policy import ToolDisplayMode, ToolDisplayPolicy
from voidx.presentation.output.console.formatting import format_tool_args, format_tool_title

ToolLabel = Callable[[str, bool], str]
StatusPolicy = Callable[[s.StatusPayload], dict[str, Any]]
SUPPORTED = (s.ToolStarted, s.ToolFinished, s.ToolResult, s.FileChanged,
             s.StatusStarted, s.StatusUpdated, s.StatusFinished,
             s.TodoUpdated, s.TodoCommitted, s.TodoCleared)


@dataclass
class _Tool:
    name: str
    hidden: bool
    ok: bool | None = None
    result_seen: bool = False


@dataclass
class ToolProjectionState:
    tools: dict[str, _Tool] = field(default_factory=dict)
    statuses: dict[str, tuple[str | None, bool]] = field(default_factory=dict)
    todo: s.TodoPayload | None = None

    def validate_complete(self) -> None:
        if any(tool.ok is None for tool in self.tools.values()):
            raise ValueError("Cannot complete turn with active tools")
        if any(not finished for _, finished in self.statuses.values()):
            raise ValueError("Cannot complete turn with active statuses")
        if self.todo is not None:
            raise ValueError("Cannot complete turn with uncommitted todo preview")


class SemanticToolProjector:
    def __init__(self, *, display_policy: ToolDisplayPolicy | None = None,
                 tool_label: ToolLabel | None = None,
                 status_policy: StatusPolicy | None = None) -> None:
        self.display_policy = display_policy if display_policy is not None else ToolDisplayPolicy()
        self.tool_label = tool_label or (lambda name, started: format_tool_title(name))
        self.status_policy = status_policy or (lambda payload: {})

    def project(self, event: s.SemanticEvent, state: ToolProjectionState,
                common: dict) -> list[ui.UiEvent]:
        p = event.payload
        if isinstance(event, (s.StatusStarted, s.StatusUpdated, s.StatusFinished)):
            return self._status(event, state, common)
        if isinstance(event, (s.TodoUpdated, s.TodoCommitted, s.TodoCleared)):
            return self._todo(event, state, common)
        if isinstance(event, s.FileChanged):
            if p.tool_call_id not in state.tools:
                raise ValueError("File change requires a started tool")
            if p.diff is None:
                raise ValueError("Unresolved result_ref: inline diff is required")
            return [ui.FileChangeAppended(**common, tool_call_id=p.tool_call_id, diff_text=p.diff)]
        if type(event) not in (s.ToolStarted, s.ToolFinished, s.ToolResult):
            raise NotImplementedError(f"Unsupported semantic kind: {event.kind}")
        tool = state.tools.get(p.tool_call_id)
        if isinstance(event, s.ToolStarted):
            if tool is not None:
                raise ValueError("Tool already started")
            rule = self.display_policy.rule_for(p.name)
            output = ui.ToolStarted(**common, tool_call_id=p.tool_call_id,
                tool_name=p.name, label=self.tool_label(p.name, True),
                args=format_tool_args(p.arguments), raw_args=p.arguments,
                display_mode=rule.mode, summary_max_lines=rule.summary_max_lines)
            state.tools[p.tool_call_id] = _Tool(p.name, rule.mode == ToolDisplayMode.HIDDEN)
            return [output]
        if tool is None or tool.name != p.name:
            raise ValueError("Tool must be started with matching name")
        if isinstance(event, s.ToolFinished):
            if tool.ok is not None:
                raise ValueError("Tool already finished")
            output = ui.ToolFinished(**common, tool_call_id=p.tool_call_id,
                label=self.tool_label(p.name, False), elapsed=p.elapsed, ok=p.ok, detail="")
            tool.ok = p.ok
            return [output]
        if tool.ok is None or tool.result_seen:
            raise ValueError("Tool result requires a finished tool and occurs once")
        if p.result_ref is not None:
            raise ValueError("Unresolved result_ref: inline tool result is required")
        mode, lines = self.display_policy.resolve_display_mode(p.name, p.summary, result_ok=tool.ok)
        output = ui.ToolResultAppended(**common, tool_call_id=p.tool_call_id,
            text=p.summary, collapsed=False, display_mode=mode, summary_max_lines=lines)
        tool.result_seen = True
        if tool.hidden and mode != ToolDisplayMode.HIDDEN and tool.ok is False:
            # Legacy consumers latch hidden IDs; a result mode alone cannot reveal it.
            return [output, ui.ErrorAppended(**common,
                message=f"{p.name} [{p.tool_call_id}]: {p.summary}")]
        return [output]

    def _status(self, event, state: ToolProjectionState, common: dict) -> list[ui.UiEvent]:
        p = event.payload
        previous = state.statuses.get(p.status_id)
        if (isinstance(event, (s.StatusUpdated, s.StatusFinished)) and p.parent_tool_call_id is None
                and (p.status_id, p.stage) in {
                    ("goal:phase", "work"), ("goal:phase", "evaluator"),
                    ("loop:waiting", "waiting"), ("loop:waiting", "idle"),
                }):
            # Scheduler commits are standalone snapshots, not tool status lifecycles.
            update = ui.StatusUpdated(**common, status_id=p.status_id,
                label=p.stage, detail=p.description, stage="working", display="tree_node")
            if isinstance(event, s.StatusFinished):
                return [update, ui.StatusFinished(**common, status_id=p.status_id,
                    label=p.stage, detail=p.result or p.description, ok=p.ok, remove=False)]
            return [update]
        if isinstance(event, s.StatusStarted):
            if previous is not None:
                raise ValueError("Status already started")
            if p.parent_tool_call_id is not None and p.parent_tool_call_id not in state.tools:
                raise ValueError("Status parent tool must be started")
        elif previous is None or previous[1] or previous[0] != p.parent_tool_call_id:
            raise ValueError("Status must be active with matching parent")
        options = dict(self.status_policy(p))
        remove = options.pop("remove", True)
        fields = dict(label=p.description, detail="", stage=p.stage, display="tree_node")
        if options.keys() - fields.keys():
            raise ValueError("Unknown status policy fields")
        fields.update(options)
        update = ui.StatusUpdated(**common, status_id=p.status_id,
            parent_tool_call_id=p.parent_tool_call_id or "", **fields)
        finished = isinstance(event, s.StatusFinished)
        if finished:
            output = ui.StatusFinished(**common, status_id=p.status_id, label=update.label,
                detail=p.result, ok=p.ok, remove=remove)
        else:
            output = update
        state.statuses[p.status_id] = (p.parent_tool_call_id, finished)
        return [output]

    def _todo(self, event, state: ToolProjectionState, common: dict) -> list[ui.UiEvent]:
        p = event.payload
        if isinstance(event, s.TodoCleared):
            if p.items or p.operation != "clear":
                raise ValueError("Todo clear requires empty items and clear operation")
            state.todo = None
            return [ui.TodoCleared(**common)]
        if p.operation == "clear":
            raise ValueError("Use todo.cleared for clear operation")
        if isinstance(event, s.TodoCommitted):
            if state.todo != p:
                raise ValueError("Todo commit must match preview payload and boundary")
            state.todo = None
            return [ui.TodoCommitted(**common)]
        if state.todo is not None and state.todo.boundary_id != p.boundary_id:
            raise ValueError("Todo preview boundary is still pending")
        output = ui.TodoUpdated(**common,
            items=[ui.TodoItemPayload(**item.model_dump()) for item in p.items],
            summary="", todo_op=p.operation)
        state.todo = p.model_copy(deep=True)
        return [output]
