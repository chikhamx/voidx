"""Repeat detection and guidance for workflow tool calls."""

from __future__ import annotations

import json

from voidx.tools.base import ToolContext, ToolResult

REPEAT_MAX = 3
STUCK_MAX = 3


def repeat_key(action: str, node: str, condition: str = "") -> str:
    return f"{action}\x1f{node}\x1f{condition}"


def track_repeat(ctx: ToolContext, key: str) -> int:
    tracker = ctx.workflow_repeat_tracker
    entry = tracker.get(key, {"count": 0})
    entry["count"] += 1
    tracker[key] = entry
    return entry["count"]


def reset_repeat(ctx: ToolContext, key: str) -> None:
    ctx.workflow_repeat_tracker.pop(key, None)


def wrap_advance_guidance(ctx: ToolContext, result: ToolResult, key_node: str) -> ToolResult:
    """Add repeat detection to guidance for an already-satisfied advance."""
    count = track_repeat(ctx, repeat_key("advance_stuck", key_node))
    if count < 2:
        return result
    guidance = repeat_guidance(count, "advance", key_node)
    payload = json.loads(result.output)
    payload["repeat_warning"] = guidance
    if count >= STUCK_MAX:
        return ToolResult(
            title=result.title,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            summary=result.summary,
            metadata={"error": True, "reason": "repeated_workflow_advance", "guidance": guidance},
            next_step_hint=guidance,
        )
    result.output = json.dumps(payload, ensure_ascii=False, indent=2)
    result.next_step_hint = guidance
    return result


def repeat_guidance(count: int, action: str, node: str) -> str:
    if action == "advance":
        return advance_repeat_guidance(count, node)
    return enter_repeat_guidance(count, node)


def advance_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"You already advanced {node!r} with this condition. "
            "The transition succeeded — do not call advance again. "
            "Proceed with the next node's workflow steps."
        )
    return (
        f"You have called advance {node!r} {count} times with the same condition. "
        "The transition already succeeded. Stop retrying — "
        "either proceed with the next node's workflow, or summarize the blocker and ask the user."
    )


def enter_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"Node {node!r} is already active. You just called enter {node} again. "
            "Do not repeat this call — proceed with the node's workflow steps instead."
        )
    return (
        f"Node {node!r} is already active and you have called enter {node} {count} times. "
        "Stop retrying. Either advance the current node with a valid exit condition, "
        "or summarize the blocker and ask the user for input."
    )
