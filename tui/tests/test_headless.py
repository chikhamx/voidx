"""Tests for headless mode (web gateway input without TUI)."""

import asyncio

import pytest

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.presentation.output.types import ThreadExecutionContext
from tui_helpers import _tui


def test_thread_context_compatibility_alias():
    from voidx.presentation.output.types import TurnExecutionContext

    assert ThreadExecutionContext is TurnExecutionContext


@pytest.mark.asyncio
async def test_run_headless_consumes_queued_input(tmp_path):
    """run_headless must consume _queue and call on_submit for each item."""
    tui = _tui(tmp_path)
    received: list[str] = []

    async def on_submit(text: str) -> bool:
        received.append(text)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("hello from gateway")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == ["hello from gateway"]


@pytest.mark.asyncio
async def test_run_headless_processes_multiple_inputs(tmp_path):
    """run_headless must keep consuming across multiple submissions."""
    tui = _tui(tmp_path)
    received: list[str] = []

    async def on_submit(text: str) -> bool:
        received.append(text)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("first")
    tui.submit_external_input("second")
    await asyncio.wait_for(asyncio.sleep(0.15), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == ["first", "second"]


@pytest.mark.asyncio
async def test_run_headless_preserves_thread_id_context(tmp_path):
    tui = _tui(tmp_path)
    received: list[tuple[str, str]] = []

    async def on_submit(text: str, *, context: ThreadExecutionContext) -> bool:
        received.append((text, context.thread_id))
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("thread scoped", thread_id="t2")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == [("thread scoped", "t2")]


@pytest.mark.asyncio
async def test_submit_external_input_fills_coding_turn_context_before_queue(tmp_path):
    tui = _tui(tmp_path)
    tui.status.session_id = lambda: "session-1"
    received: list[ThreadExecutionContext] = []

    async def on_submit(text: str, *, context: ThreadExecutionContext) -> bool:
        received.append(context)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("first turn")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert len(received) == 1
    context = received[0]
    assert context.thread_id == "session-1"
    assert context.session_id == "session-1"
    assert context.workspace == str(tmp_path)
    assert context.runtime_profile == CODING_PROFILE




@pytest.mark.asyncio
async def test_submit_external_input_uses_current_chat_session_profile(tmp_path):
    from voidx.agent.domain.prompt_policy import ChatPromptPolicy

    tui = _tui(tmp_path)
    tui.status.session_id = lambda: "chat-session"
    tui.status.runtime_profile = lambda: "chat"
    received: list[ThreadExecutionContext] = []

    async def on_submit(text: str, *, context: ThreadExecutionContext) -> bool:
        received.append(context)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))
    tui.submit_external_input("你好")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)
    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert len(received) == 1
    context = received[0]
    assert context.thread_id == "chat-session"
    assert context.session_id == "chat-session"
    assert context.runtime_profile.profile_id == "chat"
    assert isinstance(context.runtime_profile.prompt_policy, ChatPromptPolicy)
@pytest.mark.asyncio
async def test_submit_external_input_defaults_to_coding_thread_before_session_exists(tmp_path):
    tui = _tui(tmp_path)
    received: list[ThreadExecutionContext] = []

    async def on_submit(text: str, *, context: ThreadExecutionContext) -> bool:
        received.append(context)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("first turn")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert len(received) == 1
    context = received[0]
    assert context.thread_id == "coding"
    assert context.session_id == ""
    assert context.workspace == str(tmp_path)
    assert context.runtime_profile == CODING_PROFILE


def test_runtime_state_profile_lock_uses_pinned_session_snapshot(tmp_path, monkeypatch):
    from voidx.agent.application.agent_profile_loader import ProfileLoaderContext, load_profile

    resolved, _ = load_profile(
        """\
name: custom-review
revision: 3
display_name: Custom Review
workflow:
  nodes:
    - ref: review
""",
        source="project",
        context=ProfileLoaderContext(),
        expected_name="custom-review",
    )
    tui = _tui(tmp_path)
    tui.status.session_id = lambda: "session-1"
    tui.status.runtime_profile = lambda: "custom-review"
    tui.status.profile_snapshot = lambda: resolved.snapshot
    monkeypatch.setattr(
        "voidx.agent.facade.resolve_agent_profile",
        lambda *_args, **_kwargs: pytest.fail("must not re-read profile files"),
    )

    tui._lock_submit_context_for_profile("custom-review")

    context = tui._locked_submit_context
    assert context.runtime_profile.model_dump(exclude={"prompt_policy"}) == (
        resolved.runtime_profile.model_dump(exclude={"prompt_policy"})
    )
    assert type(context.runtime_profile.prompt_policy) is type(
        resolved.runtime_profile.prompt_policy
    )
    assert context.workflow_context == resolved.workflow_context
    assert context.tool_policy is not None
