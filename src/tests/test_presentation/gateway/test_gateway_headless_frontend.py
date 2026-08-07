from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.presentation.gateway.frontend import GatewayHeadlessFrontend
from voidx.presentation.protocol.requests import UiChoiceRequest, UiPermissionRequest, UiResponse, UiTextRequest


def _frontend() -> GatewayHeadlessFrontend:
    return GatewayHeadlessFrontend(SimpleNamespace(), [])


@pytest.mark.asyncio
async def test_headless_frontend_submits_with_thread_context():
    frontend = _frontend()
    submitted: list[tuple[str, str]] = []

    async def on_submit(text: str, *, context=None):
        submitted.append((text, context.thread_id))
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input(
        "hello",
        context=TurnExecutionContext(thread_id="t2", session_id="t2", runtime_profile=CODING_PROFILE),
    )
    await asyncio.wait_for(task, timeout=1)

    assert submitted == [("hello", "t2")]


@pytest.mark.asyncio
async def test_headless_frontend_fills_coding_turn_context_before_queue(tmp_path):
    frontend = GatewayHeadlessFrontend(
        SimpleNamespace(workspace=str(tmp_path), session_id=lambda: "session-1"),
        [],
    )
    submitted: list[TurnExecutionContext] = []

    async def on_submit(text: str, *, context=None):
        submitted.append(context)
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input("hello")
    await asyncio.wait_for(task, timeout=1)

    assert len(submitted) == 1
    context = submitted[0]
    assert context.thread_id == "session-1"
    assert context.session_id == "session-1"
    assert context.workspace == str(tmp_path)
    assert context.runtime_profile == CODING_PROFILE


@pytest.mark.asyncio
async def test_headless_frontend_preserves_explicit_context_values(tmp_path):
    frontend = GatewayHeadlessFrontend(
        SimpleNamespace(workspace=str(tmp_path), session_id=lambda: "status-session"),
        [],
    )
    submitted: list[TurnExecutionContext] = []
    context = TurnExecutionContext(
        thread_id="thread-2",
        session_id="session-2",
        runtime_profile=CODING_PROFILE,
        workspace="/explicit/workspace",
        tool_policy=object(),
    )

    async def on_submit(text: str, *, context=None):
        submitted.append(context)
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input("hello", context=context)
    await asyncio.wait_for(task, timeout=1)

    assert len(submitted) == 1
    queued = submitted[0]
    assert queued.thread_id == "thread-2"
    assert queued.session_id == "session-2"
    assert queued.workspace == "/explicit/workspace"
    assert queued.tool_policy is context.tool_policy


@pytest.mark.asyncio
async def test_headless_frontend_preserves_explicit_empty_session_context(tmp_path):
    frontend = GatewayHeadlessFrontend(
        SimpleNamespace(workspace=str(tmp_path), session_id=lambda: "status-session"),
        [],
    )
    submitted: list[TurnExecutionContext] = []
    context = TurnExecutionContext(
        thread_id="coding",
        session_id="",
        runtime_profile=CODING_PROFILE,
        workspace="/explicit/workspace",
        tool_policy=object(),
    )

    async def on_submit(text: str, *, context=None):
        submitted.append(context)
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input("hello", context=context)
    await asyncio.wait_for(task, timeout=1)

    assert len(submitted) == 1
    queued = submitted[0]
    assert queued.thread_id == "coding"
    assert queued.session_id == ""
    assert queued.workspace == "/explicit/workspace"
    assert queued.tool_policy is context.tool_policy


@pytest.mark.asyncio
async def test_headless_frontend_defaults_to_coding_identity_without_session(tmp_path):
    frontend = GatewayHeadlessFrontend(
        SimpleNamespace(workspace=str(tmp_path), session_id=lambda: ""),
        [],
    )
    submitted: list[TurnExecutionContext] = []

    async def on_submit(text: str, *, context=None):
        submitted.append(context)
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input("hello")
    await asyncio.wait_for(task, timeout=1)

    assert len(submitted) == 1
    queued = submitted[0]
    assert queued.thread_id == "coding"
    assert queued.session_id == ""
    assert queued.workspace == str(tmp_path)


@pytest.mark.asyncio
async def test_headless_frontend_thread_id_arg_overrides_explicit_context_thread(tmp_path):
    frontend = GatewayHeadlessFrontend(
        SimpleNamespace(workspace=str(tmp_path), session_id=lambda: "status-session"),
        [],
    )
    submitted: list[TurnExecutionContext] = []
    context = TurnExecutionContext(
        thread_id="context-thread",
        session_id="context-session",
        runtime_profile=CODING_PROFILE,
        workspace="/explicit/workspace",
    )

    async def on_submit(text: str, *, context=None):
        submitted.append(context)
        return False

    task = asyncio.create_task(frontend.run_headless(on_submit))
    frontend.submit_external_input("hello", thread_id="override-thread", context=context)
    await asyncio.wait_for(task, timeout=1)

    assert len(submitted) == 1
    queued = submitted[0]
    assert queued.thread_id == "override-thread"
    assert queued.session_id == "context-session"
    assert queued.workspace == "/explicit/workspace"

@pytest.mark.asyncio
async def test_headless_frontend_sends_permission_request_with_thread_context():
    frontend = _frontend()
    seen_requests = []

    async def handle_request(request):
        seen_requests.append(request)
        return UiResponse(request_id=request.request_id, value="y")

    frontend.set_external_request_handler(handle_request)
    frontend._current_submit_context = TurnExecutionContext(thread_id="t3", session_id="t3", runtime_profile=CODING_PROFILE)

    result = await frontend.ask_choice(
        "Allow tool use?",
        [("Yes", "y", "Allow once"), ("No", "n", "Deny")],
        details=[{"name": "edit", "pattern": "src/app.py"}],
    )

    assert result == "y"
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert isinstance(request, UiPermissionRequest)
    assert request.thread_id == "t3"
    assert request.prompt == "Allow tool use?"
    assert request.choices[0] == ("Yes", "y", "Allow once")
    assert request.tools[0].name == "edit"
    assert request.tools[0].pattern == "src/app.py"


@pytest.mark.asyncio
async def test_headless_frontend_sends_choice_and_text_requests():
    frontend = _frontend()
    seen_requests = []

    async def handle_request(request):
        seen_requests.append(request)
        return {"value": "ok"}

    frontend.set_external_request_handler(handle_request)

    assert await frontend.ask_choice("Pick", ["ok"]) == "ok"
    assert await frontend.ask_text("Type") == "ok"
    assert isinstance(seen_requests[0], UiChoiceRequest)
    assert isinstance(seen_requests[1], UiTextRequest)
