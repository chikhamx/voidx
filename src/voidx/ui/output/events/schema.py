"""Typed UI event schemas."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from voidx.runtime.todo import TodoStatus


from voidx.ui.output.display_policy import ToolDisplayMode


class UiEventBase(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent_id: int = -1


class CaptureStarted(UiEventBase):
    kind: Literal["capture.started"] = "capture.started"


class CaptureStopped(UiEventBase):
    kind: Literal["capture.stopped"] = "capture.stopped"


class RefreshRequested(UiEventBase):
    kind: Literal["refresh.requested"] = "refresh.requested"


class ResetRequested(UiEventBase):
    kind: Literal["reset.requested"] = "reset.requested"


class TurnStarted(UiEventBase):
    kind: Literal["turn.started"] = "turn.started"
    text: str


class StartupShown(UiEventBase):
    kind: Literal["startup.shown"] = "startup.shown"
    model: str
    provider: str
    workspace: str
    session_title: str
    is_new: bool
    profile_configured: bool = True


class MessageAppended(UiEventBase):
    kind: Literal["message.appended"] = "message.appended"
    text: str
    style: str = ""


class AnsiAppended(UiEventBase):
    kind: Literal["ansi.appended"] = "ansi.appended"
    text: str


class MarkdownAppended(UiEventBase):
    kind: Literal["markdown.appended"] = "markdown.appended"
    content: str


class ThoughtAppended(UiEventBase):
    kind: Literal["thought.appended"] = "thought.appended"
    text: str
    elapsed: float | None = None


class WarningAppended(UiEventBase):
    kind: Literal["warning.appended"] = "warning.appended"
    message: str


class GuidanceSubmitted(UiEventBase):
    kind: Literal["guidance.submitted"] = "guidance.submitted"
    text: str
    truncated: bool = False


class ErrorAppended(UiEventBase):
    kind: Literal["error.appended"] = "error.appended"
    message: str


class DiffAppended(UiEventBase):
    kind: Literal["diff.appended"] = "diff.appended"
    diff_text: str
    title: str = ""


class StatusUpdated(UiEventBase):
    kind: Literal["status.updated"] = "status.updated"
    status_id: str
    label: str
    detail: str = ""
    stage: Literal[
        "analyzing",
        "thinking",
        "streaming",
        "agent_step",
        "compacting",
        "waiting_permission",
        "working",
    ] = "working"
    parent_tool_call_id: str = ""


class StatusFinished(UiEventBase):
    kind: Literal["status.finished"] = "status.finished"
    status_id: str
    label: str = ""
    detail: str = ""
    ok: bool = True
    remove: bool = True


class AssistantStreamStarted(UiEventBase):
    kind: Literal["assistant_stream.started"] = "assistant_stream.started"
    stream_id: str = "default"


class AssistantStreamUpdated(UiEventBase):
    kind: Literal["assistant_stream.updated"] = "assistant_stream.updated"
    text: str
    stream_id: str = "default"
    phase: Literal["thinking", "text"] = "text"


class AssistantStreamCommitted(UiEventBase):
    kind: Literal["assistant_stream.committed"] = "assistant_stream.committed"
    stream_id: str = "default"


class AssistantStreamDiscarded(UiEventBase):
    kind: Literal["assistant_stream.discarded"] = "assistant_stream.discarded"
    stream_id: str = "default"


class ToolStarted(UiEventBase):
    kind: Literal["tool.started"] = "tool.started"
    tool_call_id: str
    label: str
    args: str = ""
    tool_name: str = ""
    raw_args: dict[str, Any] = Field(default_factory=dict)
    display_mode: ToolDisplayMode = ToolDisplayMode.SHOW
    summary_max_lines: int = 3


class ToolFinished(UiEventBase):
    kind: Literal["tool.finished"] = "tool.finished"
    tool_call_id: str
    label: str
    elapsed: float
    ok: bool = True
    detail: str = ""


class ToolResultAppended(UiEventBase):
    kind: Literal["tool_result.appended"] = "tool_result.appended"
    tool_call_id: str = ""
    text: str
    collapsed: bool = False
    display_mode: ToolDisplayMode = ToolDisplayMode.SHOW
    summary_max_lines: int = 3


class TodoItemPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    status: TodoStatus


class TodoUpdated(UiEventBase):
    kind: Literal["todo.updated"] = "todo.updated"
    items: list[TodoItemPayload]
    summary: str


class TodoCommitted(UiEventBase):
    kind: Literal["todo.committed"] = "todo.committed"


class TodoCleared(UiEventBase):
    kind: Literal["todo.cleared"] = "todo.cleared"


class FileChangeAppended(UiEventBase):
    kind: Literal["file_change.appended"] = "file_change.appended"
    tool_call_id: str = ""
    diff_text: str


class SubagentStarted(UiEventBase):
    kind: Literal["subagent.started"] = "subagent.started"
    agent_id: int
    subagent_id: str
    name: str
    description: str = ""
    parent_agent_id: int = -1
    parent_tool_call_id: str = ""


class SubagentStepStarted(UiEventBase):
    kind: Literal["subagent_step.started"] = "subagent_step.started"
    agent_id: int
    subagent_id: str
    name: str
    step: int
    max_steps: int


class SubagentFinished(UiEventBase):
    kind: Literal["subagent.finished"] = "subagent.finished"
    agent_id: int
    subagent_id: str
    ok: bool = True
    elapsed: float | None = None
    final_step: int | None = None
    max_steps: int | None = None
    finish_reason: str = ""


class PermissionToolDetail(BaseModel):
    name: str
    pattern: str = ""
    args: dict[str, Any] = Field(default_factory=dict)


class PermissionPromptShown(UiEventBase):
    kind: Literal["permission_prompt.shown"] = "permission_prompt.shown"
    prompt: str
    choices: list[tuple[str, str, str]]
    tools: list[PermissionToolDetail] = Field(default_factory=list)


class PermissionPromptCleared(UiEventBase):
    kind: Literal["permission_prompt.cleared"] = "permission_prompt.cleared"


class InputSet(UiEventBase):
    kind: Literal["input.set"] = "input.set"
    text: str
    hints: list[tuple[str, str, bool]] = Field(default_factory=list)
    cursor_pos: int | None = None


class NoticeSet(UiEventBase):
    kind: Literal["notice.set"] = "notice.set"
    text: str


UiEvent: TypeAlias = (
    CaptureStarted
    | CaptureStopped
    | RefreshRequested
    | ResetRequested
    | TurnStarted
    | StartupShown
    | MessageAppended
    | AnsiAppended
    | MarkdownAppended
    | ThoughtAppended
    | WarningAppended
    | GuidanceSubmitted
    | ErrorAppended
    | DiffAppended
    | StatusUpdated
    | StatusFinished
    | AssistantStreamStarted
    | AssistantStreamUpdated
    | AssistantStreamCommitted
    | AssistantStreamDiscarded
    | ToolStarted
    | ToolFinished
    | ToolResultAppended
    | TodoUpdated
    | TodoCommitted
    | TodoCleared
    | FileChangeAppended
    | SubagentStarted
    | SubagentStepStarted
    | SubagentFinished
    | PermissionPromptShown
    | PermissionPromptCleared
    | InputSet
    | NoticeSet
)
