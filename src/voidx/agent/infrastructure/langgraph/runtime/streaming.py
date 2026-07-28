"""LLM streaming helpers for agent execution."""

from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from voidx.agent.application.todo_state import _DSML_MARKER_RE, sanitize_todo_replay_messages
from voidx.agent.application.tool_call_ids import ai_tool_call_ids
from voidx.agent.application.tool_exchange_sanitizer import sanitize_failed_tool_exchanges
from voidx.runtime.ui_port import AgentUiPort, runtime_ui_port

_PROTOCOL_DEEPSEEK = "deepseek"

_REPLAY_UNSAFE_BLOCK_TYPES = {
    "thinking",
    "redacted_thinking",
    "reasoning",
    "reasoning_content",
}
_DSML_TOOL_CALLS_RE = re.compile(
    rf"<{_DSML_MARKER_RE}tool_calls\b[^>]*>.*?</{_DSML_MARKER_RE}tool_calls>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER_RE}invoke\b([^>]*)>(.*?)</{_DSML_MARKER_RE}invoke>",
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<{_DSML_MARKER_RE}parameter\b([^>]*)>(.*?)</{_DSML_MARKER_RE}parameter>",
    re.DOTALL,
)
_DSML_ATTR_RE = re.compile(r'([A-Za-z_][\w:-]*)="([^"]*)"')
_DSML_BOILERPLATE_RE = re.compile(
    r"(commands?\s*(列表|list).*(注册|register)|注册.*commands?\s*(列表|list))",
    re.IGNORECASE,
)
_LEGACY_XML_TOOL_CALL_RE = re.compile(
    r"<tool_call\b[^>]*>.*?</tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_LEGACY_XML_ARG_PAIR_RE = re.compile(
    r"<arg_key\b[^>]*>(.*?)</arg_key>\s*<arg_value\b[^>]*>(.*?)</arg_value>",
    re.DOTALL | re.IGNORECASE,
)
_LEGACY_XML_NAME_ATTR_RE = re.compile(r'\bname="([^"]+)"', re.IGNORECASE)
MALFORMED_TOOL_CALL_METADATA_KEY = "malformed_tool_call"


async def stream_llm(
    model,
    messages: list,
    renderer: Any,
    protocol: str = "",
    ui_port: AgentUiPort = runtime_ui_port,
) -> AIMessage:
    """Stream LLM response, render live, return merged AIMessage."""
    from voidx.llm.service import extract_thinking

    chunks: list[AIMessageChunk] = []
    renderer.start()

    try:
        async for raw_chunk in model.astream(_sanitize_messages_for_replay(messages, protocol=protocol)):
            thinking = extract_thinking(raw_chunk, protocol)
            content = _stream_visible_content(raw_chunk.content, thinking)
            chunk = (
                raw_chunk
                if content == raw_chunk.content
                else raw_chunk.model_copy(update={"content": content})
            )
            chunks.append(chunk)
            if thinking:
                renderer.feed_thinking(thinking)
            _render_stream_content(renderer, content)
    except Exception:
        renderer.discard()
        raise
    finally:
        renderer.done()
        if ui_port.events.is_running:
            await ui_port.events.drain()

    if not chunks:
        return AIMessage(content="")

    merged = chunks[0] + chunks[1:] if len(chunks) > 1 else chunks[0]

    content_without_dsml, dsml_tool_calls = _extract_dsml_tool_calls(merged.content)
    _, legacy_xml_tool_calls = _extract_legacy_xml_tool_calls(content_without_dsml)
    tool_calls = merged.tool_calls or [*dsml_tool_calls, *legacy_xml_tool_calls]
    malformed_tool_call_format = "" if tool_calls else _malformed_tool_call_format(merged.content)
    content = "" if malformed_tool_call_format else _sanitize_ai_content_for_replay(merged.content)
    response_metadata = dict(merged.response_metadata or {})
    if malformed_tool_call_format:
        response_metadata[MALFORMED_TOOL_CALL_METADATA_KEY] = True
        response_metadata["malformed_tool_call_format"] = malformed_tool_call_format
    kwargs = {
        "content": content,
        "tool_calls": tool_calls,
        "response_metadata": response_metadata,
        "additional_kwargs": merged.additional_kwargs,
    }
    usage_metadata = getattr(merged, "usage_metadata", None)
    if usage_metadata:
        kwargs["usage_metadata"] = usage_metadata
    return AIMessage(**kwargs)


