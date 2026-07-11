"""UiEventItemAdapter — converts v1 UiEvent to v2 Item notifications.

The agent core emits UiEvent (38 subtypes). This adapter maps them to the v2
protocol's 7 Item kinds with started/delta/completed lifecycle, plus a set of
non-Item notifications (turn.started, capture.started, etc.).

The agent core is untouched; all protocol translation happens here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from voidx.ui.output.events.schema import (
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
    SubagentFinished,
    SubagentStarted,
    SubagentStepStarted,
    ThoughtAppended,
    TodoCleared,
    TodoCommitted,
    TodoUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UiEvent,
    WarningAppended,
)
from voidx.ui.protocol.v2.envelope import JsonRpcNotification


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class UiEventItemAdapter:
    """Converts UiEventBus events into v2 Item notifications.

    Maintains internal state to correlate started/finished event pairs into
    a single Item with lifecycle transitions (e.g. tool.started → item.started,
    tool.finished → item.completed, sharing the same item_id).
    """

    def __init__(self, thread_id: str, turn_id: str) -> None:
        self._thread_id = thread_id
        self._turn_id = turn_id
        # tool_call_id → item_id (for correlating tool started/finished/result)
        self._tool_items: dict[str, str] = {}
        # stream_id → (item_id, accumulated_text)
        self._stream_items: dict[str, tuple[str, str]] = {}
        # subagent_id → item_id
        self._subagent_items: dict[str, str] = {}
        # status_id → item_id
        self._status_items: dict[str, str] = {}

    async def handle(self, event: UiEvent) -> JsonRpcNotification | None:
        """Convert a UiEvent to a v2 notification. Returns None if unmapped.

        Most UiEvent subtypes are mapped. GuidanceSubmitted maps to a
        guidance_preview item (started); GuidanceCommitted maps to
        guidance_preview (completed).
        None is retained as a defensive return for any future event types
        added to UiEvent without updating this adapter. Callers should filter
        None before broadcasting.
        """
        handler = _HANDLERS.get(type(event))
        if handler is None:
            return None
        return handler(self, event)

    # ── Item builders ────────────────────────────────────────────────────

    def _item_notification(
        self,
        item_id: str,
        kind: str,
        lifecycle: str,
        data: dict[str, Any],
    ) -> JsonRpcNotification:
        return JsonRpcNotification(
            method=f"item.{lifecycle}",
            params={
                "item_id": item_id,
                "turn_id": self._turn_id,
                "thread_id": self._thread_id,
                "kind": kind,
                "lifecycle": lifecycle,
                "data": data,
            },
        )

    def _notification(self, method: str, params: dict[str, Any]) -> JsonRpcNotification:
        return JsonRpcNotification(method=method, params=params)

    # ── tool ─────────────────────────────────────────────────────────────

    def _on_tool_started(self, event: ToolStarted) -> JsonRpcNotification:
        item_id = _uid()
        self._tool_items[event.tool_call_id] = item_id
        return self._item_notification(
            item_id,
            "tool",
            "started",
            {
                "tool_call_id": event.tool_call_id,
                "label": event.label,
                "args": event.args,
                "tool_name": event.tool_name,
            },
        )

    def _on_tool_finished(self, event: ToolFinished) -> JsonRpcNotification:
        item_id = self._tool_items.get(event.tool_call_id, _uid())
        return self._item_notification(
            item_id,
            "tool",
            "completed",
            {
                "tool_call_id": event.tool_call_id,
                "label": event.label,
                "elapsed": event.elapsed,
                "ok": event.ok,
                "detail": event.detail,
            },
        )

    def _on_tool_result(self, event: ToolResultAppended) -> JsonRpcNotification:
        item_id = self._tool_items.get(event.tool_call_id, _uid())
        return self._item_notification(
            item_id,
            "tool",
            "delta",
            {"tool_call_id": event.tool_call_id, "detail": event.text},
        )

    def _on_file_change(self, event: FileChangeAppended) -> JsonRpcNotification:
        item_id = self._tool_items.get(event.tool_call_id, _uid())
        return self._item_notification(
            item_id,
            "tool",
            "delta",
            {"tool_call_id": event.tool_call_id, "diff_text": event.diff_text},
        )

    # ── assistant_stream ─────────────────────────────────────────────────

    def _on_stream_started(self, event: AssistantStreamStarted) -> JsonRpcNotification:
        item_id = _uid()
        self._stream_items[event.stream_id] = (item_id, "")
        return self._item_notification(
            item_id, "assistant_stream", "started", {"text": "", "phase": "text"}
        )

    def _on_stream_updated(self, event: AssistantStreamUpdated) -> JsonRpcNotification:
        existing = self._stream_items.get(event.stream_id)
        if existing is not None:
            item_id, _ = existing
            # full-replace: data.text is the current complete text
            self._stream_items[event.stream_id] = (item_id, event.text)
        else:
            item_id = _uid()
            self._stream_items[event.stream_id] = (item_id, event.text)
        return self._item_notification(
            item_id,
            "assistant_stream",
            "delta",
            {"text": event.text, "phase": event.phase},
        )

    def _on_stream_committed(self, event: AssistantStreamCommitted) -> JsonRpcNotification:
        item_id = self._stream_items.get(event.stream_id, (_uid(), ""))[0]
        return self._item_notification(
            item_id, "assistant_stream", "completed", {}
        )

    def _on_stream_discarded(self, event: AssistantStreamDiscarded) -> JsonRpcNotification:
        item_id = self._stream_items.get(event.stream_id, (_uid(), ""))[0]
        return self._item_notification(
            item_id, "assistant_stream", "completed", {"discarded": True}
        )

    # ── message ──────────────────────────────────────────────────────────

    def _on_message(self, event: MessageAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started", {"text": event.text, "style": event.style}
        )

    def _on_markdown(self, event: MarkdownAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started", {"text": event.content, "style": "markdown"}
        )

    def _on_ansi(self, event: AnsiAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started", {"text": event.text, "style": "ansi"}
        )

    def _on_thought(self, event: ThoughtAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started",
            {"text": event.text, "style": "thought", "elapsed": event.elapsed},
        )

    def _on_warning(self, event: WarningAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started", {"text": event.message, "style": "warning"}
        )

    def _on_error(self, event: ErrorAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started", {"text": event.message, "style": "error"}
        )

    def _on_diff_appended(self, event: DiffAppended) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "message", "started",
            {"text": event.diff_text, "style": "diff", "title": event.title},
        )

    # ── guidance ─────────────────────────────────────────────────────────

    def _on_guidance_submitted(self, event: GuidanceSubmitted) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "guidance_preview", "started",
            {"text": event.text, "truncated": event.truncated},
        )

    def _on_guidance_committed(self, event: GuidanceCommitted) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "guidance_preview", "completed", {},
        )

    # ── todo ─────────────────────────────────────────────────────────────

    def _on_todo_updated(self, event: TodoUpdated) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "todo",
            "started",
            {
                "items": [item.model_dump() for item in event.items],
                "summary": event.summary,
                "todo_op": event.todo_op,
            },
        )

    def _on_todo_committed(self, event: TodoCommitted) -> JsonRpcNotification:
        return self._item_notification(_uid(), "todo", "completed", {})

    def _on_todo_cleared(self, event: TodoCleared) -> JsonRpcNotification:
        return self._item_notification(_uid(), "todo", "completed", {"cleared": True})

    # ── status ───────────────────────────────────────────────────────────

    def _on_status_updated(self, event: StatusUpdated) -> JsonRpcNotification:
        item_id = _uid()
        self._status_items[event.status_id] = item_id
        return self._item_notification(
            item_id,
            "status",
            "started",
            {
                "status_id": event.status_id,
                "label": event.label,
                "detail": event.detail,
                "stage": event.stage,
            },
        )

    def _on_status_finished(self, event: StatusFinished) -> JsonRpcNotification:
        item_id = self._status_items.get(event.status_id, _uid())
        return self._item_notification(
            item_id,
            "status",
            "completed",
            {
                "status_id": event.status_id,
                "label": event.label,
                "detail": event.detail,
                "ok": event.ok,
            },
        )

    # ── subagent ─────────────────────────────────────────────────────────

    def _on_subagent_started(self, event: SubagentStarted) -> JsonRpcNotification:
        item_id = _uid()
        self._subagent_items[event.subagent_id] = item_id
        return self._item_notification(
            item_id,
            "subagent",
            "started",
            {
                "subagent_id": event.subagent_id,
                "name": event.name,
                "description": event.description,
            },
        )

    def _on_subagent_finished(self, event: SubagentFinished) -> JsonRpcNotification:
        item_id = self._subagent_items.get(event.subagent_id, _uid())
        return self._item_notification(
            item_id,
            "subagent",
            "completed",
            {
                "subagent_id": event.subagent_id,
                "ok": event.ok,
                "elapsed": event.elapsed,
                "summary": event.summary,
            },
        )

    def _on_subagent_step_started(self, event: SubagentStepStarted) -> JsonRpcNotification:
        item_id = self._subagent_items.get(event.subagent_id, _uid())
        return self._item_notification(
            item_id,
            "subagent",
            "delta",
            {"subagent_id": event.subagent_id, "name": event.name, "step": True},
        )

    # ── prompt ───────────────────────────────────────────────────────────

    def _on_permission_prompt(self, event: PermissionPromptShown) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "prompt",
            "started",
            {
                "prompt_type": "permission",
                "interactive": False,
                "prompt": event.prompt,
                "choices": event.choices,
                "tools": [t.model_dump() for t in event.tools],
            },
        )

    def _on_checkpoint_prompt(self, event: CheckpointPromptShown) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "prompt",
            "started",
            {
                "prompt_type": "checkpoint",
                "checkpoint_id": event.checkpoint_id,
                "plan": event.plan.model_dump(),
                "choices": [c.model_dump() for c in event.choices],
            },
        )

    def _on_clarify_prompt(self, event: ClarifyPromptShown) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "prompt",
            "started",
            {
                "prompt_type": "clarify",
                "clarify_id": event.clarify_id,
                "question": event.question,
                "options": event.options,
            },
        )

    def _on_permission_prompt_cleared(self, event: PermissionPromptCleared) -> JsonRpcNotification:
        return self._item_notification(
            _uid(), "prompt", "completed", {"prompt_type": "permission", "cleared": True},
        )

    def _on_checkpoint_decision(self, event: CheckpointDecisionSubmitted) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "prompt",
            "completed",
            {
                "prompt_type": "checkpoint",
                "checkpoint_id": event.checkpoint_id,
                "decision": event.decision,
                "label": event.label,
            },
        )

    def _on_clarify_answer(self, event: ClarifyAnswerSubmitted) -> JsonRpcNotification:
        return self._item_notification(
            _uid(),
            "prompt",
            "completed",
            {
                "prompt_type": "clarify",
                "clarify_id": event.clarify_id,
                "answer": event.answer,
                "cancelled": event.cancelled,
            },
        )

    # ── non-Item notifications ───────────────────────────────────────────

    def _on_turn_started(self, event: TurnStarted) -> JsonRpcNotification:
        return self._notification(
            "turn.started",
            {
                "thread_id": event.thread_id or self._thread_id,
                "turn_id": self._turn_id,
                "text": event.text,
            },
        )

    def _on_turn_completed(self, event: TurnCompleted) -> JsonRpcNotification:
        return self._notification(
            "turn.completed",
            {
                "thread_id": event.thread_id or self._thread_id,
                "turn_id": self._turn_id,
            },
        )

    def _on_turn_failed(self, event: TurnFailed) -> JsonRpcNotification:
        return self._notification(
            "turn.failed",
            {
                "thread_id": event.thread_id or self._thread_id,
                "turn_id": self._turn_id,
                "message": event.message,
            },
        )

    def _on_turn_cancelled(self, event: TurnCancelled) -> JsonRpcNotification:
        return self._notification(
            "turn.cancelled",
            {
                "thread_id": event.thread_id or self._thread_id,
                "turn_id": self._turn_id,
            },
        )

    def _on_capture_started(self, event: CaptureStarted) -> JsonRpcNotification:
        return self._notification("capture.started", {})

    def _on_capture_stopped(self, event: CaptureStopped) -> JsonRpcNotification:
        return self._notification("capture.stopped", {})

    def _on_refresh_requested(self, event: RefreshRequested) -> JsonRpcNotification:
        return self._notification("refresh.requested", {})

    def _on_reset_requested(self, event: ResetRequested) -> JsonRpcNotification:
        return self._notification("reset.requested", {})

    def _on_startup_shown(self, event: StartupShown) -> JsonRpcNotification:
        return self._notification(
            "startup.shown",
            {
                "model": event.model,
                "provider": event.provider,
                "workspace": event.workspace,
                "session_title": event.session_title,
                "is_new": event.is_new,
                "profile_configured": event.profile_configured,
            },
        )

    def _on_input_set(self, event: InputSet) -> JsonRpcNotification:
        return self._notification(
            "input.set",
            {
                "text": event.text,
                "hints": event.hints,
                "cursor_pos": event.cursor_pos,
            },
        )

    def _on_notice_set(self, event: NoticeSet) -> JsonRpcNotification:
        return self._notification("notice.set", {"text": event.text})


# Handler dispatch table: UiEvent subclass → adapter method
_HANDLERS: dict[type, Callable[[UiEventItemAdapter, UiEvent], JsonRpcNotification]] = {
    # tool
    ToolStarted: UiEventItemAdapter._on_tool_started,
    ToolFinished: UiEventItemAdapter._on_tool_finished,
    ToolResultAppended: UiEventItemAdapter._on_tool_result,
    FileChangeAppended: UiEventItemAdapter._on_file_change,
    # assistant_stream
    AssistantStreamStarted: UiEventItemAdapter._on_stream_started,
    AssistantStreamUpdated: UiEventItemAdapter._on_stream_updated,
    AssistantStreamCommitted: UiEventItemAdapter._on_stream_committed,
    AssistantStreamDiscarded: UiEventItemAdapter._on_stream_discarded,
    # message
    MessageAppended: UiEventItemAdapter._on_message,
    MarkdownAppended: UiEventItemAdapter._on_markdown,
    AnsiAppended: UiEventItemAdapter._on_ansi,
    ThoughtAppended: UiEventItemAdapter._on_thought,
    WarningAppended: UiEventItemAdapter._on_warning,
    ErrorAppended: UiEventItemAdapter._on_error,
    DiffAppended: UiEventItemAdapter._on_diff_appended,
    # guidance
    GuidanceSubmitted: UiEventItemAdapter._on_guidance_submitted,
    GuidanceCommitted: UiEventItemAdapter._on_guidance_committed,
    # todo
    TodoUpdated: UiEventItemAdapter._on_todo_updated,
    TodoCommitted: UiEventItemAdapter._on_todo_committed,
    TodoCleared: UiEventItemAdapter._on_todo_cleared,
    # status
    StatusUpdated: UiEventItemAdapter._on_status_updated,
    StatusFinished: UiEventItemAdapter._on_status_finished,
    # subagent
    SubagentStarted: UiEventItemAdapter._on_subagent_started,
    SubagentStepStarted: UiEventItemAdapter._on_subagent_step_started,
    SubagentFinished: UiEventItemAdapter._on_subagent_finished,
    # prompt
    PermissionPromptShown: UiEventItemAdapter._on_permission_prompt,
    PermissionPromptCleared: UiEventItemAdapter._on_permission_prompt_cleared,
    CheckpointPromptShown: UiEventItemAdapter._on_checkpoint_prompt,
    CheckpointDecisionSubmitted: UiEventItemAdapter._on_checkpoint_decision,
    ClarifyPromptShown: UiEventItemAdapter._on_clarify_prompt,
    ClarifyAnswerSubmitted: UiEventItemAdapter._on_clarify_answer,
    # non-Item notifications
    TurnStarted: UiEventItemAdapter._on_turn_started,
    TurnCompleted: UiEventItemAdapter._on_turn_completed,
    TurnFailed: UiEventItemAdapter._on_turn_failed,
    TurnCancelled: UiEventItemAdapter._on_turn_cancelled,
    CaptureStarted: UiEventItemAdapter._on_capture_started,
    CaptureStopped: UiEventItemAdapter._on_capture_stopped,
    RefreshRequested: UiEventItemAdapter._on_refresh_requested,
    ResetRequested: UiEventItemAdapter._on_reset_requested,
    StartupShown: UiEventItemAdapter._on_startup_shown,
    InputSet: UiEventItemAdapter._on_input_set,
    NoticeSet: UiEventItemAdapter._on_notice_set,
}
