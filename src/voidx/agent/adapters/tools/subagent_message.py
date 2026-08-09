from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.schema import model_to_json_schema


class MessageInput(BaseModel):
    action: Literal["send", "receive"]
    target_run_id: str | None = None
    message_type: Literal["message", "question", "answer", "result"] = "message"
    payload: str = Field(
        default="{}",
        description="JSON object string containing the message payload, for example {\"text\": \"Need input\"}.",
    )
    limit: int = 1
    timeout: float = 0


class MessageTool:
    id = "message"
    description = "Send or receive structured messages between a child agent and its parent."

    def __init__(self, *, description: str | None = None) -> None:
        if description is not None:
            self.description = description

    def parameters_schema(self) -> dict:
        return model_to_json_schema(MessageInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw_args = args if isinstance(args, dict) else {}
        if isinstance(raw_args.get("payload"), dict):
            raw_args = {**raw_args, "payload": json.dumps(raw_args["payload"])}
        try:
            inp = MessageInput.model_validate(raw_args)
        except ValidationError as exc:
            return ToolResult(
                output=f"Message request rejected: {exc.errors()[0].get('msg', 'invalid arguments')}",
                metadata={"error": True, "validation_error": True},
            )
        gateway = ctx.runtime.subagent_transport
        run_id = ctx.runtime.run_id
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
                try:
                    payload = json.loads(inp.payload)
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if not isinstance(payload, dict):
                    return ToolResult(
                        output="Message request rejected: payload must be a JSON object.",
                        metadata={"error": True, "validation_error": True},
                    )
                message = await gateway.send(
                    sender_run_id=run_id,
                    target_run_id=target_run_id,
                    message_type=inp.message_type,
                    payload=payload,
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