def is_malformed_tool_call_response(message: object) -> bool:
    metadata = getattr(message, "response_metadata", None)
    return bool(isinstance(metadata, dict) and metadata.get(MALFORMED_TOOL_CALL_METADATA_KEY))


def _sanitize_messages_for_replay(messages: list, *, protocol: str = "") -> list:
    """Remove reasoning-only blocks before replaying assistant history.

    For deepseek protocol, reasoning_content/thinking blocks are preserved
    because the DeepSeek API requires them to be passed back in multi-turn
    conversations when thinking mode is enabled.
    """
    sanitized = []
    for message in messages:
        if isinstance(message, AIMessage):
            content = _sanitize_ai_content_for_replay(message.content, protocol=protocol)
            if _is_empty_content(content) and not getattr(message, "tool_calls", None):
                continue
            if content != message.content:
                sanitized.append(message.model_copy(update={"content": content}))
                continue
        sanitized.append(message)
    sanitized = sanitize_todo_replay_messages(
        sanitized,
        preserve_latest_tool_exchange=True,
    )
    sanitized = sanitize_failed_tool_exchanges(sanitized, preserve_latest=True, preserve_rounds=2)
    return _repair_tool_result_adjacency(sanitized)


def _repair_tool_result_adjacency(messages: list) -> list:
    repaired = []
    i = 0

    while i < len(messages):
        message = messages[i]
        if isinstance(message, ToolMessage):
            i += 1
            continue

        repaired.append(message)
        if not isinstance(message, AIMessage):
            i += 1
            continue

        tool_call_ids = ai_tool_call_ids(message)
        if not tool_call_ids:
            i += 1
            continue

        seen: set[str] = set()
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            tool_message = messages[j]
            tool_call_id = str(getattr(tool_message, "tool_call_id", "") or "")
            if tool_call_id in tool_call_ids and tool_call_id not in seen:
                repaired.append(tool_message)
                seen.add(tool_call_id)
            j += 1

        for tool_call_id in tool_call_ids:
            if tool_call_id not in seen:
                repaired.append(ToolMessage(
                    content="Tool result unavailable: previous tool call was not executed.",
                    tool_call_id=tool_call_id,
                    status="error",
                ))

        i = j

    return repaired

def _sanitize_ai_content_for_replay(content: object, *, protocol: str = "") -> object:
    if isinstance(content, str):
        cleaned, _ = _extract_dsml_tool_calls_from_text(content)
        cleaned, _ = _extract_legacy_xml_tool_calls_from_text(cleaned)
        return cleaned
    if not isinstance(content, list):
        return content

    blocks: list[object] = []
    text_parts: list[str] = []
    has_non_text = False

    def flush_text() -> None:
        if text_parts:
            blocks.append({"type": "text", "text": "".join(text_parts)})
            text_parts.clear()

    for item in content:
        if isinstance(item, str):
            if item:
                cleaned, _ = _extract_dsml_tool_calls_from_text(item)
                cleaned, _ = _extract_legacy_xml_tool_calls_from_text(cleaned)
                if cleaned:
                    text_parts.append(cleaned)
            continue
        if not isinstance(item, dict):
            if item is not None:
                text_parts.append(str(item))
            continue

        block_type = item.get("type")
        if block_type in _REPLAY_UNSAFE_BLOCK_TYPES:
            if protocol == _PROTOCOL_DEEPSEEK:
                blocks.append(item)
                has_non_text = True
                continue
            continue
        if block_type == "text":
            text = item.get("text", "")
            if isinstance(text, str) and text:
                cleaned, _ = _extract_dsml_tool_calls_from_text(text)
                cleaned, _ = _extract_legacy_xml_tool_calls_from_text(cleaned)
                if cleaned:
                    text_parts.append(cleaned)
            continue

        flush_text()
        blocks.append(item)
        has_non_text = True

    if not has_non_text:
        return "".join(text_parts)

    flush_text()
    return blocks


def _stream_visible_content(content: object, thinking: str) -> object:
    if isinstance(content, str):
        return _strip_duplicate_thinking_text(content, thinking)
    if not isinstance(content, list):
        return content

    blocks: list[object] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in _REPLAY_UNSAFE_BLOCK_TYPES:
            continue
        if isinstance(item, str):
            visible = _strip_duplicate_thinking_text(item, thinking)
            if visible:
                blocks.append(visible)
            continue
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                visible = _strip_duplicate_thinking_text(text, thinking)
                if visible:
                    updated = dict(item)
                    updated["text"] = visible
                    blocks.append(updated)
                continue
        blocks.append(item)

    return blocks if blocks else ""


