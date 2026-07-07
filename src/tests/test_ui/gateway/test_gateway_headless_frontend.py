from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voidx.ui.gateway.frontend import GatewayHeadlessFrontend
from voidx.ui.output.types import ThreadExecutionContext
from voidx.ui.protocol.requests import UiChoiceRequest, UiPermissionRequest, UiResponse, UiTextRequest


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
        context=ThreadExecutionContext(thread_id="t2", session_id="t2"),
    )
    await asyncio.wait_for(task, timeout=1)

    assert submitted == [("hello", "t2")]


@pytest.mark.asyncio
async def test_headless_frontend_sends_permission_request_with_thread_context():
    frontend = _frontend()
    seen_requests = []

    async def handle_request(request):
        seen_requests.append(request)
        return UiResponse(request_id=request.request_id, value="y")

    frontend.set_external_request_handler(handle_request)
    frontend._current_submit_context = ThreadExecutionContext(thread_id="t3", session_id="t3")

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

