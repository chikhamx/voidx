"""Tests for UiEventItemAdapter — converts v1 UiEvent to v2 Item notifications.

Covers the key mappings from 38 UiEvent subtypes to 7 Item kinds:
- tool.started / tool.finished → item started/completed (kind="tool")
- assistant_stream.updated → item.delta (kind="assistant_stream")
- todo.updated → item.started (kind="todo", full replace)
- message.appended / markdown.appended / etc → item.started (kind="message")
- status.updated / status.finished → item started/completed (kind="status")
- subagent.started / subagent.finished → item started/completed (kind="subagent")
- permission_prompt.shown → item.started (kind="prompt")
- turn.started → turn.started notification (not an Item)
- capture.started → capture.started notification (not an Item)
"""

from __future__ import annotations

import pytest

from voidx.agent.domain.turn_metadata import TurnMetadata
from voidx.presentation.gateway.adapter import UiEventItemAdapter
from voidx.presentation.output.events.schema import (
    AnsiAppended,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamStarted,
    AssistantStreamUpdated,
    CaptureStarted,
    CheckpointPromptShown,
    ClarifyPromptShown,
    ErrorAppended,
    FileChangeAppended,
    GuidanceCommitted,
    GuidanceSubmitted,
    MarkdownAppended,
    MessageAppended,
    PermissionPromptShown,
    PermissionToolDetail,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
    ThoughtAppended,
    TodoCleared,
    TodoCommitted,
    TodoUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TurnCompleted,
    TurnStarted,
    WarningAppended,
)
from voidx.presentation.protocol.v2.envelope import JsonRpcNotification


def _adapter() -> UiEventItemAdapter:
    return UiEventItemAdapter(thread_id="t1", turn_id="turn1")


def _method(msg) -> str:
    assert isinstance(msg, JsonRpcNotification), f"expected notification, got {type(msg)}"
    return msg.method


def _item_params(msg) -> dict:
    return msg.params


# ── direct notifications ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_shown_forwards_profile_configured():
    adapter = _adapter()
    msg = await adapter.handle(
        StartupShown(
            model="gpt-5.5",
            provider="openai",
            workspace="/tmp/voidx",
            session_title="New session",
            is_new=True,
            profile_configured=False,
        )
    )

    assert _method(msg) == "startup.shown"
    assert msg.params["model"] == "gpt-5.5"
    assert msg.params["provider"] == "openai"
    assert msg.params["profile_configured"] is False


# ── tool items ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_started_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(ToolStarted(tool_call_id="tc1", label="read file"))
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "tool"
    assert params["lifecycle"] == "started"
    assert params["item_id"]  # auto-generated
    assert params["turn_id"] == "turn1"
    assert params["thread_id"] == "t1"
    assert params["data"]["tool_call_id"] == "tc1"
    assert params["data"]["label"] == "read file"



@pytest.mark.asyncio
async def test_tool_started_forwards_raw_args_for_structured_summaries():
    adapter = _adapter()
    raw_args = {"command": "pytest -q", "cwd": "/tmp"}
    msg = await adapter.handle(
        ToolStarted(
            tool_call_id="tc-raw",
            label="run command",
            tool_name="bash",
            args='command="pytest -q", cwd="/tmp"',
            raw_args=raw_args,
        )
    )

    data = _item_params(msg)["data"]
    assert data["args"] == 'command="pytest -q", cwd="/tmp"'
    assert data["raw_args"] == raw_args

@pytest.mark.asyncio
async def test_tool_finished_to_item_completed():
    adapter = _adapter()
    # First start the tool so the adapter knows its item_id
    await adapter.handle(ToolStarted(tool_call_id="tc1", label="read file"))
    msg = await adapter.handle(
        ToolFinished(tool_call_id="tc1", label="read file", elapsed=1.5, ok=True)
    )
    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "tool"
    assert params["data"]["ok"] is True
    assert params["data"]["elapsed"] == 1.5


@pytest.mark.asyncio
async def test_tool_result_appended_updates_tool_item():
    adapter = _adapter()
    await adapter.handle(ToolStarted(tool_call_id="tc1", label="read"))
    msg = await adapter.handle(ToolResultAppended(tool_call_id="tc1", text="file contents"))
    # tool_result maps to a delta on the existing tool item
    assert _method(msg) == "item.delta"
    params = _item_params(msg)
    assert params["kind"] == "tool"
    assert "file contents" in params["data"].get("detail", "")


# ── assistant_stream items ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assistant_stream_started_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(AssistantStreamStarted(stream_id="s1"))
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "assistant_stream"