def _strip_duplicate_thinking_text(text: str, thinking: str) -> str:
    if not text or not thinking:
        return text
    # Exact match — provider duplicated reasoning in content field
    if text == thinking or text.strip() == thinking.strip():
        return ""
    # Prefix match — streaming chunk where content is a prefix of the
    # accumulated thinking text (or vice versa).  This happens when a
    # provider echoes reasoning_content in the content delta incrementally.
    if thinking.startswith(text) or text.startswith(thinking):
        return ""
    return text


def _render_stream_content(renderer: Any, content: object) -> None:
    if isinstance(content, str) and content:
        if _should_render_text_chunk(content):
            renderer.feed_text(content)
        return
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str) and _should_render_text_chunk(text):
                    renderer.feed_text(text)


def _is_empty_content(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return content == ""
    if isinstance(content, list):
        return not content
    return False


def _should_render_text_chunk(text: str) -> bool:
    normalized = _normalize_dsml(text).strip()
    if "DSML" in normalized and "<|" in normalized:
        return False
    if _looks_like_legacy_xml_tool_call(normalized):
        return False
    if _looks_like_provider_tool_call_fragment(normalized):
        return False
    return not _DSML_BOILERPLATE_RE.search(normalized)


def _extract_dsml_tool_calls(content: object) -> tuple[object, list[dict]]:
    if isinstance(content, str):
        return _extract_dsml_tool_calls_from_text(content)
    if not isinstance(content, list):
        return content, []

    blocks: list[object] = []
    calls: list[dict] = []
    for item in content:
        if isinstance(item, str):
            cleaned, found = _extract_dsml_tool_calls_from_text(item)
            calls.extend(found)
            if cleaned:
                blocks.append(cleaned)
            continue
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                cleaned, found = _extract_dsml_tool_calls_from_text(text)
                calls.extend(found)
                if cleaned:
                    updated = dict(item)
                    updated["text"] = cleaned
                    blocks.append(updated)
                continue
        blocks.append(item)

    return (blocks if blocks else ""), calls


def _extract_legacy_xml_tool_calls(content: object) -> tuple[object, list[dict]]:
    if isinstance(content, str):
        return _extract_legacy_xml_tool_calls_from_text(content)
    if not isinstance(content, list):
        return content, []

    blocks: list[object] = []
    calls: list[dict] = []
    for item in content:
        if isinstance(item, str):
            cleaned, found = _extract_legacy_xml_tool_calls_from_text(item)
            calls.extend(found)
            if cleaned:
                blocks.append(cleaned)
            continue
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                cleaned, found = _extract_legacy_xml_tool_calls_from_text(text)
                calls.extend(found)
                if cleaned:
                    updated = dict(item)
                    updated["text"] = cleaned
                    blocks.append(updated)
                continue
        blocks.append(item)

    return (blocks if blocks else ""), calls


def _extract_dsml_tool_calls_from_text(text: str) -> tuple[str, list[dict]]:
    normalized = _normalize_dsml(text)
    calls: list[dict] = []

    for block_match in _DSML_TOOL_CALLS_RE.finditer(normalized):
        block = block_match.group(0)
        for invoke_match in _DSML_INVOKE_RE.finditer(block):
            attrs = _parse_dsml_attrs(invoke_match.group(1))
            name = attrs.get("name", "").strip()
            if not name:
                continue
            args: dict[str, object] = {}
            body = invoke_match.group(2)
            for param_match in _DSML_PARAMETER_RE.finditer(body):
                param_attrs = _parse_dsml_attrs(param_match.group(1))
                param_name = param_attrs.get("name", "").strip()
                if not param_name:
                    continue
                args[param_name] = _decode_dsml_parameter(param_match.group(2), param_attrs)
            calls.append({
                "name": name,
                "args": args,
                "id": f"call_dsml_{uuid.uuid4().hex[:12]}",
                "type": "tool_call",
            })

    cleaned = _DSML_TOOL_CALLS_RE.sub("", normalized).strip()
    if calls and _DSML_BOILERPLATE_RE.search(cleaned) and len(cleaned) <= 160:
        cleaned = ""
    return cleaned, calls


def _extract_legacy_xml_tool_calls_from_text(text: str) -> tuple[str, list[dict]]:
    calls: list[dict] = []

    for block_match in _LEGACY_XML_TOOL_CALL_RE.finditer(text):
        block = block_match.group(0)
        name = _legacy_xml_tool_name(block)
        if not name:
            continue
        args = _legacy_xml_args(block)
        calls.append({
            "name": _normalize_legacy_xml_tool_name(name),
            "args": args,
            "id": f"call_xml_{uuid.uuid4().hex[:12]}",
            "type": "tool_call",
        })

    cleaned = _LEGACY_XML_TOOL_CALL_RE.sub("", text).strip() if calls else text
    return cleaned, calls


def _legacy_xml_tool_name(block: str) -> str:
    for tag in ("tool_name", "name", "tool"):
        value = _legacy_xml_tag_text(block, tag)
        if value:
            return value.strip()
    opening = block.split(">", 1)[0]
    attr_match = _LEGACY_XML_NAME_ATTR_RE.search(opening)
    if attr_match:
        return html.unescape(attr_match.group(1)).strip()
    args = _legacy_xml_arg_pairs(block)
    for key in ("tool_name", "name", "tool"):
        value = args.pop(key, None)
        if value is not None:
            return str(value).strip()
    return ""


def _normalize_legacy_xml_tool_name(name: str) -> str:
    return name.strip().replace("-", "_").lower()


def _legacy_xml_args(block: str) -> dict[str, object]:
    return {
        key: value
        for key, value in _legacy_xml_arg_pairs(block).items()
        if key not in {"tool_name", "name", "tool"}
    }


def _legacy_xml_arg_pairs(block: str) -> dict[str, object]:
    args: dict[str, object] = {}
    for pair in _LEGACY_XML_ARG_PAIR_RE.finditer(block):
        key = html.unescape(pair.group(1)).strip()
        if not key:
            continue
        args[key] = _decode_legacy_xml_arg(pair.group(2))
    return args


def _legacy_xml_tag_text(block: str, tag: str) -> str:
    match = re.search(
        rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>",
        block,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    return html.unescape(match.group(1)).strip()


def _decode_legacy_xml_arg(raw: str) -> object:
    value = html.unescape(raw)
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", stripped):
        try:
            return int(stripped)
        except ValueError:
            return stripped
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", stripped):
        try:
            return float(stripped)
        except ValueError:
            return stripped
    if stripped.startswith(("{", "[", '"')):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _looks_like_legacy_xml_tool_call(text: str) -> bool:
    lowered = text.lower()
    return (
        "<tool_call" in lowered
        or "</tool_call>" in lowered
        or ("<arg_key" in lowered and "<arg_value" in lowered)
    )


def _malformed_tool_call_format(content: object) -> str:
    for text in _content_text_fragments(content):
        normalized = _normalize_dsml(text)
        if _looks_like_malformed_dsml_tool_call(normalized):
            return "dsml"
        if _looks_like_legacy_xml_tool_call(text):
            return "legacy_xml"
        if _looks_like_provider_tool_call_fragment(text):
            return "provider_json"
    return ""


def _content_text_fragments(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    fragments: list[str] = []
    for item in content:
        if isinstance(item, str):
            fragments.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                fragments.append(text)
    return fragments


def _looks_like_malformed_dsml_tool_call(text: str) -> bool:
    return (
        "DSML" in text
        and "<" in text
        and any(marker in text for marker in ("tool_calls", "invoke", "parameter"))
    )


def _looks_like_provider_tool_call_fragment(text: str) -> bool:
    lowered = text.lower()
    return (
        "tool_calls" in lowered
        and any(marker in lowered for marker in ("function", "arguments", "name"))
        and any(char in text for char in ("{", "[", '"'))
    )


def _normalize_dsml(text: str) -> str:
    return text.replace("｜", "|")


def _parse_dsml_attrs(raw: str) -> dict[str, str]:
    return {key: html.unescape(value) for key, value in _DSML_ATTR_RE.findall(raw)}


def _decode_dsml_parameter(raw: str, attrs: dict[str, str]) -> object:
    value = html.unescape(raw)
    stripped = value.strip()

    if attrs.get("boolean", "").lower() == "true":
        return stripped.lower() == "true"
    if attrs.get("integer", "").lower() == "true":
        try:
            return int(stripped)
        except ValueError:
            return stripped
    if attrs.get("number", "").lower() == "true":
        try:
            return float(stripped)
        except ValueError:
            return stripped
    if attrs.get("json", "").lower() == "true":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    if attrs.get("string", "").lower() == "true":
        return value

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def extract_text(msg) -> str:
    content = msg.content if hasattr(msg, "content") else str(msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)
