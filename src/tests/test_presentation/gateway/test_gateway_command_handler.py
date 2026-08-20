from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.application.agent_profile_loader import ProfileLoaderContext, load_profile
from voidx.agent.ports.presentation import RuntimePresentationStatus, SessionPresentationStatus
from voidx.presentation.gateway.command_handler import GatewayCommandHandler
from voidx.presentation.protocol import UiSubmitCommand


CUSTOM = """\
name: custom-review
revision: 1
display_name: Custom Review
identity: pinned identity
workflow:
  nodes:
    - ref: review
"""


class FakeApp:
    def __init__(self) -> None:
        self.context = None

    def submit_external_input(self, text, *, context) -> None:
        self.context = context

    def cancel_external_input(self, *, context) -> None:
        self.context = context


class FakeThreadRegistry:
    def __init__(self, resolved) -> None:
        self.resolved = resolved

    def ensure_thread(self, session) -> None:
        return None

    def resolved_profile(self, thread_id: str):
        assert thread_id == "custom-thread"
        return self.resolved


@pytest.mark.asyncio
async def test_submit_uses_thread_pinned_resolved_profile() -> None:
    resolved, _ = load_profile(
        CUSTOM,
        source="project",
        context=ProfileLoaderContext(),
        expected_name="custom-review",
    )
    status = RuntimePresentationStatus(
        provider="test",
        model="test",
        workspace="/workspace",
        profile_configured=True,
        session=SessionPresentationStatus(session_id="active"),
    )
    handler = GatewayCommandHandler(
        SimpleNamespace(runtime_status=lambda: status),
        SimpleNamespace(submit_guidance=lambda *args, **kwargs: True),
        FakeThreadRegistry(resolved),
    )
    app = FakeApp()

    await handler.handle(
        app,
        UiSubmitCommand(
            text="review",
            thread_id="custom-thread",
            session_id="custom-thread",
            runtime_profile="custom-review",
            workspace="/workspace",
        ),
    )

    assert app.context.runtime_profile == resolved.runtime_profile
    assert app.context.workflow_context == resolved.workflow_context
    assert app.context.tool_policy is not None