@pytest.mark.asyncio
async def test_assistant_stream_updated_to_item_delta():
    adapter = _adapter()
    await adapter.handle(AssistantStreamStarted(stream_id="s1"))
    msg = await adapter.handle(
        AssistantStreamUpdated(text="Hello world", stream_id="s1", phase="text")
    )
    assert _method(msg) == "item.delta"
    params = _item_params(msg)
    assert params["kind"] == "assistant_stream"
    # data.text is full-replace (accumulated text), not incremental
    assert params["data"]["text"] == "Hello world"
    assert params["data"]["phase"] == "text"


@pytest.mark.asyncio
async def test_assistant_stream_updated_accumulates_text():
    adapter = _adapter()
    await adapter.handle(AssistantStreamStarted(stream_id="s1"))
    await adapter.handle(AssistantStreamUpdated(text="Hello", stream_id="s1"))
    msg = await adapter.handle(AssistantStreamUpdated(text="Hello world", stream_id="s1"))
    params = _item_params(msg)
    assert params["data"]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_assistant_stream_committed_to_item_completed():
    adapter = _adapter()
    await adapter.handle(AssistantStreamStarted(stream_id="s1"))
    msg = await adapter.handle(AssistantStreamCommitted(stream_id="s1"))
    assert _method(msg) == "item.completed"
    assert _item_params(msg)["kind"] == "assistant_stream"


@pytest.mark.asyncio
async def test_assistant_stream_discarded_to_item_completed():
    adapter = _adapter()
    await adapter.handle(AssistantStreamStarted(stream_id="s1"))
    msg = await adapter.handle(AssistantStreamDiscarded(stream_id="s1"))
    assert _method(msg) == "item.completed"


# ── message items ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(MessageAppended(text="hello", style="user"))
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "message"
    assert params["data"]["text"] == "hello"
    assert params["data"]["style"] == "user"


@pytest.mark.asyncio
async def test_markdown_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(MarkdownAppended(content="# Title"))
    params = _item_params(msg)
    assert params["kind"] == "message"
    assert params["data"]["text"] == "# Title"


@pytest.mark.asyncio
async def test_ansi_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(AnsiAppended(text="\x1b[31mred\x1b[0m"))
    params = _item_params(msg)
    assert params["kind"] == "message"


@pytest.mark.asyncio
async def test_thought_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(ThoughtAppended(text="thinking...", elapsed=2.0))
    params = _item_params(msg)
    assert params["kind"] == "message"


@pytest.mark.asyncio
async def test_warning_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(WarningAppended(message="careful"))
    params = _item_params(msg)
    assert params["kind"] == "message"


@pytest.mark.asyncio
async def test_error_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(ErrorAppended(message="broke"))
    params = _item_params(msg)
    assert params["kind"] == "message"


# ── todo items ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_todo_updated_to_item_started_full_replace():
    adapter = _adapter()
    msg = await adapter.handle(
        TodoUpdated(agent_id=-1, items=[], summary="2 tasks", todo_op="write")
    )
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "todo"
    assert params["data"]["summary"] == "2 tasks"


@pytest.mark.asyncio
async def test_child_todo_updated_does_not_emit_global_todo_item():
    adapter = _adapter()

    msg = await adapter.handle(
        TodoUpdated(agent_id=0, items=[], summary="child tasks", todo_op="write")
    )

    assert msg is None


@pytest.mark.asyncio
async def test_todo_committed_to_item_completed():
    adapter = _adapter()
    msg = await adapter.handle(TodoCommitted())
    assert _method(msg) == "item.completed"
    assert _item_params(msg)["kind"] == "todo"


@pytest.mark.asyncio
async def test_todo_cleared_to_item_completed():
    adapter = _adapter()
    msg = await adapter.handle(TodoCleared())
    assert _method(msg) == "item.completed"


# ── status items ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_updated_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(
        StatusUpdated(status_id="s1", label="Working", detail="reading files")
    )
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "status"
    assert params["data"]["status_id"] == "s1"


@pytest.mark.asyncio
async def test_status_finished_to_item_completed():
    adapter = _adapter()
    await adapter.handle(StatusUpdated(status_id="s1", label="Working"))
    msg = await adapter.handle(StatusFinished(status_id="s1"))
    assert _method(msg) == "item.completed"
    assert _item_params(msg)["kind"] == "status"


