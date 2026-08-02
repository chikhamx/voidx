from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class MessageInput(BaseModel):
    action: Literal["send", "receive"]
    target_run_id: str | None = None
    message_type: Literal["message", "question", "answer", "progress", "result"] = "message"
    payload: dict[str, Any] = Field(default_factory=dict)
    limit: int = 1
    timeout: float = 0


class MessageTool(BaseTool):
    id = "message"
    description = "Send or receive structured messages between a child agent and its parent."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(MessageInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = MessageInput.model_validate(args if isinstance(args, dict) else {})
        except ValidationError as exc:
            return ToolResult(
                output=f"Message request rejected: {exc.errors()[0].get('msg', 'invalid arguments')}",
                metadata={"error": True, "validation_error": True},
            )
        gateway = ctx.agent_gateway
        run_id = ctx.agent_run_id
        if gateway is None or not run_id:
            return ToolResult(
                output="Message gateway is unavailable for this agent run.",
                metadata={"error": True, "reason": "gateway_unavailable"},
            )
        try:
            if inp.action == "send":
                target_run_id = inp.target_run_id or _default_target_run_id(gateway, run_id)
                if not target_run_id:
                    return ToolResult(
                        output="Message send requires target_run_id for root agent runs.",
                        metadata={"error": True, "reason": "missing_target_run_id"},
                    )
                message = await gateway.send(
                    sender_run_id=run_id,
                    target_run_id=target_run_id,
                    message_type=inp.message_type,
                    payload=inp.payload,
                )
                return ToolResult(
                    output=f"Message sent: {message.type} to {message.target_run_id}.",
                    summary="Message sent",
                    metadata={
                        "message_id": message.message_id,
                        "message_type": message.type,
                        "target_run_id": message.target_run_id,
                    },
                )
            messages = await gateway.receive(
                run_id=run_id,
                limit=inp.limit,
                timeout=inp.timeout,
            )
            return ToolResult(
                output=_messages_output(messages),
                summary=f"Received {len(messages)} message(s)",
                metadata={
                    "count": len(messages),
                    "messages": [message.model_dump(mode="json") for message in messages],
                },
            )
        except (ValueError, TimeoutError) as exc:
            return ToolResult(
                output=f"Message request failed: {exc}",
                metadata={"error": True, "reason": "gateway_error", "detail": str(exc)[:200]},
            )


def _default_target_run_id(gateway: Any, run_id: str) -> str | None:
    return gateway.get_parent_run_id(run_id)


def _messages_output(messages) -> str:
    if not messages:
        return "No messages received."
    rows = [
        {
            "message_id": message.message_id,
            "source_run_id": message.source_run_id,
            "target_run_id": message.target_run_id,
            "type": message.type,
            "payload": message.payload,
        }
        for message in messages
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)
