from __future__ import annotations

import pytest

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.agent.adapters.tools.context import AgentToolRuntime
from voidx.agent.adapters.tools.subagent_message import MessageTool


class RecordingGateway:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []

    def get_parent_run_id(self, _run_id: str) -> str:
        return "root:session-1"

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        raise AssertionError("invalid message type must not reach gateway")


def _ctx(gateway: RecordingGateway) -> ToolContext:
    return ToolContext(
        workspace=".",
        session_id="session-1",
        runtime=AgentToolRuntime(
            subagent_transport=gateway,
            run_id="run-child",
        ),
    )


def test_message_schema_excludes_progress():
    schema = MessageTool().parameters_schema()

    assert schema["properties"]["message_type"]["enum"] == [
        "message",
        "question",
        "answer",
        "result",
    ]


@pytest.mark.asyncio
async def test_message_tool_rejects_progress_before_gateway_send():
    gateway = RecordingGateway()

    result = await MessageTool().execute(
        {
            "action": "send",
            "message_type": "progress",
            "payload": {"step": "working"},
        },
        _ctx(gateway),
    )

    assert result.metadata == {"error": True, "validation_error": True}
    assert "rejected" in result.output
    assert gateway.send_calls == []