# ── subagent items ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_started_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(
        SubagentStarted(agent_id=1, subagent_id="sub1", name="reviewer")
    )
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "subagent"
    assert params["data"]["subagent_id"] == "sub1"
    assert params["data"]["name"] == "reviewer"


@pytest.mark.asyncio
async def test_subagent_finished_to_item_completed():
    adapter = _adapter()
    await adapter.handle(
        SubagentStarted(agent_id=1, subagent_id="sub1", name="reviewer")
    )
    msg = await adapter.handle(
        SubagentFinished(agent_id=1, subagent_id="sub1", ok=True, elapsed=5.0)
    )
    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "subagent"
    assert params["data"]["ok"] is True




@pytest.mark.asyncio
async def test_subagent_finished_forwards_error():
    adapter = _adapter()
    await adapter.handle(
        SubagentStarted(agent_id=1, subagent_id="sub1", name="reviewer")
    )
    msg = await adapter.handle(
        SubagentFinished(
            agent_id=1,
            subagent_id="sub1",
            ok=False,
            finish_reason="error",
            error="runner failed",
        )
    )
    assert _item_params(msg)["data"]["error"] == "runner failed"
# ── prompt items ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_prompt_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(
        PermissionPromptShown(prompt="Allow file write?", choices=[("allow", "Allow", "")])
    )
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "permission"


@pytest.mark.asyncio
async def test_permission_prompt_forwards_risk_scope_tool_details():
    adapter = _adapter()
    msg = await adapter.handle(
        PermissionPromptShown(
            prompt="Allow tool?",
            choices=[("Do not run", "n", "Blocked")],
            tools=[
                PermissionToolDetail(
                    name="bash",
                    pattern="sudo true",
                    args={"command": "sudo true"},
                    risk={
                        "level": "blocked",
                        "tags": ["privilege_escalation"],
                        "reason": "sudo is blocked",
                        "tool_name": "bash",
                        "pattern": "sudo true",
                    },
                    allowed_scopes=("once",),
                    default_scope="once",
                )
            ],
        )
    )

    data = _item_params(msg)["data"]
    assert data["tools"] == [
        {
            "name": "bash",
            "pattern": "sudo true",
            "args": {"command": "sudo true"},
            "risk": {
                "level": "blocked",
                "tags": ["privilege_escalation"],
                "reason": "sudo is blocked",
                "tool_name": "bash",
                "pattern": "sudo true",
            },
            "allowed_scopes": ("once",),
            "default_scope": "once",
        }
    ]


@pytest.mark.asyncio
async def test_checkpoint_prompt_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(
        CheckpointPromptShown(
            checkpoint_id="cp1",
            plan={
                "goal": "do x",
                "plan_summary": "do x",
                "steps": [],
                "affected_files": [],
                "risks": [],
            },
        )
    )
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "checkpoint"


@pytest.mark.asyncio
async def test_clarify_prompt_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(
        ClarifyPromptShown(clarify_id="cl1", question="Which option?", options=["a", "b"])
    )
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "clarify"


# ── non-Item notifications ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_started_to_turn_started_notification():
    adapter = _adapter()
    metadata = TurnMetadata(profile_id="loop", protocol="loop", category="loop")
    msg = await adapter.handle(TurnStarted(text="hello", metadata=metadata))
    assert _method(msg) == "turn.started"
    params = _item_params(msg)
    assert params["thread_id"] == "t1"
    assert params["turn_id"] == "turn1"
    assert params["text"] == "hello"
    assert params["metadata"] == {
        "profile_id": "loop",
        "protocol": "loop",
        "category": "loop",
    }


@pytest.mark.asyncio
async def test_capture_started_to_capture_notification():
    adapter = _adapter()
    msg = await adapter.handle(CaptureStarted())
    assert _method(msg) == "capture.started"


@pytest.mark.asyncio
async def test_file_change_appended_updates_tool_item():
    adapter = _adapter()
    await adapter.handle(ToolStarted(tool_call_id="tc1", label="write"))
    msg = await adapter.handle(
        FileChangeAppended(tool_call_id="tc1", diff_text="@@ diff @@")
    )
    assert _method(msg) == "item.delta"
    params = _item_params(msg)
    assert params["kind"] == "tool"


# ── previously missing event mappings (added in review fix) ────────────

from voidx.presentation.output.events.schema import (  # noqa: E402
    CheckpointDecisionSubmitted,
    ClarifyAnswerSubmitted,
    DiffAppended,
    PermissionPromptCleared,
    SubagentStepStarted,
)


