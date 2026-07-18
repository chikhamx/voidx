"""Layer 1 — prune: truncate old tool outputs, omit large tool-call args.

Zero API calls.  Walks backwards through messages, truncating old tool
outputs and replacing large file-edit tool-call args with placeholders when
the corresponding tool result contains a diff (so the LLM can still see
the content via the diff).

Constants are read from the package object at call time so that tests can
monkeypatch ``voidx.llm.compaction.PRUNE_PROTECT`` etc. and have the change
take effect here.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from voidx.llm.context import count_tokens


def tool_result_has_diff(messages: list, ai_msg_index: int, tool_call_id: str) -> bool:
    """Check if the ToolMessage for a given tool_call_id contains a diff marker.

    Searches from ai_msg_index forward until the next HumanMessage (turn boundary).
    """
    for j in range(ai_msg_index + 1, len(messages)):
        msg = messages[j]
        if isinstance(msg, HumanMessage):
            break
        if hasattr(msg, "tool_call_id") and msg.tool_call_id == tool_call_id:
            content = str(getattr(msg, "content", ""))
            return "---" in content and "+++" in content
    return False


def prune_ai_tool_call_args(
    tool_calls: list[dict],
    messages: list,
    ai_msg_index: int,
) -> tuple[list[dict] | None, int]:
    """Omit large content/new_string args in file-edit tool calls.

    Returns (new_tool_calls, saved_chars). new_tool_calls is None if no changes.
    Only prunes when the corresponding tool result contains a diff
    (so the LLM can still see the content via the diff).
    """
    from voidx.llm.compaction import PRUNE_ARGS_PLACEHOLDER_DIFF

    changed = False
    saved_chars = 0
    new_tool_calls: list[dict] = []

    for tc in tool_calls:
        tc_copy = {**tc, "args": dict(tc.get("args", {}))}
        args = tc_copy["args"]
        name = tc.get("name", "")
        tc_id = tc.get("id", "")

        if name == "write" and "content" in args:
            placeholder = f"[omitted: {args['content'].count(chr(10)) + 1} lines written]"
            if len(args["content"]) > len(placeholder) and tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["content"]) - len(placeholder)
                args["content"] = placeholder
                changed = True
        elif name == "replace" and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True
        elif name == "write" and args.get("op") in ("insert", "append") and "new_string" in args:
            placeholder = PRUNE_ARGS_PLACEHOLDER_DIFF
            if len(args["new_string"]) > len(placeholder) and tool_result_has_diff(messages, ai_msg_index, tc_id):
                saved_chars += len(args["new_string"]) - len(placeholder)
                args["new_string"] = placeholder
                changed = True

        new_tool_calls.append(tc_copy)

    return (new_tool_calls if changed else None, saved_chars)


def prune_messages(messages: list) -> int:
    """Walk backwards through messages, truncating old tool outputs.

    Returns number of characters pruned.

    Rules:
    - Skip most recent 2 turns (user messages count as turn boundaries)
    - Protected tools (agent) are never pruned
    - Already compacted parts stop further pruning
    - Cumulative tool output > PRUNE_PROTECT → truncate to TOOL_OUTPUT_MAX_CHARS
    - Only prune if total pruned > PRUNE_MINIMUM
    - For previous-turn AIMessage tool_calls, omit large content/new_string args
      when the corresponding tool result contains a diff
    """
    from voidx.llm.compaction import (
        PRUNE_MINIMUM,
        PRUNE_PROTECT,
        PRUNE_PROTECTED_TOOLS,
        TOOL_OUTPUT_MAX_CHARS,
    )

    turns_seen = 0
    accumulated = 0
    pruned_chars = 0
    to_prune: list[tuple[int, str]] = []  # (msg_index, truncated_text)
    ai_to_rebuild: dict[int, list[dict]] = {}  # (msg_index, new_tool_calls)

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]

        # Count turns by user messages
        if isinstance(msg, HumanMessage):
            turns_seen += 1

        if isinstance(msg, AIMessage) and hasattr(msg, "summary") and msg.summary:
            break  # stop at compaction boundary

        # Prune AIMessage tool_calls args for previous turns
        if (
            isinstance(msg, AIMessage)
            and turns_seen >= 1
            and hasattr(msg, "tool_calls")
            and msg.tool_calls
        ):
            new_tcs, saved = prune_ai_tool_call_args(msg.tool_calls, messages, i)
            if new_tcs is not None:
                ai_to_rebuild[i] = new_tcs
                pruned_chars += saved
            continue

        # Tool messages have role="tool" and a tool_call_id
        if not hasattr(msg, "tool_call_id") or not msg.tool_call_id:
            continue

        tool_name = getattr(msg, "name", "")
        if tool_name in PRUNE_PROTECTED_TOOLS:
            continue

        content = str(getattr(msg, "content", ""))
        token_est = count_tokens(content)

        accumulated += token_est
        if accumulated <= PRUNE_PROTECT:
            continue

        # Protect most recent 2 turns from ToolMessage truncation
        if turns_seen < 2:
            continue

        if len(content) > TOOL_OUTPUT_MAX_CHARS:
            truncated = content[:TOOL_OUTPUT_MAX_CHARS] + (
                f"\n\n[Tool output truncated for context: omitted {len(content) - TOOL_OUTPUT_MAX_CHARS} chars]"
            )
            pruned_chars += len(content) - len(truncated)
            to_prune.append((i, truncated))

    if pruned_chars > PRUNE_MINIMUM:
        for idx, truncated in to_prune:
            messages[idx] = type(messages[idx])(
                content=truncated,
                tool_call_id=messages[idx].tool_call_id,
            )

    for idx, new_tcs in ai_to_rebuild.items():
        messages[idx] = messages[idx].model_copy(update={"tool_calls": new_tcs})

    return pruned_chars