@pytest.mark.asyncio
async def test_diff_appended_to_item_started():
    adapter = _adapter()
    msg = await adapter.handle(DiffAppended(diff_text="@@ diff @@", title="file.py"))
    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "message"
    assert params["data"]["style"] == "diff"
    assert params["data"]["text"] == "@@ diff @@"
    assert params["data"]["title"] == "file.py"


@pytest.mark.asyncio
async def test_subagent_step_started_to_item_delta():
    adapter = _adapter()
    await adapter.handle(
        SubagentStarted(agent_id=1, subagent_id="sub1", name="reviewer")
    )
    msg = await adapter.handle(
        SubagentStepStarted(agent_id=1, subagent_id="sub1", name="reviewer")
    )
    assert _method(msg) == "item.delta"
    params = _item_params(msg)
    assert params["kind"] == "subagent"
    assert params["data"]["step"] is True


@pytest.mark.asyncio
async def test_permission_prompt_cleared_to_item_completed():
    adapter = _adapter()
    msg = await adapter.handle(PermissionPromptCleared())
    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "permission"
    assert params["data"]["cleared"] is True



@pytest.mark.asyncio
async def test_permission_prompt_lifecycle_forwards_request_id():
    adapter = _adapter()
    shown = await adapter.handle(
        PermissionPromptShown(
            request_id="permission-1",
            prompt="Allow tool?",
            choices=[("allow", "y", "Allow once")],
        )
    )
    cleared = await adapter.handle(PermissionPromptCleared(request_id="permission-1"))

    assert _item_params(shown)["data"]["request_id"] == "permission-1"
    assert _item_params(cleared)["data"]["request_id"] == "permission-1"

@pytest.mark.asyncio
async def test_checkpoint_decision_submitted_to_item_completed():
    adapter = _adapter()
    msg = await adapter.handle(
        CheckpointDecisionSubmitted(checkpoint_id="cp1", decision="approve", label="Approve")
    )
    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "checkpoint"
    assert params["data"]["decision"] == "approve"


@pytest.mark.asyncio
async def test_clarify_answer_submitted_to_item_completed():
    adapter = _adapter()
    msg = await adapter.handle(
        ClarifyAnswerSubmitted(clarify_id="cl1", answer="option a", cancelled=False)
    )
    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "prompt"
    assert params["data"]["prompt_type"] == "clarify"
    assert params["data"]["answer"] == "option a"
    assert params["data"]["cancelled"] is False


@pytest.mark.asyncio
async def test_guidance_submitted_renders_as_guidance_preview_item():
    adapter = _adapter()

    msg = await adapter.handle(GuidanceSubmitted(text="keep going"))

    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "guidance_preview"
    assert params["data"] == {"text": "keep going", "truncated": False}


@pytest.mark.asyncio
async def test_guidance_committed_renders_as_completed_guidance_preview():
    adapter = _adapter()

    msg = await adapter.handle(GuidanceCommitted())

    assert _method(msg) == "item.completed"
    params = _item_params(msg)
    assert params["kind"] == "guidance_preview"


@pytest.mark.asyncio
async def test_guidance_message_appended_renders_exactly_once():
    adapter = _adapter()

    msg = await adapter.handle(MessageAppended(text="keep going", style="guidance"))

    assert _method(msg) == "item.started"
    params = _item_params(msg)
    assert params["kind"] == "message"
    assert params["data"] == {"text": "keep going", "style": "guidance"}


@pytest.mark.asyncio
async def test_empty_turn_id_is_generated_and_shared_by_stream_lifecycle():
    adapter = UiEventItemAdapter(thread_id="t1", turn_id="")

    turn_started = await adapter.handle(TurnStarted(text="hello"))
    first_turn_id = turn_started.params["turn_id"]
    assert first_turn_id

    notifications = [
        await adapter.handle(AssistantStreamStarted(stream_id="s1")),
        await adapter.handle(
            AssistantStreamUpdated(
                stream_id="s1", text="thinking...", phase="thinking"
            )
        ),
        await adapter.handle(
            AssistantStreamUpdated(
                stream_id="s1", text="● answer", phase="text"
            )
        ),
        await adapter.handle(AssistantStreamCommitted(stream_id="s1")),
    ]
    assert all(notification.params["turn_id"] == first_turn_id for notification in notifications)

    turn_completed = await adapter.handle(TurnCompleted())
    assert turn_completed.params["turn_id"] == first_turn_id

    next_turn_started = await adapter.handle(TurnStarted(text="next"))
    assert next_turn_started.params["turn_id"]
    assert next_turn_started.params["turn_id"] != first_turn_id
